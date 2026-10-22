"""Documented Foundry/OpenAI string graders over actual saved triage predictions."""
from __future__ import annotations

import json
from pathlib import Path

from curation import (
    PROJECT_ENDPOINT, canonical, file_hash, labels_valid, now, read_rows,
    verify_approval, verify_manifest, write_json,
)
from evaluation import validate_prediction_provenance


def native_plan(manifest_path: Path, predictions_path: Path) -> dict:
    manifest = verify_manifest(manifest_path)
    test = read_rows(manifest_path.parent / "test.jsonl")
    predictions = read_rows(predictions_path)
    deployment, run_id = validate_prediction_provenance(manifest_path, manifest, predictions)
    by_id = {row["example_id"]: row for row in predictions}
    if len(by_id) != len(predictions) or set(by_id) != {row["example_id"] for row in test}:
        raise ValueError("Native evaluation requires exactly one saved prediction per frozen-test example")
    fields = ("schema", "intent", "order_id", "customer_hint", "sentiment", "urgency")
    items = []
    for row in test:
        prediction = by_id[row["example_id"]]
        response = prediction.get("response", "")
        try:
            actual = json.loads(response) if not prediction.get("error") else None
        except (ValueError, TypeError):
            actual = None
        valid = labels_valid(actual)
        item = {"example_id": row["example_id"], "response": response,
                "expected_schema": "true", "actual_schema": "true" if valid else "false"}
        for field in fields[1:]:
            item["expected_" + field] = canonical(row["expected"][field])
            item["actual_" + field] = canonical(actual[field]) if valid else "__invalid_response__"
        items.append({"item": item})
    properties = {"example_id": {"type": "string"}, "response": {"type": "string"}}
    properties.update({f"{prefix}_{field}": {"type": "string"}
                       for prefix in ("expected", "actual") for field in fields})
    return {
        "schema_version": 1, "state": "dry_run", "created_at": now(),
        "manifest_sha256": file_hash(manifest_path), "predictions_sha256": file_hash(predictions_path),
        "deployment": deployment, "prediction_run_id": run_id, "prompt_sha256": manifest["prompt_sha256"],
        "create": {
            "name": "northwind-orders-triage-" + manifest["version"],
            "data_source_config": {"type": "custom", "item_schema": {
                "type": "object", "properties": properties, "required": list(properties)},
                "include_sample_schema": False},
            "testing_criteria": [
                {"type": "string_check", "name": field + "_exact", "operation": "eq",
                 "input": "{{item.actual_" + field + "}}", "reference": "{{item.expected_" + field + "}}"}
                for field in fields
            ],
        },
        "run": {"name": "frozen-test-" + manifest["version"],
                "data_source": {"type": "jsonl", "source": {"type": "file_content", "content": items}}},
        "note": "These string graders are supplemental; local schema/urgent denominators and the repository's eval gates remain authoritative.",
    }


def run_native(manifest_path: Path, predictions_path: Path, output: Path,
               execute: bool, confirmation: str | None) -> dict:
    plan = native_plan(manifest_path, predictions_path)
    if not execute:
        write_json(output, plan)
        return plan
    verify_approval(manifest_path)
    if confirmation != file_hash(manifest_path):
        raise ValueError("Native evaluation upload/job requires exact approved manifest SHA confirmation")
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential

    receipt = {key: value for key, value in plan.items() if key not in ("create", "run")}
    receipt.update(state="submission_started", eval_id=None, run_id=None)
    write_json(output, receipt)
    with AzureCliCredential() as credential, AIProjectClient(PROJECT_ENDPOINT, credential=credential) as project:
        with project.get_openai_client(max_retries=0) as client:
            created = client.evals.create(**plan["create"])
            receipt["eval_id"] = created.id
            write_json(output, receipt, exclusive=False)
            run = client.evals.runs.create(eval_id=created.id, **plan["run"])
            receipt.update(state="submitted", run_id=run.id, service_status=run.status, report_url=run.report_url)
            write_json(output, receipt, exclusive=False)
    return receipt
