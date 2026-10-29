"""Northwind Insurance evaluation runner (Tier 2).

Reads the golden JSONL dataset, posts each row to POST /insurance/chat on the deployed API
(or a local one for offline tests), then scores every response with:
  - Foundry built-in evaluators (TaskAdherence, Coherence, HateUnfairness, Violence)
  - ClaimActionCompleteness (deterministic) on the claim-action draft saved in the
    human-review queue (GET /hitl)
  - LLM-as-judge rubrics: `triage` rows are judged on the triage specialist's JSON, `safety`
    rows on the reply to a coverage / approval / payment demand or an injected instruction

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
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluators import judge_model_config_from_env, load_agent_rubric  # noqa: E402
from evaluators.claim_action_completeness import evaluate as score_claim_action  # noqa: E402
from evaluators.expectations import evaluate as score_expectations  # noqa: E402
from evals.evidence import file_hash, report as evidence_report  # noqa: E402

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

# Golden-row workflows. Every row is posted to /insurance/chat; the orchestrator picks the specialists.
CHAT_WORKFLOWS = {"triage", "policy_wording", "policy_claim_lookup", "claim_action_draft", "safety"}
DRAFT_WORKFLOWS = {"claim_action_draft"}
RUBRIC_WORKFLOWS = ("triage", "safety")
ACTION_KIND = "insurance_claim_action"
# The canonical department slug comes first; the legacy slug is still read from older baseline files.
BASELINE_DEPARTMENTS = ("claims-concierge", "order-concierge")


class BaselineUnavailable(ValueError):
    pass


def validated_reference(path: str | None, target: str, aggregates: dict) -> dict:
    if not path or not Path(path).is_file():
        raise BaselineUnavailable("A reviewer-approved measured baseline is required")
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    base = document.get("aggregates") if isinstance(document, dict) else None
    if not isinstance(base, dict):
        raise ValueError("Baseline aggregates must be a metric object")
    department_key = next((key for key in BASELINE_DEPARTMENTS if key in base), None)
    if department_key is not None:
        department = base[department_key]
        if not isinstance(department, dict):
            raise ValueError(f"Baseline {department_key} section must be an object")
        reference = department.get(target, {})
    elif target == "orchestrator":
        reference = base
    else:
        raise BaselineUnavailable("Harness requires a claims-concierge/copilot-sdk baseline")
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


async def chat_turn(http: httpx.AsyncClient, message: str) -> dict:
    """Stream one /insurance/chat turn and collect the assistant text, the server-bound
    workflow ID and the specialists' tool results (keyed by tool name)."""
    text = ""
    session_id = None
    workflow_id = None
    tool_names: dict[str, str] = {}
    specialist_results: dict[str, list[str]] = {}
    events = []
    async with http.stream("POST", f"{API_BASE}/insurance/chat", json={"message": message}) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            ev = json.loads(line)
            events.append(ev)
            kind = ev.get("kind")
            if kind == "error":
                raise RuntimeError(ev.get("message", "Workflow failed"))
            if ev.get("workflow_id"):
                if workflow_id is not None and workflow_id != ev["workflow_id"]:
                    raise RuntimeError("Stream contains conflicting server workflow IDs")
                workflow_id = ev["workflow_id"]
            if kind == "session":
                session_id = ev.get("session_id")
            elif kind == "assistant_delta":
                text += ev.get("text", "")
            elif kind == "assistant_message" and ev.get("text"):
                text = ev["text"]
            elif kind == "tool_call" and ev.get("call_id") and ev.get("tool"):
                tool_names[ev["call_id"]] = ev["tool"]
            elif kind == "tool_result" and isinstance(ev.get("result"), str) and ev["result"].strip():
                tool = tool_names.get(ev.get("call_id"), "")
                specialist_results.setdefault(tool, []).append(ev["result"])
    return {"text": text, "session_id": session_id, "workflow_id": workflow_id,
            "tools_called": list(dict.fromkeys(tool_names.values())), "specialist_results": specialist_results,
            "events": events, "tool_call_count": len(tool_names)}


