"""Publish captured insurance-agent executions to a Foundry evaluation run.

This command judges responses already captured by ``run_evals.py``. It never
replays prompts against a model or invokes the insurance API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))
from foundry_rubrics import AGENT_RUBRICS  # noqa: E402

sys.path.insert(0, str(REPO))
from evals.evidence import file_hash  # noqa: E402

WORKFLOW_EVALUATORS = {
    "triage": "triage",
    "policy_claim_lookup": "policy_claim_lookup",
    "policy_wording": "policy_wording",
    "claim_action_draft": "claim_action_draft",
    "safety": "orchestrator",
}
BUILD_SHA = re.compile(r"^[0-9a-f]{40}$")


def prepare_rows(report: dict[str, Any], *, dataset_sha256: str, max_rows: int) -> list[dict[str, Any]]:
    """Validate a complete, synthetic API run and shape its actual outputs for Foundry."""
    if report.get("target") != "orchestrator":
        raise ValueError("Foundry publication currently supports only captured orchestrator runs.")
    if report.get("dataset_sha256") != dataset_sha256:
        raise ValueError("Only the repository's synthetic golden dataset may be published.")
    source_id = report.get("evaluation_run_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("The source report has no evaluation_run_id.")
    build = report.get("expected_build")
    if not isinstance(build, str) or not BUILD_SHA.fullmatch(build):
        raise ValueError("A full 40-character expected_build SHA is required for Foundry publication.")
    rows = report.get("rows")
    if not isinstance(rows, list) or not rows or report.get("row_count") != len(rows):
        raise ValueError("The source report must contain every row from a non-empty evaluation.")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < len(rows):
        raise ValueError(f"--max-rows must be at least the source row count ({len(rows)}).")

    prepared = []
    seen_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("The source report contains a malformed row.")
        row_id = row.get("id")
        query = row.get("query")
        response = row.get("response")
        workflow = row.get("workflow")
        if not isinstance(row_id, str) or not row_id or row_id in seen_ids:
            raise ValueError("Every source row must have a unique, non-empty id.")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"Source row {row_id} has no captured query.")
        if not isinstance(response, str) or not response.strip():
            raise ValueError(f"Source row {row_id} has no captured agent response.")
        if row.get("error"):
            raise ValueError(f"Source row {row_id} is incomplete: {row['error']}")
        if not isinstance(row.get("evidence_scope"), str) or not row["evidence_scope"].startswith("API execution"):
            raise ValueError(f"Source row {row_id} is not verified API-execution evidence.")
        if workflow not in WORKFLOW_EVALUATORS:
            raise ValueError(f"Unsupported Foundry evaluation workflow: {workflow!r}")

        seen_ids.add(row_id)
        expected = row.get("expected_behavior", row.get("expected"))
        if expected is None:
            expected = {}
        if not isinstance(expected, str):
            expected = json.dumps(expected, ensure_ascii=False, sort_keys=True)
        prepared.append({
            "id": row_id,
            "query": query,
            "response": response,
            "workflow": workflow,
            "expected_behavior": expected,
            "source_run_id": source_id,
            "source_build": build,
            "execution_evidence": json.dumps({
                "tools_called": row.get("tools_called", []),
                "tool_call_count": row.get("tool_call_count"),
                "evidence_scope": row["evidence_scope"],
            }, ensure_ascii=False, sort_keys=True),
        })
    return prepared


def evaluator_name(agent: str) -> str:
    prompt = AGENT_RUBRICS[agent]["prompt_text"]
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    return f"northwind_{agent}_rubric_{digest}"


def ensure_evaluator(project_client: Any, agent: str) -> str:
    from azure.ai.projects.models import EvaluatorCategory, EvaluatorDefinitionType

    spec = AGENT_RUBRICS[agent]
    name = evaluator_name(agent)
    if any(item.name == name for item in project_client.beta.evaluators.list(type="custom")):
        return name
    definition = {
        "name": name,
        "categories": [EvaluatorCategory.QUALITY],
        "display_name": spec["display_name"],
        "description": spec["description"],
        "definition": {
            "type": EvaluatorDefinitionType.PROMPT,
            "prompt_text": spec["prompt_text"],
            "init_parameters": {
                "type": "object",
                "properties": {"deployment_name": {"type": "string"}},
                "required": ["deployment_name"],
            },
            "data_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "response": {"type": "string"},
                    "expected_behavior": {"type": "string"},
                    "execution_evidence": {"type": "string"},
                },
                "required": ["query", "response", "expected_behavior", "execution_evidence"],
            },
            "metrics": {
                name: {
                    "type": "ordinal",
                    "desirable_direction": "increase",
                    "min_value": 1,
                    "max_value": 5,
                }
            },
        },
    }
    project_client.beta.evaluators.create_version(name=name, evaluator_version=definition)
    return name


def evaluation_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "query": {"type": "string"},
            "response": {"type": "string"},
            "workflow": {"type": "string"},
            "expected_behavior": {"type": "string"},
            "source_run_id": {"type": "string"},
            "source_build": {"type": "string"},
            "execution_evidence": {"type": "string"},
        },
        "required": [
            "id", "query", "response", "workflow", "expected_behavior",
            "source_run_id", "source_build", "execution_evidence",
        ],
    }


def evaluation_criterion(agent: str, evaluator: str, judge_deployment: str) -> dict[str, Any]:
    return {
        "type": "azure_ai_evaluator",
        "name": f"{agent}_rubric",
        "evaluator_name": evaluator,
        "data_mapping": {
            "query": "{{item.query}}",
            "response": "{{item.response}}",
            "expected_behavior": "{{item.expected_behavior}}",
            "execution_evidence": "{{item.execution_evidence}}",
        },
        "initialization_parameters": {"deployment_name": judge_deployment},
    }


def ensure_evaluation_definition(
    project_client: Any,
    *,
    agent: str,
    judge_deployment: str,
) -> dict[str, Any]:
    evaluator = ensure_evaluator(project_client, agent)
    client = project_client.get_openai_client()
    name = f"northwind-{agent}-{evaluator.rsplit('_', 1)[-1]}-{judge_deployment}"
    existing = next((item for item in client.evals.list(limit=100) if item.name == name), None)
    if existing is not None:
        return {"agent": agent, "evaluator": evaluator, "eval_name": name, "eval_id": existing.id, "created": False}
    evaluation = client.evals.create(
        name=name,
        data_source_config={
            "type": "custom",
            "item_schema": evaluation_item_schema(),
            "include_sample_schema": False,
        },
        testing_criteria=[evaluation_criterion(agent, evaluator, judge_deployment)],
    )
    return {"agent": agent, "evaluator": evaluator, "eval_name": name, "eval_id": evaluation.id, "created": True}


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def run_group(
    project_client: Any,
    rows: list[dict[str, Any]],
    *,
    agent: str,
    judge_deployment: str,
    source_run_id: str,
) -> dict[str, Any]:
    evaluator = ensure_evaluator(project_client, agent)
    client = project_client.get_openai_client()
    eval_obj = client.evals.create(
        name=f"northwind-{agent}-{source_run_id[:8]}",
        data_source_config={
            "type": "custom",
            "item_schema": evaluation_item_schema(),
            "include_sample_schema": False,
        },
        testing_criteria=[evaluation_criterion(agent, evaluator, judge_deployment)],
    )
    run = client.evals.runs.create(
        eval_id=eval_obj.id,
        name=f"{agent}-{source_run_id[:8]}",
        data_source={
            "type": "jsonl",
            "source": {"type": "file_content", "content": [{"item": row} for row in rows]},
        },
    )
    terminal = {"completed", "failed", "canceled", "cancelled"}
    for _ in range(120):
        status = str(getattr(run.status, "value", run.status)).lower()
        if status in terminal:
            break
        time.sleep(3)
        run = client.evals.runs.retrieve(eval_id=eval_obj.id, run_id=run.id)
    else:
        raise TimeoutError(f"Foundry evaluation run {run.id} did not reach a terminal state.")

    output_items = list(client.evals.runs.output_items.list(eval_id=eval_obj.id, run_id=run.id))
    return {
        "agent": agent,
        "evaluator": evaluator,
        "eval_id": eval_obj.id,
        "run_id": run.id,
        "status": status,
        "report_url": getattr(run, "report_url", "") or "",
        "result_counts": _jsonable(getattr(run, "result_counts", None)),
        "output_items": _jsonable(output_items),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, help="Completed local evals/run_evals.py JSON report.")
    parser.add_argument("--output", required=True, help="Path for the Foundry publication receipt JSON.")
    parser.add_argument("--project-endpoint", default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    parser.add_argument("--judge-deployment", default=os.environ.get("FOUNDRY_JUDGE_DEPLOYMENT"))
    parser.add_argument("--max-rows", required=True, type=int, help="Explicit upper bound for scored Foundry rows.")
    parser.add_argument("--approved-budget-usd", required=True, type=Decimal, help="User-approved spend ceiling recorded in the receipt; not an Azure billing cap.")
    parser.add_argument("--approve-cloud-eval", action="store_true", help="Confirm this paid Foundry registration/evaluation run.")
    args = parser.parse_args()
    if not args.approve_cloud_eval:
        parser.error("Foundry registration and judge runs require --approve-cloud-eval.")
    if not args.project_endpoint:
        parser.error("Pass --project-endpoint or configure FOUNDRY_PROJECT_ENDPOINT.")
    if not args.judge_deployment:
        parser.error("Pass --judge-deployment or configure FOUNDRY_JUDGE_DEPLOYMENT.")
    if args.max_rows <= 0:
        parser.error("--max-rows must be a positive integer.")
    if not args.approved_budget_usd.is_finite() or args.approved_budget_usd <= 0:
        parser.error("--approved-budget-usd must be a finite positive amount.")

    results_path = Path(args.results)
    output_path = Path(args.output)
    if results_path.resolve() == output_path.resolve():
        parser.error("--output must not overwrite the source evaluation report.")
    report = json.loads(results_path.read_text(encoding="utf-8"))
    dataset_path = REPO / "data" / "golden" / "dataset.jsonl"
    rows = prepare_rows(report, dataset_sha256=file_hash(dataset_path), max_rows=args.max_rows)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        agent = WORKFLOW_EVALUATORS[row["workflow"]]
        groups.setdefault(agent, []).append(row)

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=args.project_endpoint, credential=credential)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "source_report": str(results_path),
        "source_run_id": report["evaluation_run_id"],
        "source_build": report["expected_build"],
        "dataset_sha256": report["dataset_sha256"],
        "published_row_count": len(rows),
        "approved_budget_usd": str(args.approved_budget_usd),
        "budget_enforcement": "approval recorded only; Foundry/Azure billing is not capped by this CLI.",
        "target": "orchestrator",
        "publication_status": "running",
        "runs": [],
    }
    try:
        for agent, group in groups.items():
            receipt["runs"].append(run_group(
                project_client, group, agent=agent,
                judge_deployment=args.judge_deployment,
                source_run_id=report["evaluation_run_id"],
            ))
        receipt["publication_status"] = (
            "published" if all(run["status"] == "completed" for run in receipt["runs"]) else "failed"
        )
    except Exception as exc:
        receipt["publication_status"] = "failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        project_client.close()
        credential.close()

    print(json.dumps(receipt, indent=2))
    return 0 if receipt["publication_status"] == "published" else 1


if __name__ == "__main__":
    raise SystemExit(main())
