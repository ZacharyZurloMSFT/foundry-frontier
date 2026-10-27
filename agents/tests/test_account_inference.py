"""Triage account inference works without non-default project Chat Completions."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.credentials import AccessToken

from agents.hosted.config import Settings
from agents.hosted.runtime import account_openai_client, build_agent
from agents.hosted.scope import RequestScope, current_scope

ENDPOINT = "https://aisvc-zurlo-northwind.openai.azure.com/openai/v1/"
RESULT = {
    "claim_id": None, "policy_id": None, "policyholder_id": None, "intent": "OTHER", "sentiment": "calm",
    "urgency": "P4", "problem_summary": "Policyholder asks a general question.",
}
CANDIDATE = "northwind-insurance-triage-ft-candidate"


def config(**extra):
    return Settings.from_env({
        "FOUNDRY_PROJECT_ENDPOINT": "https://account.services.ai.azure.com/api/projects/nw-insurance-claims",
        "AGENT_ROLE": "triage", "MODEL_DEPLOYMENT_NAME": "baseline",
        "AGENT_OPENAI_ENDPOINT": ENDPOINT, "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED": "true", **extra,
    })


@pytest.mark.parametrize("override,expected", [("", "baseline"), (CANDIDATE, CANDIDATE)])
def test_account_chat_inference_uses_identity_selected_model_and_no_storage(override, expected):
    async def run():
        calls = []

        def inference(request):
            body = json.loads(request.content)
            calls.append(body)
            assert str(request.url) == ENDPOINT + "chat/completions"
            assert request.headers["authorization"] == "Bearer unit-test-token"
            assert body["model"] == expected
            assert body["store"] is False
            assert body["response_format"]["type"] == "json_schema"
            assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
            assert body["messages"][0]["content"] == settings.prompt["instructions"]
            chunk = {"id": "chat_test", "created": 1, "model": "provider-" + expected, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {"content": json.dumps(RESULT)}, "finish_reason": "stop"}]}
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})

        settings = config(TRIAGE_MODEL_DEPLOYMENT=override)
        credential = SimpleNamespace(
            get_token=AsyncMock(return_value=AccessToken("unit-test-token", int(time.time()) + 3600)),
        )
        http = httpx.AsyncClient(transport=httpx.MockTransport(inference))
        client = account_openai_client(settings, credential, http_client=http)
        # No project client is supplied: triage must not try project-level inference.
        agent = build_agent(settings, None, account_client=client)
        token = current_scope.set(RequestScope())
        try:
            updates = [update async for update in agent.run("Classify this request.", stream=True)]
            text = "".join(c.text or "" for update in updates for c in update.contents if c.type == "text")
            assert json.loads(text) == RESULT
            assert len(calls) == 1
            assert current_scope.get().provider_model == "provider-" + expected
            assert current_scope.get().provider_response_id == "chat_test"
            assert credential.get_token.call_args.args == ("https://cognitiveservices.azure.com/.default",)
            assert settings.metadata["model_endpoint_kind"] == "account_chat_completions"
        finally:
            current_scope.reset(token)
            await client.close()
    asyncio.run(run())


def test_triage_cannot_silently_fall_back_to_project_inference():
    with pytest.raises(ValueError, match="AGENT_OPENAI_ENDPOINT"):
        build_agent(config(), None)
    with pytest.raises(ValueError, match="AGENT_OPENAI_ENDPOINT"):
        account_openai_client(config(AGENT_OPENAI_ENDPOINT=""), None)
    with pytest.raises(ValueError, match="openai/v1"):
        account_openai_client(config(AGENT_OPENAI_ENDPOINT="https://account.openai.azure.com/"), None)


def test_the_deprecated_account_endpoint_name_still_selects_the_account_endpoint():
    legacy = {"ORDERS_OPENAI_ENDPOINT": ENDPOINT}
    env = {
        "FOUNDRY_PROJECT_ENDPOINT": "https://account.services.ai.azure.com/api/projects/nw-insurance-claims",
        "ORDER_AGENT_ROLE": "triage", "MODEL_DEPLOYMENT_NAME": "baseline", **legacy,
    }
    settings = Settings.from_env(env)
    client = account_openai_client(settings, None)
    assert str(client.base_url) == ENDPOINT and settings.role == "triage"
    asyncio.run(client.close())
    with pytest.raises(ValueError, match="AGENT_OPENAI_ENDPOINT"):
        account_openai_client(Settings.from_env({**env, "ORDERS_OPENAI_ENDPOINT": ""}), None)
