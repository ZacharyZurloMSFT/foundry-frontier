"""Nightly Prompt/Agent Optimizer runner.

Wraps Foundry's Prompt Optimizer against a single agent's prompty/yaml.
Compares candidate variants against the current golden-set baseline; if
the best variant beats baseline by >= OPTIMIZER_MIN_LIFT on the rubric
evaluator, this script:

  1. Rewrites `agents/<agent>.yaml` (`instructions:` block) with the winning
     prompt in-place.
  2. Emits GitHub Actions outputs `improved=true`, `lift=0.NNN` so the
     workflow can open a PR.
  3. Writes a JSON report summarising all candidates.

If no lift is found, exits cleanly with `improved=false`.

Usage:
    python pipelines/optimizer/run_prompt_optimizer.py \
        --agent triage \
        --dataset data/golden/dataset.jsonl \
        --baseline evals/baseline.json \
        --report optimizer-report-triage.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_agent_prompt(agent: str) -> tuple[Path, dict]:
    path = REPO_ROOT / "agents" / f"{agent}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return path, data


def _write_agent_prompt(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _try_import_optimizer():
    """Foundry Prompt Optimizer surface has moved several times; try the
    most likely import paths in order and return whichever wins."""
    for mod, attr in (
        ("azure.ai.evaluation.prompt_optimizer", "PromptOptimizer"),
        ("azure.ai.projects.prompt_optimizer", "PromptOptimizer"),
        ("azure.ai.evaluation.optimizers", "PromptOptimizer"),
    ):
        try:
            m = __import__(mod, fromlist=[attr])
            return getattr(m, attr)
        except (ImportError, AttributeError):
            continue
    return None


def run(agent: str, dataset: str, baseline_path: str, min_lift: float) -> dict:
    path, data = _load_agent_prompt(agent)
    current = str(data.get("instructions") or "").strip()

    Optimizer = _try_import_optimizer()
    if Optimizer is None:
        return {
            "agent": agent, "improved": False, "lift": 0.0,
            "reason": "Prompt Optimizer SDK not available in this environment; "
                      "workflow will exit clean until it's installed.",
            "current_prompt_preview": current[:200],
        }

    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    judge = os.environ.get("FOUNDRY_JUDGE_DEPLOYMENT", "gpt-5.4")

    baseline_scores = {}
    bp = Path(baseline_path)
    if bp.exists():
        baseline_scores = json.loads(bp.read_text(encoding="utf-8"))

    opt = Optimizer(azure_ai_project=endpoint, judge_deployment=judge)
    result = opt.optimize(
        seed_prompt=current, dataset_path=dataset,
        target_metric=f"{agent}_rubric_score",
    )

    candidates = getattr(result, "candidates", None) or result.get("candidates", [])
    if not candidates:
        return {"agent": agent, "improved": False, "lift": 0.0,
                "reason": "optimizer returned no candidates."}

    best = max(candidates, key=lambda c: c.get("score", 0.0))
    baseline_score = float(baseline_scores.get(f"{agent}_rubric_score", 0.0))
    lift = float(best.get("score", 0.0)) - baseline_score

    report = {
        "agent": agent, "baseline_score": baseline_score,
        "best_score": best.get("score"), "lift": lift,
        "min_lift": min_lift, "candidates": candidates,
        "chosen_prompt_preview": best.get("prompt", "")[:300],
    }
    if lift >= min_lift and best.get("prompt"):
        data["instructions"] = best["prompt"]
        _write_agent_prompt(path, data)
        report["improved"] = True
    else:
        report["improved"] = False
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    min_lift = float(os.environ.get("OPTIMIZER_MIN_LIFT", "0.05"))
    try:
        report = run(args.agent, args.dataset, args.baseline, min_lift)
    except Exception as e:  # noqa: BLE001
        report = {"agent": args.agent, "improved": False, "lift": 0.0,
                  "error": str(e)}

    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"agent": args.agent, "improved": report.get("improved"),
                      "lift": report.get("lift", 0.0)}, indent=2))

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"improved={'true' if report.get('improved') else 'false'}\n")
            fh.write(f"lift={report.get('lift', 0.0):.4f}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
