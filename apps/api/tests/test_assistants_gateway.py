"""Embedded assistants preserve main's gateway routing, identity and no-fallback contract."""
import json

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import foundry_client, model_gateway
from app.assistants import extractive, runtime
from app.model_gateway import GatewayConfigError
from test_assistants_help import QUOTE, REPORT, Search, helping  # noqa: F401
from test_assistants_routes import auth, conversations, events_of, post  # noqa: F401
from test_business_api import api, identity  # noqa: F401
from test_gateway_routing import (  # noqa: F401
    ENDPOINT,
    PROVIDER,
    SCOPE,
    assert_gateway_call,
    fresh_gateway_clients,
    gateway,
)


@pytest.fixture
def routed(helping, monkeypatch):  # noqa: F811
    helping.ai_gateway_model_endpoint = ENDPOINT
    helping.ai_gateway_model_provider = PROVIDER
    helping.ai_gateway_model_aliases = ""
    helping.ai_gateway_scope = SCOPE
    helping.ai_gateway_required = True
    # A configured gateway needs no direct Foundry project to serve these model calls.
    helping.foundry_project_endpoint = ""

    def forbidden(*_args, **_kwargs):
        pytest.fail("An assistant attempted direct Foundry access with the AI Gateway selected.")

    monkeypatch.setattr(foundry_client, "_project_client", forbidden)
    monkeypatch.setattr(extractive, "_http", Search(REPORT))
    return helping


def response_body(surface):
    content = ({"answer": "Your records are available in the portal.", "citations": [], "human_review": False}
               if surface == "concierge" else
               {"selections": [{"source_id": REPORT["id"], "quote": QUOTE}], "unknown": False})
    return {
        "id": "resp-gateway", "object": "response", "created_at": 1, "status": "completed",
        "model": "upstream-model", "output": [{
            "id": "msg-gateway", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": json.dumps(content), "annotations": []}],
        }],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["concierge", "help_center"])
async def test_assistants_use_gateway_alias_and_entra_without_closing_shared_client(
        api, routed, conversations, gateway, monkeypatch, surface):  # noqa: F811
    client, _, identity_ = api
    exporter, provider = InMemorySpanExporter(), TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(runtime, "tracer", provider.get_tracer("assistant-gateway-test"))
    gateway.reply(lambda _: httpx.Response(200, json=response_body(surface)))
    shared = model_gateway.get_gateway_client(routed)
    status = (await client.get("/api/v1/assistants/status", headers=auth(identity_))).json()
    assert all(item["available"] for item in status["surfaces"])
    for _ in range(2):
        events = events_of(await post(client, identity_, {"message": "How do I report a loss?"}, surface=surface))
        assert events[-1]["provenance"]["model"] == "gpt-test"
        assert [event["kind"] for event in events] == [
            "session", "assistant_delta", "citations" if surface == "concierge" else "sources", "done"]
        assert not shared.is_closed()
        assert model_gateway.get_gateway_client(routed) is shared
    alias = f"{PROVIDER}/gpt-test"
    assert len(gateway.requests) == len(conversations.items) == 2
    for request in gateway.requests:
        assert_gateway_call(request, "responses", alias)
        assert json.loads(request.content)["store"] is False
    assert gateway.credential.scopes == [(SCOPE,)]
    spans = [span for span in exporter.get_finished_spans() if span.name == "assistant.turn"]
    assert len(spans) == 2
    assert all(span.attributes["gateway.route"] == "gateway" and
               span.attributes["gateway.model_alias"] == alias for span in spans)
    assert all(item["meta"]["model"] == "gpt-test" for item in conversations.items)
    provider.shutdown()


INVALID = [
    pytest.param({"ai_gateway_model_endpoint": ""}, id="required-gateway-missing"),
    pytest.param({"ai_gateway_model_endpoint": "http://invalid.test/openai/v1"}, id="insecure-endpoint"),
    pytest.param({"ai_gateway_model_endpoint": "https://invalid.test/openai/v1?secret=hidden"}, id="endpoint-query"),
    pytest.param({"ai_gateway_required": False, "ai_gateway_model_endpoint": "http://invalid.test/openai/v1"},
                 id="optional-but-selected-invalid-gateway"),
    pytest.param({"ai_gateway_model_aliases": "not-json"}, id="malformed-aliases"),
    pytest.param({"ai_gateway_model_provider": "", "ai_gateway_model_aliases": '{"another-model":"provider/other"}'},
                 id="assistant-alias-missing"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["concierge", "help_center"])
@pytest.mark.parametrize("changes", INVALID)
async def test_invalid_gateway_disables_each_assistant_and_never_falls_back(
        api, routed, conversations, gateway, surface, changes):  # noqa: F811
    client, _, identity_ = api
    for key, value in changes.items():
        setattr(routed, key, value)
    status = (await client.get("/api/v1/assistants/status", headers=auth(identity_))).json()
    selected = next(item for item in status["surfaces"] if item["surface"] == surface)
    assert selected["available"] is False and "Gateway" in selected["reason"]
    response = await post(client, identity_, {"message": "How do I report a loss?"}, surface=surface)
    assert response.status_code == 503 and response.json()["error"]["code"] == "assistant_unavailable"
    assert "hidden" not in response.text and not gateway.requests and not conversations.items
    # Enforce the same boundary even if a future caller skips status/route validation.
    with pytest.raises(GatewayConfigError):
        async with foundry_client.model_client(routed, routed.assistant_model):
            pytest.fail("Invalid gateway configuration yielded a model client")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["concierge", "help_center"])
@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
async def test_gateway_rejection_fails_closed_without_direct_retry_or_private_history(
        api, routed, conversations, gateway, surface, status):  # noqa: F811
    client, _, identity_ = api
    gateway.reply(lambda _: httpx.Response(status, json={"error": {"message": "sensitive-provider-detail"}}))
    model_gateway.get_gateway_client(routed).max_retries = 0
    response = await post(client, identity_, {"message": "How do I report a loss?"}, surface=surface)
    events = events_of(response)
    assert [event["kind"] for event in events] == ["session", "error", "done"]
    assert events[1]["code"] == "assistant_failed"
    assert "sensitive-provider-detail" not in response.text
    assert len(gateway.requests) == 1 and not conversations.items
