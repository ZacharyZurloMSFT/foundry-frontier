import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
module = importlib.import_module("run-orders-candidate-demo-eval")


@pytest.fixture
def files(tmp_path, monkeypatch):
    data = "\n".join(json.dumps({"example_id": str(i), "query": "Where is NW-40021?"}) for i in range(100)).encode()
    (tmp_path / "verified-native-triage-evaluation.jsonl").write_bytes(data)
    monkeypatch.setattr(module, "SNAPSHOT", hashlib.sha256(data).hexdigest())
    (tmp_path / "orders-candidate-deployment.json").write_text(json.dumps({
        "state": "Succeeded", "verified_training": {"fine_tuned_model": module.MODEL},
        "budget": {"candidate_reservation_usd": "2.50"},
    }))
    (tmp_path / "orders-native-baseline-eval.json").write_text(json.dumps({
        "plan": {"jobs": {"triage": {"create": {
            "name": "baseline", "testing_criteria": [{"evaluator_name": "builtin.f1_score"}],
        }}}},
    }))
    return tmp_path


def test_full_existing_dataset_has_explicit_custom_target_and_cap(files):
    plan = module.build_plan(files)
    source = plan["jobs"]["triage"]["run"]["data_source"]
    assert source["source"] == {"type": "file_id", "id": module.DATASET}
    assert source["target"] == {"type": "azure_ai_model", "model": module.DEPLOYMENT,
                                 "sampling_params": {"max_completion_tokens": 512, "temperature": 0}}
    assert plan["jobs"]["triage"]["selected_rows"] == 100
    assert float(plan["token_upper_bound_usd"]) <= 0.18
    assert "ground_truth" not in json.dumps(source)
    assert "file_content" not in json.dumps(source)


def test_changed_dataset_is_rejected(files):
    (files / "verified-native-triage-evaluation.jsonl").write_text("{}")
    with pytest.raises(ValueError, match="snapshot changed"):
        module.build_plan(files)


def test_unverified_model_is_rejected(files):
    path = files / "orders-candidate-deployment.json"
    receipt = json.loads(path.read_text())
    receipt["verified_training"]["fine_tuned_model"] = "gpt-4o"
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="Exact approved"):
        module.build_plan(files)


def test_uncertain_eval_submission_never_repeats(files, monkeypatch):
    plan = module.build_plan(files)
    receipt = {"plan": plan, "plan_sha256": module.native.digest(plan),
               "jobs": {"triage": {"eval_submission_started": True}}}
    create = Mock()
    client = SimpleNamespace(evals=SimpleNamespace(list=Mock(), create=create))
    monkeypatch.setattr(module.native, "pages", lambda *args, **kwargs: [])
    with pytest.raises(RuntimeError, match="never repeat"):
        module.execute(client, receipt, Mock())
    create.assert_not_called()
