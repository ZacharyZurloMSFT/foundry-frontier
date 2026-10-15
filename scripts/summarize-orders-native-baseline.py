"""Score actual native triage outputs locally, without additional inference."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipelines" / "orders-learning"))
from evaluation import grade


def summarize(receipt: dict, expected_rows: list[dict]) -> dict:
    job = receipt["jobs"]["triage"]
    if receipt["state"] != "completed" or job.get("readback_verified") is not True:
        raise ValueError("Require a completed, verified native evaluation")
    expected = {row["example_id"]: row for row in expected_rows}
    if len(expected) != len(expected_rows):
        raise ValueError("Frozen dataset contains duplicate example IDs")
    results = []
    seen = set()
    totals = Counter()
    denominators = Counter()
    intent_totals = Counter()
    intent_matches = Counter()
    diagnostic = []
    tokens = Counter()
    latencies = []
    mismatches = []
    for output in job["output_items"]:
        source = output["datasource_item"]
        example_id = source["example_id"]
        if example_id in seen or example_id not in expected:
            raise ValueError("Duplicate or unknown native output example ID")
        seen.add(example_id)
        reference = expected[example_id]
        if source["query"] != reference["query"] or source["ground_truth"] != reference["ground_truth"]:
            raise ValueError("Native reference differs from the frozen dataset")
        raw = source.get("sample.output_text")
        if not isinstance(raw, str):
            raise ValueError("Native record has no actual generated output text")
        if output.get("status") != "completed" or (output.get("sample") or {}).get("error"):
            raise ValueError("Native output reports an inference error")
        labels = json.loads(reference["ground_truth"])
        scores = grade(labels, raw)
        for metric, value in scores.items():
            totals[metric] += value
            denominators[metric] += 1
        intent_totals[labels["intent"]] += 1
        intent_matches[labels["intent"]] += scores["intent_accuracy"]
        if scores["joint_accuracy"] != 1:
            actual = json.loads(raw) if scores["schema_accuracy"] == 1 else {"raw_output": raw}
            mismatches.append({"example_id": example_id, "expected": labels, "actual": actual,
                               "scores": scores})
        results.append({"example_id": example_id, "family": reference["family"], "scores": scores})
        usage = (output.get("sample") or {}).get("usage") or {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            count = usage.get(name)
            if type(count) is not int or count < 0:
                raise ValueError("Native output is missing measured token usage")
            tokens[name] += count
        latency = (output.get("sample") or {}).get("latency_ms")
        if isinstance(latency, (int, float)) and latency >= 0:
            latencies.append(latency)
        for result in output["results"]:
            if result["name"] == "diagnostic-token-f1":
                diagnostic.append(result["score"])
    if seen != set(expected) or len(diagnostic) != len(expected):
        raise ValueError("Native output coverage is incomplete")
    pricing = receipt["plan"]["model"]["pricing"]
    token_cost = (tokens["prompt_tokens"] * float(pricing["input_usd_per_million"])
                  + tokens["completion_tokens"] * float(pricing["output_usd_per_million"])) / 1_000_000
    sandbox_rate = 0.5 * 0.0994 + 1 * 0.0118
    compute_bound = len(expected) * float(receipt["plan"]["jobs"]["triage"]["bounds"]["non_model_usd_per_row"])
    return {
        "schema_version": 1, "eval_id": job["eval_id"], "run_id": job["run_id"],
        "target": receipt["plan"]["jobs"]["triage"]["target"], "rows": len(expected),
        "independent_scenario_families": len({row["family"] for row in expected_rows}),
        "reference_status": "synthetic labels authorised for demo, not individually human-reviewed",
        "source": "Actual completed native evaluation output items, not ground truth copied as predictions.",
        "metrics": {name: totals[name] / denominators[name] for name in totals},
        "denominators": dict(denominators),
        "per_intent_accuracy": {name: intent_matches[name] / intent_totals[name] for name in intent_totals},
        "diagnostic_token_f1_mean": sum(diagnostic) / len(diagnostic),
        "native_diagnostic_counts": job["native_run"]["result_counts"],
        "usage": dict(tokens),
        "measured_latency_total_seconds": sum(latencies) / 1000,
        "estimated_token_cost_usd": token_cost,
        "runtime_pricing_usd_per_sandbox_hour": sandbox_rate,
        "compute_cost_upper_bound_usd": compute_bound,
        "baseline_cost_reservation_reconciled_upper_usd": token_cost + compute_bound,
        "cost_note": "Measured token usage at public retail rates, not an invoice. Sandbox idle/runtime charge remains conservatively reserved; other service charges are not assumed free.",
        "mismatches": mismatches, "per_example_scores": results,
        "metric_note": "Joint accuracy compares five exact labels plus valid canonical JSON. It does not score summary semantics. Token F1 is lexical and is not classification accuracy.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    raw = args.dataset.read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt["plan"]["jobs"]["triage"]["snapshot_sha256"]:
        raise ValueError("Frozen dataset hash differs from the recorded native run")
    rows = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    result = summarize(receipt, rows)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ("mismatches", "per_example_scores")}, indent=2))


if __name__ == "__main__":
    main()
