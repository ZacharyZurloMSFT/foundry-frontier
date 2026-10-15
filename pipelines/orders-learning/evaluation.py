"""Deterministic scoring of actual saved or explicitly requested model outputs."""
from __future__ import annotations

import json
import math
import uuid
from pathlib import Path

from curation import (
    PROJECT_ENDPOINT, file_hash, labels_valid, now, read_json, read_rows,
    verify_approval, verify_manifest, write_json,
)

METRICS = ("schema_accuracy", "intent_accuracy", "entity_accuracy", "urgency_accuracy",
           "sentiment_accuracy", "joint_accuracy", "urgent_recall", "p1_recall")


def prediction_provenance(manifest_path: Path, manifest: dict, deployment: str, run_id: str) -> dict:
    return {"deployment": deployment, "prediction_run_id": run_id,
            "manifest_sha256": file_hash(manifest_path), "dataset_version": manifest["version"],
            "frozen_test_sha256": manifest["frozen_test_sha256"], "prompt_sha256": manifest["prompt_sha256"]}


def validate_prediction_provenance(manifest_path: Path, manifest: dict, predictions: list[dict],
                                   deployment: str | None = None) -> tuple[str, str]:
    if not predictions:
        raise ValueError("Predictions require recorded deployment/release/prompt/run provenance")
    deployment = deployment or predictions[0].get("deployment")
    run_id = predictions[0].get("prediction_run_id")
    if not isinstance(deployment, str) or not deployment.strip() or not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("Prediction deployment and prediction_run_id are required")
    expected = prediction_provenance(manifest_path, manifest, deployment, run_id)
    for prediction in predictions:
        if any(prediction.get(key) != value for key, value in expected.items()):
            raise ValueError("Prediction provenance does not match requested deployment, release, frozen test, prompt or run")
    return deployment, run_id


def grade(expected: dict, response: str) -> dict:
    try:
        actual = json.loads(response)
    except (ValueError, TypeError):
        actual = None
    valid = labels_valid(actual)
    actual = actual if valid else {}
    result = {
        "schema_accuracy": float(valid),
        "intent_accuracy": float(valid and actual["intent"] == expected["intent"]),
        "entity_accuracy": float(valid and all(actual[k] == expected[k] for k in ("order_id", "customer_hint"))),
        "urgency_accuracy": float(valid and actual["urgency"] == expected["urgency"]),
        "sentiment_accuracy": float(valid and actual["sentiment"] == expected["sentiment"]),
    }
    result["joint_accuracy"] = float(all(result.values()))
    if expected["urgency"] in ("P1", "P2"):
        result["urgent_recall"] = float(valid and actual["urgency"] in ("P1", "P2"))
    if expected["urgency"] == "P1":
        result["p1_recall"] = float(valid and actual["urgency"] == "P1")
    return result


