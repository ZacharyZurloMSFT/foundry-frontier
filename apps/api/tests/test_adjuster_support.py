import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import adjuster_support as support
from fastapi import FastAPI, HTTPException
from azure.core.exceptions import AzureError
from azure.core.credentials import AccessToken
from azure.ai.projects.aio import AIProjectClient
from openai import NotFoundError

load_support_history = support.load_support_history


@pytest.fixture
def config(monkeypatch):
    settings = SimpleNamespace(
        search_endpoint="https://search.unit.test",
        foundry_project_endpoint="https://foundry.unit.test/api/projects/northwind-orders",
        cosmos_endpoint="https://cosmos.unit.test", adjuster_support_enabled=True,
        adjuster_support_index=support.INDEX, adjuster_support_search_auth="entra_id",
        adjuster_support_timeout=10, adjuster_support_concurrency=2,
    )
    monkeypatch.setattr(support, "get_settings", lambda: settings)
    monkeypatch.setattr(support, "_active", 0)
    monkeypatch.setattr(support, "append_message", AsyncMock())
    monkeypatch.setattr(support, "load_support_history", AsyncMock(return_value=[]))
    return settings


@pytest.fixture
def evidence():
    return [{"id": "ADJ-TEST-001", "title": "Fictional evidence procedure", "version": "1",
             "source_uri": "urn:northwind:synthetic:adjuster-support:ADJ-TEST-001:v1",
             "effective_from": "2026-01-01", "category": "adjuster",
             "content": "Missing evidence requires human review. Never automatically deny a claim."}]


def test_canonical_prompt_and_explicit_configuration(config):
    assert support.availability()["state"] == "configured"
    assert "Never use model memory" in support.prompt()
    config.adjuster_support_index = "nw-other-index"
    assert support.availability()["state"] == "unavailable"
    config.adjuster_support_index = support.INDEX
    config.adjuster_support_search_auth = "query_key"
    assert support.availability()["state"] == "unavailable"
    config.adjuster_support_query_key = "explicit-memory-only"
    assert support.availability()["state"] == "configured"
    assert support.availability()["business_domain"] == "insurance"


def test_citations_and_quotes_are_server_validated(evidence):
    selection = support.EvidenceAnswer(selections=[support.Selection(source_id="ADJ-TEST-001",
                                                quote="Missing evidence requires human review.")], unknown=False)
    text, citations = support.render_answer(selection, evidence)
    assert evidence[0]["source_uri"] in text and "version 1" in text
    assert citations[0]["title"] == evidence[0]["title"]
    for bad in [
        support.EvidenceAnswer(selections=[support.Selection(source_id="invented", quote="facts")], unknown=False),
        support.EvidenceAnswer(selections=[support.Selection(source_id="ADJ-TEST-001", quote="Coverage is approved")], unknown=False),
        support.EvidenceAnswer(selections=[], unknown=False),
    ]:
        with pytest.raises(RuntimeError):
            support.render_answer(bad, evidence)
    assert support.render_answer(support.EvidenceAnswer(selections=[], unknown=True), evidence) == (support.UNKNOWN, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty", "retrieval_error", "model_error", "success"])
async def test_rag_no_hit_and_errors_never_fall_back(config, evidence, monkeypatch, mode):
    retrieve = AsyncMock(return_value=evidence if mode != "empty" else [])
    model = AsyncMock(return_value=support.EvidenceAnswer(
        selections=[support.Selection(source_id="ADJ-TEST-001", quote="Missing evidence requires human review.")],
        unknown=False))
    if mode == "retrieval_error":
        retrieve.side_effect = RuntimeError("Bearer secret-must-not-escape")
    if mode == "model_error":
        model.side_effect = RuntimeError("invalid model")
    monkeypatch.setattr(support, "retrieve", retrieve)
    monkeypatch.setattr(support, "select_evidence", model)
    events = [e async for e in support.run_support_turn("adjuster-gpt-5-4-unit", "Missing evidence?")]
    assert events[0]["kind"] == "session" and events[-1]["kind"] == "done"
    if mode in ("model_error", "retrieval_error"):
        assert not any(e["kind"] == "assistant_delta" for e in events)
        assert events[-2]["kind"] == "error" and "secret-must-not-escape" not in json.dumps(events)
    elif mode == "empty":
        assert events[1]["text"] == support.UNKNOWN
        model.assert_not_awaited()
    else:
        assert events[2]["citations"][0]["source_uri"] == evidence[0]["source_uri"]
        assert support.append_message.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["entra_id", "query_key"])
