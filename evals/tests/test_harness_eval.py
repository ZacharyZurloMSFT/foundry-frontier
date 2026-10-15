import importlib.util
from pathlib import Path

from evals.harness_eval import grade, resolution_complete


def test_failed_runs_and_missing_evidence_never_pass():
    assert grade({"status": "succeeded", "output": "hello", "events": []})["harness_contract_pass"] == 0
    assert grade({"status": "failed", "output": "partial", "events": [{"kind": "run_started"}]})["harness_contract_pass"] == 0


def test_structured_resolution_is_graded_instead_of_prose():
    draft = {"order_id": "NW-40021", "customer_id": "C-8801", "reason": "Late",
             "policy_ref": "POLICY", "amount_usd": 189, "resolution_type": "refund_full", "urgency": "P2"}
    assert resolution_complete(draft)
    assert not resolution_complete({**draft, "amount_usd": float("nan")})
    assert not resolution_complete({**draft, "policy_ref": None})


def test_redteam_requires_scored_attacks():
    spec = importlib.util.spec_from_file_location(
        "redteam", Path(__file__).resolve().parents[2] / "pipelines" / "red-team" / "run_redteam.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import pytest
    with pytest.raises(ValueError):
        module._summarize({"attacks": []}, strict=True)
    rate, _ = module._summarize({"attack_details": [
        {"attack_success": False}, {"attack_success": True},
    ]}, strict=True)
    assert rate == 0.5
