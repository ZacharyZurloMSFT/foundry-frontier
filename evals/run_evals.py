"""Northwind evaluation runner.

Reads the golden JSONL dataset, invokes the target Northwind workflow for each
row (against the deployed API, or the local orchestrator for offline tests),
then scores every response with:
  - Foundry built-in evaluators (TaskAdherence, Groundedness, Coherence, safety)
  - Our custom WorkOrderCompleteness evaluator

Usage:
    python run_evals.py --dataset ../data/golden/dataset.jsonl \
                        --output ./results.json \
                        --baseline ./baseline.json   # optional, enables gate
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluators import judge_model_config_from_env, load_agent_rubric  # noqa: E402
from evaluators.work_order_completeness import evaluate as wo_complete  # noqa: E402

# Foundry evaluators SDK
try:
    from azure.ai.evaluation import (
        CoherenceEvaluator,
        HateUnfairnessEvaluator,
        TaskAdherenceEvaluator,
        ViolenceEvaluator,
    )
    from azure.identity import DefaultAzureCredential
    _HAVE_EVALS = True
except ImportError:  # allow lightweight local runs
    _HAVE_EVALS = False


API_BASE = os.environ.get("API_URI") or os.environ.get("API_BASE") or "http://localhost:8000"


class BaselineUnavailable(ValueError):
    pass


def validated_reference(path: str | None, target: str, aggregates: dict) -> dict:
    if not path or not Path(path).is_file():
        raise BaselineUnavailable("A reviewer-approved measured baseline is required")
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    base = document.get("aggregates") if isinstance(document, dict) else None
    if not isinstance(base, dict):
        raise ValueError("Baseline aggregates must be a metric object")
    if "order-concierge" in base:
        department = base["order-concierge"]
        if not isinstance(department, dict):
            raise ValueError("Baseline order-concierge section must be an object")
        reference = department.get(target, {})
    elif target == "orchestrator":
        reference = base
    else:
        raise BaselineUnavailable("Harness requires an order-concierge/copilot-sdk baseline")
    if not isinstance(reference, dict):
        raise ValueError("Baseline target metrics must be an object")
    if not reference:
        raise BaselineUnavailable("Baseline target metrics are empty")
    for key, value in reference.items():
        if not isinstance(key, str) or key not in aggregates or key in {"id", "response_len"}:
            raise ValueError(f"Unsupported or unmeasured baseline metric: {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Baseline metric must be a finite number: {key}")
        if not math.isfinite(aggregates[key]):
            raise ValueError(f"Current metric is not finite: {key}")
    return reference


async def run_workflow(row: dict) -> str:
    """Invoke the target workflow against the API and return the concatenated assistant response."""
    wf = row["workflow"]
    # Legacy Field Services workflow keys (triage / manual_retrieval /
    # work_order_draft / tech_lookup) are kept for backwards compat with
    # older golden datasets. Order Concierge adds order_lookup and
    # resolution_draft. Both sets route through /chat — the manager picks
    # the right specialist based on the prompt.
    if wf in {"triage", "manual_retrieval", "work_order_draft", "tech_lookup",
              "order_lookup", "resolution_draft"}:
        text = ""
        session_id = None
        workflow_id = None
        async with httpx.AsyncClient(timeout=90.0) as http:
            async with http.stream("POST", f"{API_BASE}/chat", json={"message": row["input"]}) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    ev = json.loads(line)
                    if ev.get("kind") == "error":
                        raise RuntimeError(ev.get("message", "Workflow failed"))
                    if ev.get("workflow_id"):
                        if workflow_id is not None and workflow_id != ev["workflow_id"]:
                            raise RuntimeError("Stream contains conflicting server workflow IDs")
                        workflow_id = ev["workflow_id"]
                    if ev.get("kind") == "session":
                        session_id = ev.get("session_id")
                    elif ev.get("kind") == "assistant_delta":
                        text += ev.get("text", "")
                    elif ev.get("kind") == "assistant_message" and ev.get("text"):
                        text = ev["text"]
            if wf in {"work_order_draft", "resolution_draft"}:
                if not workflow_id and not session_id:
                    raise RuntimeError("Draft evaluation requires a session-bound persisted approval")
                workflow_id = workflow_id or f"wf-{session_id}"
                saved = await http.get(f"{API_BASE}/hitl", params={"workflow_id": workflow_id})
                saved.raise_for_status()
                drafts = [item for item in saved.json().get("items", [])
                          if item.get("workflowId") == workflow_id and item.get("status") == "pending"]
                if len(drafts) != 1 or not isinstance(drafts[0].get("payload"), dict):
                    raise RuntimeError("Exactly one pending draft bound to this session is required")
                return json.dumps(drafts[0]["payload"])
        return text
    if wf == "cu_extraction":
        # For eval purposes we hit a helper endpoint (or read cached CU output)
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.get(f"{API_BASE}/cu/{row['input']}")
            if r.status_code == 200:
                return json.dumps(r.json())
        return ""
    return ""


def _score_row(
    row: dict,
    response: str,
    foundry_evals: dict | None,
    rubric_evals: dict | None = None,
) -> dict[str, Any]:
    scores: dict[str, Any] = {
        "id": row["id"], "workflow": row["workflow"],
        "response_len": len(response),
        "response": response,
    }
    if row["workflow"] in ("work_order_draft", "resolution_draft"):
        scores.update(wo_complete(response=response))
    if rubric_evals is not None:
        ev = rubric_evals.get(row["workflow"])
        if ev is not None:
            scores.update(ev(query=row["input"], response=response))
    if foundry_evals is not None:
        for name, ev in foundry_evals.items():
            try:
                r = ev(query=row["input"], response=response)
                scores[name] = r
            except Exception as exc:  # noqa: BLE001
                scores[name] = {"error": str(exc)}
    return scores


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--baseline", default=None, help="If provided, fail on any metric regression > 0.05")
    ap.add_argument("--target", choices=["orchestrator", "copilot-sdk"], default="orchestrator")
    ap.add_argument("--expected-build", help="Require the API's deployed git_sha to match this candidate.")
    args = ap.parse_args()

    rows = [json.loads(line) for line in Path(args.dataset).read_text().splitlines() if line.strip()]
    if args.expected_build:
        async with httpx.AsyncClient(timeout=30) as http:
            build = await http.get(f"{API_BASE}/build")
            build.raise_for_status()
            if build.json()["build"]["git_sha"] != args.expected_build:
                Path(args.output).write_text(json.dumps({
                    "aggregates": {}, "rows": [], "target": args.target,
                    "evaluation_status": "candidate_build_mismatch",
                }), encoding="utf-8")
                return 2

    foundry_evals = None
    judge_cfg = judge_model_config_from_env()
    if _HAVE_EVALS and os.environ.get("FOUNDRY_PROJECT_ENDPOINT") and judge_cfg:
        model_cfg = judge_cfg
        foundry_evals = {
            "coherence": CoherenceEvaluator(model_config=model_cfg),
            "task_adherence": TaskAdherenceEvaluator(model_config=model_cfg),
            "hate_unfairness": HateUnfairnessEvaluator(azure_ai_project=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=DefaultAzureCredential()),
            "violence": ViolenceEvaluator(azure_ai_project=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=DefaultAzureCredential()),
        }
        # Groundedness only for manual_retrieval — needs context, added ad-hoc below

    rubric_evals: dict[str, Any] | None = None
    if judge_cfg is not None:
        # Add per-agent rubric evaluators here as their .prompty files land.
        rubric_evals = {}
        for wf in {"triage"}:
            try:
                rubric_evals[wf] = load_agent_rubric(wf, judge_cfg)
            except FileNotFoundError:
                pass

    results = []
    for row in rows:
        if args.target == "copilot-sdk":
            from harness_eval import grade, invoke
            try:
                run = await invoke(row["input"], API_BASE)
                # The deterministic contract checks the stored resolution JSON, not prose.
                scores = _score_row({**row, "workflow": "harness"}, run["output"], foundry_evals, rubric_evals)
                scores.update(grade(run, row))
                scores.update(workflow=row["workflow"], run_id=run["run_id"])
                results.append(scores)
            except Exception as exc:
                logging.getLogger(__name__).exception("Harness evaluation failed for %s", row["id"])
                results.append({"id": row["id"], "workflow": row["workflow"],
                                "harness_contract_pass": 0.0, "error": str(exc)})
        else:
            try:
                response = await run_workflow(row)
                results.append(_score_row(row, response, foundry_evals, rubric_evals))
            except Exception as exc:
                results.append({"id": row["id"], "workflow": row["workflow"], "error": str(exc)})

    aggregates: dict[str, float] = {}
    for result in results:
        for evaluator, scores in list(result.items()):
            if isinstance(scores, dict) and evaluator != "fields_present":
                for metric, value in scores.items():
                    if isinstance(value, (int, float)):
                        if math.isfinite(value):
                            result[f"{evaluator}.{metric}"] = value
                        else:
                            result["error"] = f"{evaluator}.{metric} was not a finite score."
    for r in results:
        for k, v in r.items():
            if isinstance(v, (int, float)) and k != "response_len":
                aggregates[k] = aggregates.get(k, 0.0) + float(v)
    for k in aggregates:
        aggregates[k] = round(aggregates[k] / max(1, len(results)), 4)

    payload = {"aggregates": aggregates, "rows": results, "target": args.target,
               "judge_status": "configured" if foundry_evals is not None else "unavailable"}
    reference = {}
    baseline_status = "evaluated"
    try:
        reference = validated_reference(args.baseline, args.target, aggregates)
    except BaselineUnavailable as exc:
        payload["baseline_error"] = str(exc)
        baseline_status = "unavailable"
    except (ValueError, TypeError, OSError) as exc:
        payload["baseline_error"] = str(exc)
        baseline_status = "invalid"
    payload["regressions"] = [key for key, value in reference.items() if aggregates[key] + 0.05 < value]
    harness_incomplete = False
    if args.target == "copilot-sdk":
        harness_incomplete = not reference or foundry_evals is None
        payload["regression_status"] = baseline_status if baseline_status != "evaluated" else "unavailable" if harness_incomplete else "evaluated"
        payload["baseline_note"] = "Harness metrics require a reviewer-approved order-concierge/copilot-sdk baseline."
    else:
        incomplete = not reference or foundry_evals is None or (
            any(row["workflow"] == "triage" for row in rows) and not (rubric_evals or {}).get("triage")
        )
        payload["regression_status"] = baseline_status if baseline_status != "evaluated" else "unavailable" if incomplete else "evaluated"
    Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(aggregates, indent=2))

    if args.target == "copilot-sdk":
        failures = not results or any(
            r.get("harness_contract_pass") != 1.0 or r.get("error")
            or any(isinstance(v, dict) and v.get("error") for v in r.values())
            for r in results
        )
        return 1 if failures or payload.get("regressions") else 2 if harness_incomplete else 0

    failures = not results or any(
        r.get("error") or any(isinstance(v, dict) and v.get("error") for v in r.values())
        or ("work_order_completeness" in r and (r["work_order_completeness"] != 1.0 or not r["plausible"]))
        or ("triage_rubric_pass" in r and not r["triage_rubric_pass"])
        for r in results
    )
    return 1 if failures or payload.get("regressions") else 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