async def test_search_is_isolated_and_auth_refreshes_without_fallback(config, evidence, monkeypatch, auth):
    config.adjuster_support_search_auth = auth
    config.adjuster_support_query_key = "explicit-readonly-key"
    credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="fresh")))
    monkeypatch.setattr(support, "_async_credential", lambda: credential)
    requests = []
    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"value": [{**evidence[0], "@search.score": 2.0}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(support.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    for _ in range(2):
        assert (await support.retrieve("human review"))[0]["id"] == evidence[0]["id"]
    assert credential.get_token.await_count == (2 if auth == "entra_id" else 0)
    assert support.INDEX in str(requests[0].url) and "nw-other-index" not in str(requests[0].url)
    assert "publisher eq" in json.loads(requests[0].content)["filter"]
    assert "category eq 'adjuster'" in json.loads(requests[0].content)["filter"]
    assert ("api-key" in requests[0].headers) is (auth == "query_key")
    assert ("authorization" in requests[0].headers) is (auth == "entra_id")


@pytest.mark.asyncio
async def test_failed_entra_search_never_uses_legacy_key(config, monkeypatch):
    config.azure_search_key = "legacy-key-not-authorized-for-support"
    credential = SimpleNamespace(get_token=AsyncMock(side_effect=RuntimeError("Identity unavailable")))
    monkeypatch.setattr(support, "_async_credential", lambda: credential)
    monkeypatch.setattr(support.httpx, "AsyncClient", lambda **_: pytest.fail("No key-auth fallback is allowed"))
    with pytest.raises(RuntimeError, match="Identity unavailable"):
        await support.retrieve("evidence")


@pytest.mark.asyncio
async def test_history_loads_latest_bounded_messages_only(config, monkeypatch):
    captured = {}
    class Container:
        def query_items(self, **kwargs):
            captured.update(kwargs)
            async def rows():
                yield {"role": "assistant", "content": "latest"}
                yield {"role": "user", "content": "x" * 4000}
            return rows()
    monkeypatch.setattr(support, "_container", AsyncMock(return_value=Container()))
    rows = await load_support_history("adjuster-gpt-5-4-unit")
    assert len(rows[0]["content"]) == 3000 and rows[-1]["content"] == "latest"
    assert "TOP 12" in captured["query"] and "DESC" in captured["query"]
    assert captured["partition_key"] == "adjuster-gpt-5-4-unit"


@pytest.mark.asyncio
async def test_model_client_uses_deployment_not_a_native_agent(config, evidence, monkeypatch):
    captured = {}
    class Context:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get_openai_client(self, **kwargs):
            assert kwargs == {}
            return self
        @property
        def responses(self):
            return self
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(status="completed", output_text='{"selections":[],"unknown":true}')
    monkeypatch.setattr(support, "_project_client", Context)
    result = await support.select_evidence("question", evidence, [], "gpt-5.4")
    assert result.unknown
    assert captured["model"] == "gpt-5.4" and captured["store"] is False
    assert "tools" not in captured and "agent_reference" not in captured
    assert evidence[0]["source_uri"] in captured["input"][1]["content"][0]["text"]


@pytest.mark.asyncio
async def test_actual_sdk_wire_uses_typed_responses_messages_and_preserves_grounding(config, evidence, monkeypatch):
    requests = []
    question = "When does missing evidence require review?"
    history = [{"role": "user", "content": f"Prior question {i}"} for i in range(20)]
    selected = {"selections": [{"source_id": evidence[0]["id"],
                                "quote": "Missing evidence requires human review."}], "unknown": False}
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "resp_offline_contract", "object": "response", "created_at": 1,
            "model": "gpt-5.4", "status": "completed",
            "output": [{"id": "msg_offline", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(selected), "annotations": []}]}],
        })
    class Credential:
        async def get_token(self, *scopes, **kwargs):
            assert scopes == ("https://ai.azure.com/.default",)
            return AccessToken("offline-contract-token", 2_147_483_647)
    class OfflineProject(AIProjectClient):
        def get_openai_client(self, **kwargs):
            assert kwargs == {}
            return super().get_openai_client(
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(support, "_project_client", lambda: OfflineProject(
        endpoint="https://example.services.ai.azure.com/api/projects/offline",
        credential=Credential(), allow_preview=True))
    answer = await support.select_evidence(question, evidence, history, "gpt-5.4")
    assert answer.model_dump() == selected and len(requests) == 1
    assert requests[0].url.path == "/api/projects/offline/openai/v1/responses"
    body = json.loads(requests[0].content)
    assert body["model"] == "gpt-5.4" and body["store"] is False and "tools" not in body
    assert [item["role"] for item in body["input"]] == ["developer", "user"]
    assert all(item["type"] == "message" and len(item["content"]) == 1
               and item["content"][0]["type"] == "input_text" for item in body["input"])
    assert "Never use model memory" in body["input"][0]["content"][0]["text"]
    grounding = json.loads(body["input"][1]["content"][0]["text"])
    assert grounding == {"current_question": question, "history_not_evidence": history[-12:],
                         "retrieved_evidence": evidence}
    assert support.render_answer(answer, evidence)[1][0]["source_uri"] == evidence[0]["source_uri"]


@pytest.mark.asyncio
async def test_route_session_isolation_disabled_and_concurrency(config):
    config.adjuster_support_enabled = False
    with pytest.raises(HTTPException) as disabled:
        await support.support_chat(support.SupportRequest(message="question"))
    assert disabled.value.status_code == 503
    config.adjuster_support_enabled = True
    with pytest.raises(HTTPException) as wrong:
        await support.support_chat(support.SupportRequest(message="question", session_id="adjuster-kimi-unit"))
    assert wrong.value.status_code == 422
    support._active = 2
    with pytest.raises(HTTPException) as full:
        await support.support_chat(support.SupportRequest(message="question"))
    assert full.value.status_code == 429


@pytest.mark.asyncio
async def test_support_cancellation_closes_retrieval_and_releases_capacity(config, monkeypatch):
    entered = asyncio.Event()
    cancelled = False
    async def retrieve(_):
        nonlocal cancelled
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled = True
    monkeypatch.setattr(support, "retrieve", retrieve)
    response = await support.support_chat(support.SupportRequest(message="question"))
    await anext(response.body_iterator)
    task = asyncio.create_task(anext(response.body_iterator))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled and support._active == 0


@pytest.mark.asyncio
async def test_public_router_contract(config, monkeypatch):
    monkeypatch.setattr(support, "retrieve", AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(support.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/adjuster-support/status")).json()["index"] == support.INDEX
        response = await client.post("/adjuster-support/gpt-5-4-mini/chat", json={"message": "Missing evidence?"})
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[0]["session_id"].startswith("adjuster-gpt-5-4-mini-")
        assert events[1]["text"] == support.UNKNOWN and events[-1]["kind"] == "done"
        # Deprecated slug still served for clients deployed before the rename; it resolves to the canonical model.
        legacy = await client.post("/adjuster-support/hr-gpt-5-4-mini/chat", json={"message": "Missing evidence?"})
        legacy_events = [json.loads(line) for line in legacy.text.splitlines()]
        assert legacy_events[0]["session_id"].startswith("adjuster-gpt-5-4-mini-")
        assert legacy_events[-1]["kind"] == "done"
        assert (await client.post("/adjuster-support/unknown-model/chat", json={"message": "Q"})).status_code == 404


def test_deprecated_model_slugs_and_session_prefixes_resolve_to_canonical_names():
    assert support.canonical_model_slug("hr-kimi") == "kimi" == support.canonical_model_slug("kimi")
    assert support.session_prefixes("gpt-5-4") == ("adjuster-gpt-5-4-", "adjuster-hr-gpt-5-4-")
    assert support.session_prefixes("gpt-5-4-mini") == ("adjuster-gpt-5-4-mini-", "adjuster-hr-gpt-5-4-mini-")
    assert set(support.MODELS) == {"gpt-5-4", "gpt-5-4-mini", "kimi"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["history_load", "history_save_user", "search_retrieval",
                                  "model_selection", "answer_validation", "history_save_assistant"])
async def test_failures_log_stage_and_class_without_private_data(config, evidence, monkeypatch, caplog, stage):
    private = "PRIVATE_POLICYHOLDER_PROMPT_TOKEN_AND_RESPONSE"
    monkeypatch.setattr(support, "retrieve", AsyncMock(return_value=evidence))
    selected = support.EvidenceAnswer(selections=[support.Selection(
        source_id=evidence[0]["id"], quote="Missing evidence requires human review.")], unknown=False)
    monkeypatch.setattr(support, "select_evidence", AsyncMock(return_value=selected))
    if stage == "history_load":
        support.load_support_history.side_effect = AzureError(private)
    elif stage == "history_save_user":
        support.append_message.side_effect = AzureError(private)
    elif stage == "search_retrieval":
        support.retrieve.side_effect = support.SupportRetrievalError(403)
    elif stage == "model_selection":
        support.select_evidence.side_effect = RuntimeError(private)
    elif stage == "answer_validation":
        monkeypatch.setattr(support, "render_answer", lambda *_: (_ for _ in ()).throw(RuntimeError(private)))
    else:
        support.append_message.side_effect = [None, AzureError(private)]
    sid = "adjuster-gpt-5-4-private-person"
    with caplog.at_level(logging.ERROR):
        events = [event async for event in support.run_support_turn(sid, private)]
    records = [record for record in caplog.records if record.name == support.__name__]
    assert records and records[-1].stage == stage and records[-1].error_class
    if stage == "search_retrieval":
        assert records[-1].http_status == 403
    assert all(record.exc_info is None for record in records)
    assert private not in caplog.text and sid not in caplog.text
    assert events[-2]["kind"] == "error" and private not in json.dumps(events)


@pytest.mark.asyncio
async def test_model_request_diagnostics_keep_status_not_private_response(config, evidence, monkeypatch, caplog):
    private = "PRIVATE_MODEL_REQUEST_RESPONSE_AUTH"
    response = httpx.Response(404, request=httpx.Request(
        "POST", "https://model.unit.test/private", headers={"Authorization": private}),
        json={"error": private})
    error = NotFoundError(private, response=response, body={"error": private})
    class Context:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get_openai_client(self):
            return self
        @property
        def responses(self):
            return self
        async def create(self, **kwargs):
            raise error
    monkeypatch.setattr(support, "_project_client", Context)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(NotFoundError):
            await support.select_evidence(private, evidence, [], "gpt-5.4")
    record = next(record for record in caplog.records if record.name == support.__name__)
    assert record.stage == "model_request" and record.http_status == 404
    assert record.error_class == "NotFoundError" and record.exc_info is None
    assert private not in caplog.text and "https://" not in caplog.text
