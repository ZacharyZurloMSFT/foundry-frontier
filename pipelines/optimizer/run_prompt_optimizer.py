"""Nightly Prompt/Agent Optimizer runner.

Wraps Foundry's Prompt Optimizer against one Northwind Insurance agent prompt
under agents/insurance/. Compares candidate variants against the current
golden-set baseline; if the best variant beats baseline by >= OPTIMIZER_MIN_LIFT
on the rubric evaluator, this script:

  1. Rewrites the agent's prompt file (`instructions:` block) with the winning
     prompt in-place.
  2. Emits GitHub Actions outputs `improved=true`, `lift=0.NNN` and
     `prompt_path=agents/insurance/<file>.yaml` so the workflow can open a PR.
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
PROMPTS_DIR = REPO_ROOT / "agents" / "insurance"
# Agent keys match the rubric names (`<agent>_rubric_score`).
PROMPT_FILES = {
    "triage": "triage.yaml",
    "policy_claim_lookup": "policy_claim_lookup.yaml",
    "policy_wording": "policy_wording.yaml",
    "claim_action_draft": "claim_action_draft.yaml",
    "orchestrator": "orchestrator.yaml",
}
# The canonical department slug comes first; the legacy slug is still read from older baseline files.
BASELINE_DEPARTMENTS = ("claims-concierge", "order-concierge")


def _load_agent_prompt(agent: str) -> tuple[Path, dict]:
    path = PROMPTS_DIR / PROMPT_FILES[agent]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return path, data


def _baseline_score(path: Path, metric: str) -> float:
    """aggregates.<department>.orchestrator.<metric> from the reviewer-approved baseline
    (a flat aggregates object is read as-is); 0.0 when no measured baseline exists."""
    if not path.exists():
        return 0.0
    aggregates = json.loads(path.read_text(encoding="utf-8")).get("aggregates", {})
    for department in BASELINE_DEPARTMENTS:
        if isinstance(aggregates.get(department), dict):
            aggregates = aggregates[department].get("orchestrator", {})
            break
    value = aggregates.get(metric, 0.0)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


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
    baseline_score = _baseline_score(Path(baseline_path), f"{agent}_rubric_score")
    lift = float(best.get("score", 0.0)) - baseline_score

    report = {
        "agent": agent, "prompt_path": path.relative_to(REPO_ROOT).as_posix(),
        "baseline_score": baseline_score,
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
    ap.add_argument("--agent", required=True, choices=sorted(PROMPT_FILES))
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
        prompt_path = (PROMPTS_DIR / PROMPT_FILES[args.agent]).relative_to(REPO_ROOT).as_posix()
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"improved={'true' if report.get('improved') else 'false'}\n")
            fh.write(f"lift={report.get('lift', 0.0):.4f}\n")
            fh.write(f"prompt_path={prompt_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
