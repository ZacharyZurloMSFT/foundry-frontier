import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from evals import run_evals
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


@pytest.mark.parametrize("same_workflow", [True, False])
def test_orchestrator_grades_only_session_bound_saved_draft(monkeypatch, same_workflow):
    draft = {"order_id": "NW-1", "amount_usd": 0}

    class FakeHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        @asynccontextmanager
        async def stream(self, *_args, **_kwargs):
            yield self

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for event in [{"kind": "session", "session_id": "candidate", "workflow_id": "wf-server-authoritative"},
                          {"kind": "assistant_message", "text": "I created a perfect draft."}]:
                yield json.dumps(event)

        async def get(self, url, *, params):
            assert url.endswith("/hitl")
            assert params == {"workflow_id": "wf-server-authoritative"}
            return self

        def json(self):
            return {"items": [{"workflowId": "wf-server-authoritative" if same_workflow else "wf-other",
                               "status": "pending", "payload": draft}]}

    monkeypatch.setattr(run_evals.httpx, "AsyncClient", lambda **_kwargs: FakeHTTP())
    if same_workflow:
        output = asyncio.run(run_evals.run_workflow({"workflow": "resolution_draft", "input": "test"}))
        assert json.loads(output) == draft
    else:
        with pytest.raises(RuntimeError, match="bound to this session"):
            asyncio.run(run_evals.run_workflow({"workflow": "resolution_draft", "input": "test"}))
