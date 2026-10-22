import json

from evals.evaluators.rubric_evaluator import judge_model_config_from_env
from evals.evaluators.work_order_completeness import evaluate


def test_judge_requires_configuration_and_rejects_known_incompatible_models(monkeypatch):
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://unit-test.invalid")
    for key in ("FOUNDRY_JUDGE_DEPLOYMENT", "FOUNDRY_PROMPT_AGENT_DEPLOYMENT", "FOUNDRY_WORKER_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)
    assert judge_model_config_from_env() is None
    monkeypatch.setenv("FOUNDRY_WORKER_DEPLOYMENT", "gpt-5-mini")
    assert judge_model_config_from_env() is None
    monkeypatch.setenv("FOUNDRY_JUDGE_DEPLOYMENT", "gpt-4o")
    assert judge_model_config_from_env()["azure_deployment"] == "gpt-4o"


def test_zero_cost_resolution_is_complete_but_boolean_is_not():
    payload = {"order_id": "NW-1", "customer_id": "C-1", "resolution_type": "cancel",
               "amount_usd": 0, "reason": "Requested", "policy_ref": "policy-id", "urgency": "P3"}
    assert evaluate(response=json.dumps(payload))["work_order_completeness"] == 1
    assert not evaluate(response=json.dumps({**payload, "amount_usd": False}))["plausible"]
