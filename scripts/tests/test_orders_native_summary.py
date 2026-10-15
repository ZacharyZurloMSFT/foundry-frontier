import copy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "orders_native_summary", Path(__file__).parents[1] / "summarize-orders-native-baseline.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def fixture():
    labels = {"order_id": "NW-83001", "customer_hint": None, "intent": "ORDER_STATUS",
              "sentiment": "calm", "urgency": "P3", "problem_summary": "Customer asks for status."}
    row = {"example_id": "fixture", "family": "fixture-family", "query": "Where is NW-83001?",
           "ground_truth": json.dumps(labels)}
    item = {"datasource_item": {**row, "sample.output_text": json.dumps(labels)},
            "sample": {"error": None, "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                               "total_tokens": 150}, "latency_ms": 1000},
            "results": [{"name": "diagnostic-token-f1", "score": 0.2}], "status": "completed"}
    receipt = {"state": "completed", "jobs": {"triage": {
        "readback_verified": True, "eval_id": "eval-fixture", "run_id": "run-fixture",
        "output_items": [item], "native_run": {"result_counts": {"total": 1, "passed": 0, "failed": 1}},
    }}, "plan": {"model": {"pricing": {"input_usd_per_million": "2.5", "output_usd_per_million": "10"}},
                "jobs": {"triage": {"target": {"name": "northwind-triage", "version": "2"},
                                    "bounds": {"non_model_usd_per_row": "0.025625"}}}}}
    return receipt, [row]


def test_low_lexical_f1_is_not_mistaken_for_wrong_labels():
    receipt, rows = fixture()
    result = module.summarize(receipt, rows)
    assert result["metrics"]["joint_accuracy"] == 1
    assert result["diagnostic_token_f1_mean"] == 0.2
    assert result["estimated_token_cost_usd"] == 0.00075
    assert result["compute_cost_upper_bound_usd"] == 0.025625


def test_invalid_generated_json_is_counted_as_a_failure_not_replaced():
    receipt, rows = fixture()
    receipt["jobs"]["triage"]["output_items"][0]["datasource_item"]["sample.output_text"] = "not JSON"
    result = module.summarize(receipt, rows)
    assert result["metrics"]["schema_accuracy"] == 0
    assert result["metrics"]["intent_accuracy"] == 0
    assert result["mismatches"][0]["actual"] == {"raw_output": "not JSON"}


def test_duplicate_native_outputs_and_changed_references_are_rejected():
    receipt, rows = fixture()
    duplicate = copy.deepcopy(receipt)
    duplicate["jobs"]["triage"]["output_items"] *= 2
    with pytest.raises(ValueError, match="Duplicate"):
        module.summarize(duplicate, rows)
    changed = copy.deepcopy(receipt)
    changed["jobs"]["triage"]["output_items"][0]["datasource_item"]["query"] = "different"
    with pytest.raises(ValueError, match="reference differs"):
        module.summarize(changed, rows)