async def saved_draft(http: httpx.AsyncClient, turn: dict) -> str:
    """The claim-action draft persisted for this turn's server-bound workflow, as JSON."""
    workflow_id, session_id = turn["workflow_id"], turn["session_id"]
    if not workflow_id and not session_id:
        raise RuntimeError("Draft evaluation requires a session-bound persisted approval")
    workflow_id = workflow_id or f"wf-insurance-{session_id}"
    saved = await http.get(f"{API_BASE}/hitl", params={"workflow_id": workflow_id})
    saved.raise_for_status()
    drafts = [item for item in saved.json().get("items", [])
              if item.get("workflowId") == workflow_id and item.get("status") == "pending"
              and item.get("kind") == ACTION_KIND and item.get("execution_status") == "not_executed"]
    if len(drafts) != 1 or not isinstance(drafts[0].get("payload"), dict):
        raise RuntimeError("Exactly one pending draft bound to this session is required")
    return json.dumps(drafts[0]["payload"])


async def run_row(row: dict) -> dict:
    """Invoke the target workflow against the API. `response` is what the general evaluators
    grade; `rubric_response` is what the LLM-as-judge rubrics grade (the triage specialist's
    JSON when the orchestrator called it, otherwise the same text)."""
    started = time.perf_counter()
    wf = row["workflow"]
    if wf not in CHAT_WORKFLOWS:
        raise ValueError(f"Unsupported golden-row workflow: {wf}")
    async with httpx.AsyncClient(timeout=90.0) as http:
        turn = await chat_turn(http, row["input"])
        if wf in DRAFT_WORKFLOWS:
            response = await saved_draft(http, turn)
        else:
            response = turn["text"]
    triage = next((r for r in turn["specialist_results"].get("triage", []) if r.strip()), None)
    return {"response": response, "assistant_response": turn["text"],
            "rubric_response": triage if wf == "triage" and triage else response,
            "tools_called": turn["tools_called"], "events": turn["events"],
            "specialist_results": turn["specialist_results"],
            "session_id": turn["session_id"], "workflow_id": turn["workflow_id"],
            "tool_call_count": turn["tool_call_count"],
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "evidence_scope": "API execution and specialist outputs; internal specialist traces unverified."}


async def run_workflow(row: dict) -> str:
    return (await run_row(row))["response"]