def evaluate(manifest_path: Path, predictions_path: Path, output: Path,
             deployment: str, baseline_path: Path | None = None) -> dict:
    manifest = verify_manifest(manifest_path)
    test = read_rows(manifest_path.parent / "test.jsonl")
    predictions = read_rows(predictions_path)
    deployment, run_id = validate_prediction_provenance(manifest_path, manifest, predictions, deployment)
    ids = [row.get("example_id") for row in predictions]
    test_ids = {row["example_id"] for row in test}
    if len(set(ids)) != len(ids) or set(ids) - test_ids:
        raise ValueError("Duplicate or unknown prediction example_id")
    by_id = {row["example_id"]: row for row in predictions}
    results = []
    for row in test:
        prediction = by_id.get(row["example_id"], {})
        response = prediction.get("response")
        error = prediction.get("error")
        if not isinstance(response, str):
            error = error or "missing_string_response"
            response = ""
        scores = grade(row["expected"], response if not error else "")
        results.append({"example_id": row["example_id"], "family": row["family"],
                        "response": response, "error": error, "scores": scores,
                        "usage": prediction.get("usage")})
    metrics = {}
    denominators = {}
    for metric in METRICS:
        values = [row["scores"][metric] for row in results if metric in row["scores"]]
        denominators[metric] = len(values)
        metrics[metric] = round(sum(values) / len(values), 6) if values else None
    regressions = []
    regression_status = "unavailable"
    baseline_sha = None
    if baseline_path:
        baseline = read_json(baseline_path)
        if (baseline.get("frozen_test_sha256") != manifest["frozen_test_sha256"]
                or baseline.get("manifest_sha256") != file_hash(manifest_path)
                or baseline.get("prompt_sha256") != manifest["prompt_sha256"]
                or baseline.get("provenance_verified") is not True
                or baseline.get("state") != "completed"
                or baseline.get("denominators") != denominators):
            raise ValueError("Baseline must be a completed run on the exact same frozen test")
        if (baseline.get("predictions_sha256") == file_hash(predictions_path)
                or baseline.get("prediction_run_id") == run_id):
            raise ValueError("Baseline and candidate cannot reuse the same predictions or inference run")
        if baseline.get("deployment") == deployment or not baseline.get("deployment"):
            raise ValueError("Baseline and candidate must identify distinct recorded deployments")
        baseline_sha = file_hash(baseline_path)
        for metric in METRICS:
            old, new = baseline["metrics"].get(metric), metrics[metric]
            if (isinstance(old, bool) or not isinstance(old, (int, float))
                    or not math.isfinite(old) or not 0 <= old <= 1 or new is None):
                raise ValueError(f"Baseline/test lacks coverage for {metric}")
            if new + 0.05 < old:
                regressions.append(metric)
        regression_status = "regressed" if regressions else "compared"
    gates = {
        "all_predictions_present": len(predictions) == len(test),
        "no_prediction_errors": all(not row["error"] for row in results),
        "schema": metrics["schema_accuracy"] == 1.0,
        "intent": metrics["intent_accuracy"] is not None and metrics["intent_accuracy"] >= 0.9,
        "entities": metrics["entity_accuracy"] is not None and metrics["entity_accuracy"] >= 0.9,
        "urgency": metrics["urgency_accuracy"] is not None and metrics["urgency_accuracy"] >= 0.9,
        "urgent_recall": metrics["urgent_recall"] == 1.0,
        "p1_recall": metrics["p1_recall"] == 1.0,
        "no_regressions": not regressions,
    }
    report = {
        "schema_version": 1, "state": "completed" if gates["all_predictions_present"] and gates["no_prediction_errors"] else "incomplete",
        "created_at": now(), "deployment": deployment, "manifest_sha256": file_hash(manifest_path),
        "dataset_version": manifest["version"], "frozen_test_sha256": manifest["frozen_test_sha256"],
        "prompt_sha256": manifest["prompt_sha256"], "provenance_verified": True, "prediction_run_id": run_id,
        "predictions_sha256": file_hash(predictions_path), "baseline_sha256": baseline_sha,
        "metrics": metrics, "denominators": denominators, "gates": gates, "rows": results,
        "regression_status": regression_status, "regressions": regressions,
        "passed": all(gates.values()) and regression_status == "compared",
        "promotion_note": "Not an approval. All four existing eval tiers and a human deployment approval remain required.",
    }
    write_json(output, report)
    return report


def predict_live(manifest_path: Path, output: Path, deployment: str, confirmation: str) -> Path:
    verify_approval(manifest_path)
    manifest = verify_manifest(manifest_path)
    provenance = prediction_provenance(manifest_path, manifest, deployment, str(uuid.uuid4()))
    if confirmation != file_hash(manifest_path) or not deployment.strip():
        raise ValueError("Live inference requires exact manifest SHA confirmation and a deployment")
    # Azure CLI credentials deliberately exclude environment keys and server managed identities.
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential

    prompt = (manifest_path.parent / "triage-instructions.txt").read_text(encoding="utf-8")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with AzureCliCredential() as credential, AIProjectClient(PROJECT_ENDPOINT, credential=credential) as project:
        with project.get_openai_client(max_retries=0) as client, output.open("x", encoding="utf-8") as stream:
            for row in read_rows(manifest_path.parent / "test.jsonl"):
                prediction = {"example_id": row["example_id"], **provenance}
                try:
                    response = client.chat.completions.create(
                        model=deployment,
                        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": row["input"]}],
                        max_completion_tokens=800,
                    )
                    prediction["response"] = response.choices[0].message.content or ""
                    prediction["provider_response_id"] = response.id
                    prediction["served_model"] = response.model
                    prediction["usage"] = response.usage.model_dump() if response.usage else None
                    if response.choices[0].finish_reason != "stop":
                        prediction["error"] = "non_stop_finish:" + str(response.choices[0].finish_reason)
                except Exception as exc:
                    prediction.update(response="", error=type(exc).__name__)
                stream.write(json.dumps(prediction, ensure_ascii=False) + "\n")
                stream.flush()
    return output
