"""Drift detector — reads the last N nights of continuous-eval artifacts
from the GitHub Actions API, computes a rolling z-score per metric, and
files an issue when anything drifts >THRESHOLD_SIGMA from the trailing
WINDOW_DAYS baseline.

The issue template `eval-regression` is used so the GitHub Copilot coding
agent (which watches for `copilot-fix` labels) picks it up automatically.

Env:
    GITHUB_TOKEN          — Actions token, must have contents:read + issues:write
    GITHUB_REPOSITORY     — owner/repo (set by Actions)
    THRESHOLD_SIGMA       — default 2.0
    WINDOW_DAYS           — default 7
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import zipfile
from statistics import mean, pstdev
from typing import Any

import httpx

API = "https://api.github.com"


def _gh(path: str, token: str, **params: Any) -> Any:
    r = httpx.get(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        params=params or None, timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _download_artifact(repo: str, artifact_id: int, token: str) -> dict | None:
    r = httpx.get(
        f"{API}/repos/{repo}/actions/artifacts/{artifact_id}/zip",
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=True, timeout=60.0,
    )
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        for name in zf.namelist():
            if name == "continuous-eval-results.json":
                result = json.loads(zf.read(name))
                if not isinstance(result, dict):
                    raise ValueError("Continuous-eval artifact requires a report object with aggregates.")
                return result
    return None


def detect_drift(latest: dict, history: list[dict], threshold: float) -> list[tuple[str, float, float, float]]:
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("Drift threshold must be positive and finite.")
    if latest.get("evaluation_status") not in {"evaluated", "failed"}:
        raise ValueError("Latest report is not complete measured evidence.")
    identity = ("target", "evidence_scope", "evaluator_version")
    if any(not latest.get(key) for key in identity):
        raise ValueError("Target, evidence scope and evaluator version are required for comparison.")
    comparable = [h for h in history if h.get("evaluation_status") in {"evaluated", "failed"}
                  and all(h.get(key) == latest[key] for key in identity)]
    if len(comparable) < 3:
        raise ValueError("Need at least three comparable historical reports.")
    metrics: dict[str, list[float]] = {}
    for historical in comparable:
        for key, value in historical.get("aggregates", {}).items():
            if type(value) in (int, float) and math.isfinite(value):
                metrics.setdefault(key, []).append(float(value))
    drifted = []
    compared = 0
    for key, current in latest.get("aggregates", {}).items():
        if type(current) not in (int, float) or not math.isfinite(current) or len(metrics.get(key, [])) < 3:
            continue
        if not (key.endswith(("_score", "_pass")) or "safety" in key.lower()):
            continue
        compared += 1
        mu, sigma = mean(metrics[key]), pstdev(metrics[key])
        # A constant history has no z-score. Only actual deterioration is actionable.
        if current >= mu:
            continue
        z = (float(current) - mu) / max(sigma, 1e-6)
        if z <= -threshold:
            drifted.append((key, float(current), mu, z))
    if not compared:
        raise ValueError("No comparable measured score/pass metrics.")
    return drifted


def main() -> int:
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    threshold = float(os.environ.get("THRESHOLD_SIGMA", "2.0"))
    window = int(os.environ.get("WINDOW_DAYS", "7"))
    if window < 3:
        raise ValueError("At least three historical reports are required.")

    runs = _gh(f"/repos/{repo}/actions/workflows/continuous-eval.yml/runs",
               token, per_page=min(window + 5, 100), status="completed")["workflow_runs"]
    if len(runs) < 4:
        print(f"only {len(runs)} continuous-eval runs found — need >=4 for drift signal.")
        return 2

    reports: list[dict[str, Any]] = []
    for index, run in enumerate(runs[: window + 1]):
        arts = _gh(f"/repos/{repo}/actions/runs/{run['id']}/artifacts", token)["artifacts"]
        found = False
        for a in arts:
            if a["name"] == "continuous-eval-results":
                data = _download_artifact(repo, a["id"], token)
                if data:
                    reports.append(data)
                    found = True
                break
        if index == 0 and not found:
            print("Latest completed run has no evaluation report; refusing to substitute an older run.")
            return 2

    if len(reports) < 4:
        print("need a latest report plus at least three historical reports.")
        return 2
    latest, *history = reports
    try:
        drifted = detect_drift(latest, history, threshold)
    except ValueError as exc:
        print(f"Drift evidence incomplete: {exc}", file=sys.stderr)
        return 2

    if not drifted:
        print("no drift beyond threshold; all clear.")
        return 0

    existing = _gh(f"/repos/{repo}/issues", token, state="open", labels="eval-regression", per_page=100)
    for k, cur, mu, z in drifted:
        title = f"[eval-regression] {k} drifted {z:+.2f}σ on continuous eval"
        marker = f"<!-- eval-drift:{latest['target']}:{latest['evidence_scope']}:{k} -->"
        if any(marker in (issue.get("body") or "") for issue in existing):
            print(f"existing open drift issue for {k}")
            continue
        body = (
            f"{marker}\n### Drift detected\n\n"
            f"- **Evidence scope:** `{latest['evidence_scope']}`\n"
            f"- **Evaluator:** `{latest['evaluator_version']}`\n"
            f"- **Metric:** `{k}`\n"
            f"- **Latest:** `{cur:.4f}`\n"
            f"- **7-day mean:** `{mu:.4f}`\n"
            f"- **Z-score:** `{z:+.2f}` (threshold ±{threshold})\n\n"
            f"The GitHub Copilot coding agent will pick this issue up "
            f"(label `copilot-fix`) and draft a candidate fix PR.\n"
        )
        httpx.post(
            f"{API}/repos/{repo}/issues",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            json={"title": title, "body": body,
                  "labels": ["eval-regression", "drift", "copilot-fix"]},
            timeout=30.0,
        ).raise_for_status()
        print(f"filed issue for {k}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