def _score_row(
    row: dict,
    response: str,
    foundry_evals: dict | None,
    rubric_evals: dict | None = None,
    *,
    rubric_response: str | None = None,
) -> dict[str, Any]:
    scores: dict[str, Any] = {
        "id": row["id"], "workflow": row["workflow"],
        "query": row.get("input"),
        "expected_behavior": json.dumps(
            row.get("expected_behavior", row.get("expected", {})),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "response_len": len(response),
        "response": response,
    }
    if row["workflow"] in DRAFT_WORKFLOWS:
        scores.update(score_claim_action(response=response, expected=row.get("expected")))
    elif row["workflow"] in CHAT_WORKFLOWS:
        scores.update(score_expectations(row, rubric_response if row["workflow"] == "triage" and rubric_response else response))
    if rubric_evals is not None:
        ev = rubric_evals.get(row["workflow"])
        if ev is not None:
            scores.update(ev(
                query=row["input"], response=response if rubric_response is None else rubric_response,
                context=json.dumps({"expected": row.get("expected", {})}),
            ))
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

    rows = [json.loads(line) for line in Path(args.dataset).read_text(encoding="utf-8").splitlines() if line.strip()]
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

    rubric_evals: dict[str, Any] | None = None
    if judge_cfg is not None:
        # Add per-workflow rubric evaluators here as their .prompty files land.
        rubric_evals = {}
        for wf in RUBRIC_WORKFLOWS:
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
                # The deterministic contract checks the stored claim-action draft, not prose.
                scores = _score_row({**row, "workflow": "harness"}, run["output"], foundry_evals, rubric_evals)
                scores.update(grade(run, row))
                scores.update(workflow=row["workflow"], run_id=run["run_id"])
                scores.update({
                    "events": run.get("events", []), "workflow_id": run.get("workflow_id"),
                    "latency_ms": run.get("latency_ms"),
                    "tool_call_count": sum(e.get("kind") == "tool_start" for e in run.get("events", [])),
                    "evidence_scope": "Harness events and persisted approval; internal specialist traces unverified.",
                })
                results.append(scores)
            except Exception as exc:
                logging.getLogger(__name__).exception("Harness evaluation failed for %s", row["id"])
                results.append({"id": row["id"], "workflow": row["workflow"],
                                "harness_contract_pass": 0.0, "error": str(exc)})
        else:
            try:
                turn = await run_row(row)
                scores = _score_row(row, turn["response"], foundry_evals, rubric_evals,
                                    rubric_response=turn["rubric_response"])
                final_safety = score_expectations({"workflow": "safety"}, turn["assistant_response"])
                scores["critical_safety_pass"] = min(
                    scores.get("critical_safety_pass", 1.0), final_safety["critical_safety_pass"])
                scores["final_response_failures"] = final_safety["expectation_failures"]
                if not turn["assistant_response"].strip():
                    scores["error"] = "No final assistant output was returned."
                scores.update({key: value for key, value in turn.items() if key not in {"response", "rubric_response"}})
                results.append(scores)
            except Exception as exc:
                results.append({"id": row["id"], "workflow": row["workflow"], "error": str(exc)})

    payload = evidence_report(results, target=args.target)
    aggregates = payload["aggregates"]
    payload.update(
        judge_status="configured" if foundry_evals is not None else "unavailable",
        dataset_sha256=file_hash(Path(args.dataset)),
        expected_build=args.expected_build,
        provenance_status="unverified_specialist_versions",
        publication_status="not_published",
    )
    root = Path(__file__).resolve().parents[1]
    payload["local_sources_sha256"] = {
        str(path.relative_to(root)): file_hash(path)
        for path in [
            Path(__file__), Path(__file__).with_name("evidence.py"),
            Path(__file__).with_name("harness_eval.py"),
            *sorted((root / "agents" / "insurance").glob("*.yaml")),
            *sorted((root / "evals" / "evaluators" / "rubrics").glob("*.prompty")),
        ]
    }
    payload["judge_deployment"] = judge_cfg["azure_deployment"] if judge_cfg else None
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
        payload["baseline_note"] = "Harness metrics require a reviewer-approved claims-concierge/copilot-sdk baseline."
    else:
        incomplete = not reference or foundry_evals is None or any(
            any(row["workflow"] == wf for row in rows) and not (rubric_evals or {}).get(wf)
            for wf in RUBRIC_WORKFLOWS
        )
        payload["regression_status"] = baseline_status if baseline_status != "evaluated" else "unavailable" if incomplete else "evaluated"
    if args.target == "copilot-sdk":
        failures = not results or any(
            r.get("harness_contract_pass") != 1.0 or r.get("error")
            or any(isinstance(v, dict) and v.get("error") for v in r.values())
            for r in results
        )
        code = 1 if failures or payload.get("regressions") else 2 if harness_incomplete else 0
    else:
        failures = not results or any(
            r.get("error") or any(isinstance(v, dict) and v.get("error") for v in r.values())
            or r.get("expectation_pass") == 0.0 or r.get("critical_safety_pass") == 0.0
            or ("claim_action_completeness" in r and (
                r["claim_action_completeness"] != 1.0 or not r["plausible"] or r.get("expected_match") == 0.0))
            or any(f"{wf}_rubric_pass" in r and not r[f"{wf}_rubric_pass"] for wf in RUBRIC_WORKFLOWS)
            for r in results
        )
        code = 1 if failures or payload.get("regressions") else 2 if incomplete else 0
    payload["evaluation_status"] = {0: "evaluated", 1: "failed", 2: "incomplete"}[code]
    Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(aggregates, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
