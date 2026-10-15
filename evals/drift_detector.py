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
    if r.status_code != 200:
        return None
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        for name in zf.namelist():
            if name.endswith(".json"):
                return json.loads(zf.read(name))
    return None


def main() -> int:
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    threshold = float(os.environ.get("THRESHOLD_SIGMA", "2.0"))
    window = int(os.environ.get("WINDOW_DAYS", "7"))

    runs = _gh(f"/repos/{repo}/actions/workflows/continuous-eval.yml/runs",
               token, per_page=window + 5, status="success")["workflow_runs"]
    if len(runs) < 3:
        print(f"only {len(runs)} continuous-eval runs found — need >=3 for drift signal.")
        return 0

    reports: list[dict[str, Any]] = []
    for run in runs[: window + 1]:
        arts = _gh(f"/repos/{repo}/actions/runs/{run['id']}/artifacts", token)["artifacts"]
        for a in arts:
            if a["name"] == "continuous-eval-results":
                data = _download_artifact(repo, a["id"], token)
                if data:
                    reports.append(data)
                break

    if len(reports) < 3:
        print("could not fetch enough report artifacts.")
        return 0

    latest, *history = reports
    metrics: dict[str, list[float]] = {}
    for h in history:
        for k, v in (h.get("aggregates") or {}).items():
            if isinstance(v, (int, float)):
                metrics.setdefault(k, []).append(float(v))

    drifted: list[tuple[str, float, float, float]] = []
    for k, cur in (latest.get("aggregates") or {}).items():
        if not isinstance(cur, (int, float)) or k not in metrics or len(metrics[k]) < 3:
            continue
        mu, sigma = mean(metrics[k]), pstdev(metrics[k]) or 1e-6
        z = (float(cur) - mu) / sigma
        if abs(z) >= threshold and (
            # for score metrics, only flag drops; for pass metrics, flag either.
            (k.endswith("_score") and z < 0) or k.endswith("_pass") or "safety" in k.lower()
        ):
            drifted.append((k, float(cur), mu, z))

    if not drifted:
        print("no drift beyond threshold; all clear.")
        return 0

    for k, cur, mu, z in drifted:
        title = f"[eval-regression] {k} drifted {z:+.2f}σ on continuous eval"
        body = (
            f"### Drift detected\n\n"
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
