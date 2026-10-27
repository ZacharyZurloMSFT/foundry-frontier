"""AI Gateway routing for the API app's model calls: configuration, aliases, auth and call sites."""
import asyncio
import contextlib
import contextvars
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import adjuster_support as support
from app import (
    azure_clients,
    insurance,
    main,
    model_gateway,
    observability,
    traces,
)
from app.harness import routes as harness_routes
from app.harness import runtime as harness_runtime
from app.harness import store
from app.harness.models import HarnessError, Text
from app.model_gateway import GatewayConfigError
from app.settings import Settings
from azure.core.credentials import AccessToken
from azure.monitor.query import LogsQueryStatus
from fastapi import HTTPException
from test_harness import Session

GATEWAY = "https://ai-gateway-northwind.azure-api.net"
ENDPOINT = f"{GATEWAY}/default/models/openai/v1/"
PROVIDER = "aisvc-zurlo-northwind"
SCOPE = "https://azure-api.net/aigateway/.default"
TOKEN = "gateway-entra-token"
FOUNDRY_ENDPOINT = "https://foundry.unit.test/api/projects/northwind-orders"


def gateway_settings(**overrides):
    values = {
        "ai_gateway_required": False, "ai_gateway_model_endpoint": ENDPOINT, "ai_gateway_scope": SCOPE,
        "ai_gateway_model_provider": PROVIDER, "ai_gateway_model_aliases": "",
        "planner_deployment": "gpt-5.4", "worker_deployment": "gpt-5.4-mini",
        "harness_model": "", "foundry_project_endpoint": FOUNDRY_ENDPOINT,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def direct_settings(**overrides):
    return gateway_settings(ai_gateway_model_endpoint="", ai_gateway_model_provider="", **overrides)


class Credential:
    """Synchronous TokenCredential double that records the scopes and threads it was called with."""

    def __init__(self, token=TOKEN, lifetime=3600):
        self.token, self.lifetime = token, lifetime
        self.scopes, self.threads = [], []

    def get_token(self, *scopes, **_kwargs):
        self.scopes.append(scopes)
        self.threads.append(threading.get_ident())
        return AccessToken(self.token, int(time.time()) + self.lifetime)


@pytest.fixture(autouse=True)
def fresh_gateway_clients():
    model_gateway.reset_gateway_clients()
    yield
    model_gateway.reset_gateway_clients()


@pytest.fixture
def gateway(monkeypatch):
    """Serve every get_gateway_client() call from a recording MockTransport and a fake Entra credential."""
    sent = []
    box = SimpleNamespace(respond=None)

    def handle(request):
        sent.append(request)
        return box.respond(request)

    credential = Credential()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    real = model_gateway.get_gateway_client
    monkeypatch.setattr(model_gateway, "get_gateway_client",
                        lambda settings=None, **_: real(settings, credential=credential, http_client=http_client))
    return SimpleNamespace(requests=sent, credential=credential,
                           reply=lambda respond: setattr(box, "respond", respond))


def forbid_direct(monkeypatch):
    def fail(*_args, **_kwargs):
        pytest.fail("A direct Foundry client was created while the AI Gateway is in use.")

    for module in (support,):
        monkeypatch.setattr(module, "_project_client", fail)


class RecordingTracer:
    """Collects the attributes set on every span a module opens, by span name."""

    def __init__(self):
        self.spans = {}

    @contextlib.contextmanager
    def start_as_current_span(self, name, *_args, attributes=None, **_kwargs):
        recorded = self.spans.setdefault(name, dict(attributes or {}))
        yield SimpleNamespace(set_attribute=recorded.__setitem__)


@pytest.fixture
def spans(monkeypatch):
    tracer = RecordingTracer()
    monkeypatch.setattr(support, "tracer", tracer)
    monkeypatch.setattr(harness_routes, "get_tracer", lambda *_: tracer)
    return tracer.spans


def assert_gateway_call(request, operation, alias):
    assert str(request.url) == ENDPOINT + operation
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert "api-key" not in request.headers
    assert json.loads(request.content)["model"] == alias


def completion(text="A grounded answer."):
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-5.4-mini-2026",
        "prompt_filter_results": [{"prompt_index": 0, "content_filter_results": {
            "jailbreak": {"filtered": False, "detected": False}}}],
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text},
                     "content_filter_results": {"hate": {"filtered": False, "severity": "safe"}}}],
    }


# ── Aliases ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("deployment,alias", [
    ("gpt-5.4", f"{PROVIDER}/gpt-5-4"),
    ("Kimi-K2.6", f"{PROVIDER}/kimi-k2-6"),
    ("gpt-4o", f"{PROVIDER}/gpt-4o"),
    ("gpt-5.4-mini", f"{PROVIDER}/gpt-5-4-mini"),
    ("gpt-5.6-luna", f"{PROVIDER}/gpt-5-6-luna"),
    ("text-embedding-3-small", f"{PROVIDER}/text-embedding-3-small"),
    ("GPT 5_6.Luna", f"{PROVIDER}/gpt-5-6-luna"),
])
def test_alias_is_provider_plus_sanitised_registration_name(deployment, alias):
    assert model_gateway.gateway_alias(gateway_settings(), deployment) == alias


def test_alias_override_map_wins_and_other_deployments_are_derived():
    settings = gateway_settings(ai_gateway_model_aliases=json.dumps({
        "gpt-5.4": "other-provider/frontier", "Kimi-K2.6": f"{PROVIDER}/kimi-custom"}))
    assert model_gateway.gateway_alias(settings, "gpt-5.4") == "other-provider/frontier"
    assert model_gateway.gateway_alias(settings, "kimi-k2.6") == f"{PROVIDER}/kimi-custom"
    assert model_gateway.gateway_alias(settings, "gpt-5.4-mini") == f"{PROVIDER}/gpt-5-4-mini"


def test_overrides_alone_are_enough_without_a_provider():
    settings = gateway_settings(ai_gateway_model_provider="",
                                ai_gateway_model_aliases=json.dumps({"gpt-5.4": "p/gpt-5-4"}))
    assert model_gateway.gateway_alias(settings, "gpt-5.4") == "p/gpt-5-4"
    assert model_gateway.configuration_problem(settings) is None
    with pytest.raises(GatewayConfigError, match="AI_GATEWAY_MODEL_PROVIDER"):
        model_gateway.gateway_alias(settings, "gpt-4o")


def test_missing_provider_and_override_is_an_error_not_a_guessed_alias():
    settings = gateway_settings(ai_gateway_model_provider="")
    with pytest.raises(GatewayConfigError, match="AI_GATEWAY_MODEL_PROVIDER"):
        model_gateway.gateway_alias(settings, "gpt-5.4")
    assert "AI_GATEWAY_MODEL_PROVIDER" in model_gateway.configuration_problem(settings)


@pytest.mark.parametrize("raw", [
    "not json", "[]", '"text"', '{"gpt-5.4": 5}', '{"gpt-5.4": "bare-name"}', '{"": "p/x"}', '{"gpt-5.4": "p/x/y"}',
])
def test_invalid_alias_map_is_rejected(raw):
    settings = gateway_settings(ai_gateway_model_aliases=raw)
    with pytest.raises(GatewayConfigError, match="AI_GATEWAY_MODEL_ALIASES"):
        model_gateway.gateway_alias(settings, "gpt-5.4")
    assert "AI_GATEWAY_MODEL_ALIASES" in model_gateway.configuration_problem(settings)


@pytest.mark.parametrize("provider", ["a/b", "a b"])
def test_provider_must_be_a_bare_name(provider):
    with pytest.raises(GatewayConfigError, match="AI_GATEWAY_MODEL_PROVIDER"):
        model_gateway.gateway_alias(gateway_settings(ai_gateway_model_provider=provider), "gpt-5.4")


def test_blank_deployment_has_no_alias():
    with pytest.raises(GatewayConfigError):
        model_gateway.gateway_alias(gateway_settings(), " ")


# ── Endpoint validation ────────────────────────────────────────────────────
@pytest.mark.parametrize("endpoint,expected", [
    (ENDPOINT, ENDPOINT),
    (ENDPOINT.rstrip("/"), ENDPOINT),
    (f"  {ENDPOINT}  ", ENDPOINT),
    ("https://gw.example.net/openai/v1", "https://gw.example.net/openai/v1/"),
    ("HTTPS://gw.example.net:8443/default/models/openai/v1//",
     "https://gw.example.net:8443/default/models/openai/v1/"),
])
def test_valid_endpoints_are_normalised_with_one_trailing_slash(endpoint, expected):
    assert model_gateway.validate_endpoint(endpoint) == expected


@pytest.mark.parametrize("endpoint", [
    "", "   ",
    "http://gw.example.net/default/models/openai/v1/",
    "ftp://gw.example.net/openai/v1/",
    "gw.example.net/openai/v1",
    "/default/models/openai/v1/",
    "https:///openai/v1/",
    "https://user:secret@gw.example.net/openai/v1/",
    "https://user@gw.example.net/openai/v1/",
    "https://gw.example.net/openai/v1/?api-version=1",
    "https://gw.example.net/openai/v1/#fragment",
    "https://gw.example.net/default/models/",
    "https://gw.example.net/openai/v2/",
    "https://gw.example.net/xopenai/v1",
    "https://gw.example.net",
    "https://gw.example.net:badport/openai/v1/",
    "https://gw .example.net/openai/v1/",
])
def test_invalid_endpoints_are_rejected(endpoint):
    with pytest.raises(GatewayConfigError, match="AI_GATEWAY_MODEL_ENDPOINT"):
        model_gateway.validate_endpoint(endpoint)


def test_sdk_base_url_drops_the_openai_v1_suffix_the_sdk_appends_itself():
    assert model_gateway.sdk_base_url(gateway_settings()) == f"{GATEWAY}/default/models"
    assert model_gateway.sdk_base_url(
        gateway_settings(ai_gateway_model_endpoint="https://gw.example.net/openai/v1")) == "https://gw.example.net"


def test_scope_defaults_when_blank_or_absent():
    assert model_gateway.gateway_scope(gateway_settings()) == SCOPE
    assert model_gateway.gateway_scope(gateway_settings(ai_gateway_scope="  ")) == SCOPE
    assert model_gateway.gateway_scope(SimpleNamespace()) == SCOPE
    assert model_gateway.gateway_scope(gateway_settings(ai_gateway_scope="api://custom/.default")) == (
        "api://custom/.default")


# ── Settings ───────────────────────────────────────────────────────────────
@pytest.fixture
def clean_gateway_env(monkeypatch):
    for name in ("AI_GATEWAY_REQUIRED", "AI_GATEWAY_MODEL_ENDPOINT", "AI_GATEWAY_SCOPE",
                 "AI_GATEWAY_MODEL_PROVIDER", "AI_GATEWAY_MODEL_ALIASES"):
        monkeypatch.delenv(name, raising=False)


def test_settings_are_unset_by_default(clean_gateway_env):
    settings = Settings(_env_file=None)
    assert settings.ai_gateway_required is False
    assert settings.ai_gateway_model_endpoint == "" and settings.ai_gateway_model_provider == ""
    assert settings.ai_gateway_model_aliases == "" and settings.ai_gateway_scope == SCOPE
    assert not model_gateway.gateway_enabled(settings)
    assert model_gateway.route_for(settings) == "direct"
    model_gateway.require_gateway(settings)


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True),
    ("false", False), ("0", False), ("no", False), ("", False), ("  ", False),
])
def test_required_flag_accepts_the_documented_spellings_and_blank(monkeypatch, clean_gateway_env, raw, expected):
    monkeypatch.setenv("AI_GATEWAY_REQUIRED", raw)
    assert Settings(_env_file=None).ai_gateway_required is expected


def test_settings_read_the_gateway_environment_and_blank_scope_is_the_default(monkeypatch, clean_gateway_env):
    monkeypatch.setenv("AI_GATEWAY_MODEL_ENDPOINT", f" {ENDPOINT} ")
    monkeypatch.setenv("AI_GATEWAY_MODEL_PROVIDER", PROVIDER)
    monkeypatch.setenv("AI_GATEWAY_MODEL_ALIASES", '{"gpt-5.4": "p/x"}')
    monkeypatch.setenv("AI_GATEWAY_SCOPE", "")
    settings = Settings(_env_file=None)
    assert settings.ai_gateway_model_endpoint == ENDPOINT and settings.ai_gateway_model_provider == PROVIDER
    assert settings.ai_gateway_scope == SCOPE
    assert model_gateway.gateway_alias(settings, "gpt-5.4") == "p/x"
    monkeypatch.setenv("AI_GATEWAY_SCOPE", "api://custom/.default")
    assert Settings(_env_file=None).ai_gateway_scope == "api://custom/.default"


# ── Fail closed and status ─────────────────────────────────────────────────
def test_required_gateway_fails_closed_when_missing_or_unusable():
    model_gateway.require_gateway(direct_settings())
    model_gateway.require_gateway(gateway_settings(ai_gateway_required=True))
    for broken in (
        direct_settings(ai_gateway_required=True),
        gateway_settings(ai_gateway_required=True, ai_gateway_model_endpoint="http://gw.example.net/openai/v1/"),
        gateway_settings(ai_gateway_required=True, ai_gateway_model_provider=""),
        gateway_settings(ai_gateway_required=True, ai_gateway_model_aliases="not json"),
    ):
        with pytest.raises(GatewayConfigError, match="AI_GATEWAY_REQUIRED") as raised:
            model_gateway.require_gateway(broken)
        assert "direct Foundry model calls are blocked" in str(raised.value)


def test_optional_gateway_that_is_selected_but_broken_is_reported_but_never_falls_back_silently():
    settings = gateway_settings(ai_gateway_model_endpoint="http://gw.example.net/openai/v1/")
    model_gateway.require_gateway(settings)
    assert model_gateway.route_for(settings) == "gateway"
    assert "AI_GATEWAY_MODEL_ENDPOINT" in model_gateway.configuration_problem(settings)
    with pytest.raises(GatewayConfigError):
        model_gateway.get_gateway_client(settings, credential=Credential())


def test_status_is_non_secret_and_describes_the_route():
    result = model_gateway.status(gateway_settings(ai_gateway_required=True), ["Kimi-K2.6", "gpt-5.4"])
    assert result == {
        "required": True, "enabled": True, "endpoint_host": "ai-gateway-northwind.azure-api.net",
        "provider": PROVIDER, "scope": SCOPE,
        "features": dict.fromkeys(("adjuster_support", "harness", "concierge", "help_center"), "gateway"),
        "model_aliases": {"gpt-5.4": f"{PROVIDER}/gpt-5-4", "gpt-5.4-mini": f"{PROVIDER}/gpt-5-4-mini",
                          "Kimi-K2.6": f"{PROVIDER}/kimi-k2-6"},
        "problem": None,
    }
    assert "/default/models" not in json.dumps(result)


def test_status_for_direct_and_for_required_but_missing():
    direct = model_gateway.status(direct_settings())
    assert direct["enabled"] is False and direct["endpoint_host"] == "" and direct["model_aliases"] == {}
    assert set(direct["features"].values()) == {"direct"} and direct["problem"] is None
    blocked = model_gateway.status(direct_settings(ai_gateway_required=True))
    assert blocked["required"] is True and blocked["enabled"] is False
    assert set(blocked["features"].values()) == {"direct"}
    assert "AI_GATEWAY_REQUIRED" in blocked["problem"]


def test_status_with_unusable_aliases_never_raises():
    result = model_gateway.status(gateway_settings(ai_gateway_model_provider=""))
    assert result["enabled"] is True and "AI_GATEWAY_MODEL_PROVIDER" in result["problem"]
    assert set(result["model_aliases"].values()) == {None}


@pytest.mark.asyncio
async def test_gateway_status_route_matches_the_helper(monkeypatch):
    settings = gateway_settings()
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    stamped = []
    monkeypatch.setattr(main, "set_request_gateway", lambda route, alias="": stamped.append(route))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/gateway/status")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True and body["endpoint_host"] == "ai-gateway-northwind.azure-api.net"
    assert body["features"] == dict.fromkeys(("adjuster_support", "harness", "concierge", "help_center"), "gateway")
    assert body["model_aliases"] == {"gpt-5.4": f"{PROVIDER}/gpt-5-4", "gpt-5.4-mini": f"{PROVIDER}/gpt-5-4-mini",
                                     "Kimi-K2.6": f"{PROVIDER}/kimi-k2-6"}
    assert set(body) == {"required", "enabled", "endpoint_host", "provider", "scope", "features",
                         "model_aliases", "problem"}
    assert stamped == ["gateway"]


@pytest.mark.asyncio
async def test_gateway_status_route_reports_direct_by_default(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: direct_settings())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        body = (await client.get("/gateway/status")).json()
    assert body["enabled"] is False and set(body["features"].values()) == {"direct"}


# ── Token provider ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_token_provider_requests_the_gateway_scope_off_the_event_loop_thread_and_caches():
    credential = Credential()
    provider = model_gateway.GatewayTokenProvider(credential, SCOPE)
    assert [await provider(), await provider(), await provider()] == [TOKEN] * 3
    assert credential.scopes == [(SCOPE,)]
    assert credential.threads and credential.threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_token_provider_refreshes_shortly_before_expiry():
    class Counting:
        def __init__(self):
            self.scopes = []

        def get_token(self, scope):
            self.scopes.append(scope)
            return AccessToken(f"token-{len(self.scopes)}", 11_000)

    now = [1_000.0]
    credential = Counting()
    provider = model_gateway.GatewayTokenProvider(credential, SCOPE, refresh_margin=300, clock=lambda: now[0])
    assert await provider() == "token-1"
    now[0] = 10_699
    assert await provider() == "token-1" and len(credential.scopes) == 1
    now[0] = 10_700
    assert await provider() == "token-2" and len(credential.scopes) == 2


@pytest.mark.asyncio
async def test_token_provider_never_caches_a_token_without_an_expiry():
    calls = []

    def get_token(scope):
        calls.append(scope)
        return SimpleNamespace(token="no-expiry")

    provider = model_gateway.GatewayTokenProvider(SimpleNamespace(get_token=get_token), SCOPE)
    assert [await provider(), await provider()] == ["no-expiry"] * 2
    assert calls == [SCOPE, SCOPE]


@pytest.mark.asyncio
async def test_token_provider_supports_async_credentials_and_single_flights_concurrent_refreshes():
    calls = []

    class AsyncCredential:
        async def get_token(self, *scopes):
            calls.append(scopes)
            await asyncio.sleep(0.01)
            return AccessToken("async-token", int(time.time()) + 3600)

    provider = model_gateway.GatewayTokenProvider(AsyncCredential(), SCOPE)
    assert await asyncio.gather(*(provider() for _ in range(5))) == ["async-token"] * 5
    assert calls == [(SCOPE,)]


@pytest.mark.asyncio
async def test_token_provider_failures_propagate_and_are_not_cached():
    attempts = []

    def get_token(scope):
        attempts.append(scope)
        if len(attempts) == 1:
            raise RuntimeError("identity unavailable")
        return AccessToken("recovered", int(time.time()) + 3600)

    provider = model_gateway.GatewayTokenProvider(SimpleNamespace(get_token=get_token), SCOPE)
    with pytest.raises(RuntimeError, match="identity unavailable"):
        await provider()
    assert await provider() == "recovered"


# ── Client ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_client_authenticates_with_a_bearer_token_never_a_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-be-used")
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={"id": "r", "object": "response", "created_at": 1, "model": "m",
                                             "status": "completed", "output": []})
        return httpx.Response(200, json=completion())

    credential = Credential()
    client = model_gateway.get_gateway_client(
        gateway_settings(ai_gateway_model_endpoint=ENDPOINT.rstrip("/")), credential=credential,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert str(client.base_url) == ENDPOINT
    await client.chat.completions.create(model=f"{PROVIDER}/gpt-5-4", messages=[{"role": "user", "content": "x"}])
    await client.responses.create(model=f"{PROVIDER}/gpt-5-4", input="x")
    assert [str(r.url) for r in seen] == [ENDPOINT + "chat/completions", ENDPOINT + "responses"]
    for request in seen:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert "api-key" not in request.headers
    assert credential.scopes == [(SCOPE,)]


def test_client_is_cached_per_configuration():
    credential, transport = Credential(), httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None))
    first = model_gateway.get_gateway_client(gateway_settings(), credential=credential, http_client=transport)
    assert model_gateway.get_gateway_client(gateway_settings(), credential=credential, http_client=transport) is first
    other_endpoint = gateway_settings(ai_gateway_model_endpoint="https://other.example.net/default/models/openai/v1/")
    assert model_gateway.get_gateway_client(other_endpoint, credential=credential, http_client=transport) is not first
    other_scope = gateway_settings(ai_gateway_scope="api://other/.default")
    assert model_gateway.get_gateway_client(other_scope, credential=credential, http_client=transport) is not first
    assert model_gateway.get_gateway_client(gateway_settings(), credential=Credential(), http_client=transport) is not first


@pytest.mark.asyncio
async def test_client_defaults_to_the_app_identity_and_default_scope(monkeypatch):
    credential = Credential()
    monkeypatch.setattr(azure_clients, "get_credential", lambda: credential)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=completion())))
    client = model_gateway.get_gateway_client(gateway_settings(ai_gateway_scope=""), http_client=http_client)
    await client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}])
    assert credential.scopes == [(SCOPE,)]


@pytest.mark.asyncio
async def test_shutdown_closes_only_the_clients_the_module_owns():
    owned = model_gateway.get_gateway_client(gateway_settings(), credential=Credential())
    borrowed_transport = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    borrowed = model_gateway.get_gateway_client(gateway_settings(), credential=Credential(),
                                                http_client=borrowed_transport)
    await model_gateway.close_gateway_clients()
    assert owned.is_closed() and not borrowed.is_closed()
    await borrowed_transport.aclose()


@pytest.mark.asyncio
async def test_application_shutdown_releases_the_gateway_clients():
    client = model_gateway.get_gateway_client(gateway_settings(), credential=Credential())
    async with main.lifespan(main.app):
        assert not client.is_closed()
    assert client.is_closed()


@pytest.mark.asyncio
async def test_application_startup_logs_the_route_and_any_gateway_problem(monkeypatch, caplog):
    caplog.set_level("INFO", logger=main.logger.name)
    monkeypatch.setattr(main, "get_settings", lambda: direct_settings(ai_gateway_required=True))
    async with main.lifespan(main.app):
        pass
    assert any(r.levelname == "ERROR" and "AI_GATEWAY_REQUIRED" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setattr(main, "get_settings", lambda: gateway_settings())
    async with main.lifespan(main.app):
        pass
    assert any(r.levelname == "INFO" and "AI Gateway" in r.getMessage() for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)
    caplog.clear()
    monkeypatch.setattr(main, "get_settings", lambda: direct_settings())
    async with main.lifespan(main.app):
        pass
    assert not [r for r in caplog.records if "AI Gateway" in r.getMessage()]


# ── Adjuster support ───────────────────────────────────────────────────────
def support_settings(**overrides):
    return gateway_settings(
        search_endpoint="https://search.unit.test",
        cosmos_endpoint="https://cosmos.unit.test", adjuster_support_enabled=True,
        adjuster_support_index=support.INDEX, adjuster_support_search_auth="entra_id",
        adjuster_support_timeout=10, adjuster_support_concurrency=2, **overrides)


@pytest.fixture
def support_evidence():
    return [{"id": "ADJ-TEST-001", "title": "Fictional evidence procedure", "version": "1",
             "source_uri": "urn:northwind:synthetic:employee-support:ADJ-TEST-001:v1",
             "effective_from": "2026-01-01", "category": "adjuster",
             "content": "Missing evidence requires human review. Never automatically deny a claim."}]


def response_with(payload):
    return {"id": "resp_gateway", "object": "response", "created_at": 1, "model": "gpt-5.4", "status": "completed",
            "output": [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(payload), "annotations": []}]}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment,registration", [("gpt-5.4", "gpt-5-4"), ("Kimi-K2.6", "kimi-k2-6")])
async def test_support_model_call_goes_to_the_gateway_responses_endpoint(
        monkeypatch, gateway, spans, support_evidence, deployment, registration):
    monkeypatch.setattr(support, "get_settings", lambda: support_settings())
    forbid_direct(monkeypatch)
    selected = {"selections": [{"source_id": "ADJ-TEST-001", "quote": "Missing evidence requires human review."}],
                "unknown": False}
    gateway.reply(lambda request: httpx.Response(200, json=response_with(selected)))
    history = [{"role": "user", "content": f"Prior {i}"} for i in range(20)]
    answer = await support.select_evidence("When is review needed?", support_evidence, history, deployment)
    assert answer.model_dump() == selected
    (request,) = gateway.requests
    assert_gateway_call(request, "responses", f"{PROVIDER}/{registration}")
    body = json.loads(request.content)
    assert body["store"] is False and "tools" not in body and "agent_reference" not in body
    assert [item["role"] for item in body["input"]] == ["developer", "user"]
    grounding = json.loads(body["input"][1]["content"][0]["text"])
    assert grounding == {"current_question": "When is review needed?", "history_not_evidence": history[-12:],
                         "retrieved_evidence": support_evidence}
    assert spans["adjuster_support.model_request"] == {
        "gateway.route": "gateway", "gateway.model_alias": f"{PROVIDER}/{registration}"}


@pytest.mark.asyncio
async def test_support_turn_is_answered_through_the_gateway_end_to_end(monkeypatch, gateway, support_evidence):
    monkeypatch.setattr(support, "get_settings", lambda: support_settings(foundry_project_endpoint=""))
    monkeypatch.setattr(support, "_active", 0)
    monkeypatch.setattr(support, "append_message", AsyncMock())
    monkeypatch.setattr(support, "load_support_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(support, "retrieve", AsyncMock(return_value=support_evidence))
    forbid_direct(monkeypatch)
    gateway.reply(lambda request: httpx.Response(200, json=response_with({
        "selections": [{"source_id": "ADJ-TEST-001", "quote": "Missing evidence requires human review."}],
        "unknown": False})))
    events = [e async for e in support.run_support_turn("adjuster-gpt-5-4-unit", "Missing evidence?")]
    assert [e["kind"] for e in events] == ["session", "assistant_delta", "citations", "done"]
    assert events[2]["citations"][0]["source_uri"] == support_evidence[0]["source_uri"]
    assert json.loads(gateway.requests[0].content)["model"] == f"{PROVIDER}/gpt-5-4"


def test_support_availability_reports_the_route_and_gateway_problems(monkeypatch):
    settings = support_settings()
    monkeypatch.setattr(support, "get_settings", lambda: settings)
    assert support.availability()["state"] == "configured" and support.availability()["model_route"] == "gateway"
    settings.foundry_project_endpoint = ""
    assert support.availability()["state"] == "configured"
    settings.ai_gateway_model_endpoint = ""
    assert support.availability()["state"] == "unavailable" and support.availability()["model_route"] == "direct"
    settings.foundry_project_endpoint, settings.ai_gateway_required = FOUNDRY_ENDPOINT, True
    state = support.availability()
    assert state["state"] == "unavailable" and "AI_GATEWAY_REQUIRED" in state["message"]
    settings.ai_gateway_model_endpoint, settings.ai_gateway_model_provider = ENDPOINT, ""
    assert "AI_GATEWAY_MODEL_PROVIDER" in support.availability()["message"]


@pytest.mark.asyncio
async def test_support_fails_closed_when_the_gateway_is_required_but_missing(monkeypatch, support_evidence):
    settings = support_settings(ai_gateway_model_endpoint="", ai_gateway_model_provider="", ai_gateway_required=True)
    monkeypatch.setattr(support, "get_settings", lambda: settings)
    forbid_direct(monkeypatch)
    with pytest.raises(HTTPException) as unavailable:
        await support.support_chat(support.SupportRequest(message="question"))
    assert unavailable.value.status_code == 503 and "AI_GATEWAY_REQUIRED" in unavailable.value.detail
    with pytest.raises(GatewayConfigError, match="AI_GATEWAY_REQUIRED"):
        await support.select_evidence("question", support_evidence, [], "gpt-5.4")


@pytest.mark.asyncio
async def test_support_never_falls_back_to_foundry_when_a_selected_gateway_is_broken(monkeypatch, support_evidence):
    settings = support_settings(ai_gateway_model_provider="")
    monkeypatch.setattr(support, "get_settings", lambda: settings)
    forbid_direct(monkeypatch)
    with pytest.raises(GatewayConfigError, match="AI_GATEWAY_MODEL_PROVIDER"):
        await support.select_evidence("question", support_evidence, [], "gpt-5.4")
    with pytest.raises(HTTPException) as unavailable:
        await support.support_chat(support.SupportRequest(message="question"))
    assert unavailable.value.status_code == 503 and "AI_GATEWAY_MODEL_PROVIDER" in unavailable.value.detail


@pytest.mark.asyncio
async def test_support_direct_path_is_unchanged_without_gateway_settings(monkeypatch, support_evidence):
    monkeypatch.setattr(support, "get_settings", lambda: support_settings(
        ai_gateway_model_endpoint="", ai_gateway_model_provider=""))
    monkeypatch.setattr(model_gateway, "get_gateway_client",
                        lambda *_a, **_k: pytest.fail("The gateway client must not be used without gateway settings."))
    captured = {}

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        def get_openai_client(self):
            return self

        @property
        def responses(self):
            return self

        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(status="completed", output_text='{"selections":[],"unknown":true}')

    monkeypatch.setattr(support, "_project_client", Context)
    assert (await support.select_evidence("question", support_evidence, [], "gpt-5.4")).unknown
    assert captured["model"] == "gpt-5.4" and captured["store"] is False


# ── Harness ────────────────────────────────────────────────────────────────
@pytest.fixture
def harness_config(monkeypatch):
    settings = Settings(
        _env_file=None, COPILOT_HARNESS_ENABLED=True, FOUNDRY_PROJECT_ENDPOINT=FOUNDRY_ENDPOINT,
        NORTHWIND_INSURANCE_AGENTS_READY=True, HOSTED_AGENTS_ENABLED=True,
        FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID="northwind-insurance-orchestrator",
        MCP_CLAIM_REVIEW_URL="https://mcp.unit.test/mcp/", COSMOS_ENDPOINT="https://unit.test/cosmos",
        AI_GATEWAY_MODEL_ENDPOINT=ENDPOINT, AI_GATEWAY_MODEL_PROVIDER=PROVIDER)
    monkeypatch.setattr(harness_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(harness_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(insurance, "get_settings", lambda: settings)
    monkeypatch.setattr(harness_routes, "runtime", harness_runtime.HarnessRuntime())
    return settings


async def start_run(token="gateway-bearer"):
    runtime = harness_runtime.HarnessRuntime()
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return Session(kwargs["on_event"])

    runtime.client = SimpleNamespace(create_session=AsyncMock(side_effect=create), delete_session=AsyncMock())
    runtime.credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token=token)))
    events = [e async for e in runtime.run("harness-test", "wf-harness-test", "hi", store.RunStore())]
    return runtime, captured, events


@pytest.mark.asyncio
async def test_harness_session_uses_the_gateway_provider_scope_and_alias(harness_config):
    runtime, captured, events = await start_run()
    assert events[-1].kind == "assistant_message"
    provider = captured["provider"]
    assert provider["type"] == "azure" and provider["wire_api"] == "responses"
    assert provider["base_url"] == f"{GATEWAY}/default/models"
    assert provider["wire_model"] == f"{PROVIDER}/gpt-5-4" and provider["max_output_tokens"] == 4096
    assert "api_key" not in provider and "bearer_token" not in provider
    assert captured["model"] == "gpt-5.4"
    assert await provider["bearer_token_provider"]({}) == "gateway-bearer"
    assert runtime.credential.get_token.await_args.args == (SCOPE,)


@pytest.mark.asyncio
async def test_harness_model_override_gets_its_own_alias(harness_config):
    harness_config.harness_model = "gpt-5.4-mini"
    _, captured, _ = await start_run()
    assert captured["model"] == "gpt-5.4-mini" and captured["provider"]["wire_model"] == f"{PROVIDER}/gpt-5-4-mini"
    harness_config.ai_gateway_model_aliases = json.dumps({"gpt-5.4-mini": "other/mini"})
    _, captured, _ = await start_run()
    assert captured["provider"]["wire_model"] == "other/mini"


@pytest.mark.asyncio
async def test_harness_direct_provider_is_unchanged_without_gateway_settings(harness_config):
    harness_config.ai_gateway_model_endpoint = ""
    harness_config.ai_gateway_model_provider = ""
    runtime, captured, _ = await start_run()
    assert captured["provider"] == {
        "type": "azure", "base_url": FOUNDRY_ENDPOINT, "wire_api": "responses",
        "bearer_token_provider": captured["provider"]["bearer_token_provider"], "max_output_tokens": 4096}
    assert await captured["provider"]["bearer_token_provider"]({}) == "gateway-bearer"
    assert runtime.credential.get_token.await_args.args == ("https://ai.azure.com/.default",)


def test_harness_status_names_the_route_and_its_cache_key_changes_with_the_gateway(harness_config):
    runtime = harness_runtime.HarnessRuntime()
    status = runtime.status()
    assert status["provider"] == "AI Gateway (APIM) / managed identity" and status["model_route"] == "gateway"
    assert status["model"] == "gpt-5.4" and status["state"] == "configured"
    _, sha = harness_runtime.instructions(harness_config)
    gateway_key = runtime.configuration(harness_config, sha)
    assert ENDPOINT in gateway_key and PROVIDER in gateway_key
    runtime.client, runtime.credential, runtime.verified_config = object(), object(), gateway_key
    assert runtime.status()["state"] == "ready"
    harness_config.ai_gateway_model_provider = "another-provider"
    assert runtime.configuration(harness_config, sha) != gateway_key
    assert runtime.status()["state"] == "configured"
    harness_config.ai_gateway_model_endpoint = ""
    harness_config.ai_gateway_model_provider = ""
    direct = runtime.status()
    assert direct["provider"] == "Azure Foundry / managed identity" and direct["model_route"] == "direct"


@pytest.mark.asyncio
async def test_harness_run_span_records_the_gateway_route_and_alias(harness_config, database, spans, monkeypatch):
    async def fake_run(*_):
        yield Text(kind="assistant_message", text="complete answer")

    monkeypatch.setattr(harness_routes.runtime, "run", fake_run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/harness/runs", json={"request_id": "5a9b3a0e-3b57-4f0e-a7e7-7d1f4e0d2c11",
                                                            "message": "status"})
    assert response.status_code == 200
    recorded = spans["harness.run"]
    assert recorded["model"] == "gpt-5.4" and recorded["gateway.route"] == "gateway"
    assert recorded["gateway.model_alias"] == f"{PROVIDER}/gpt-5-4"
    assert json.loads(response.text.splitlines()[0])["model"] == "gpt-5.4"


@pytest.mark.asyncio
async def test_harness_fails_closed_when_the_gateway_is_required_but_missing(harness_config):
    harness_config.ai_gateway_model_endpoint = ""
    harness_config.ai_gateway_required = True
    runtime = harness_runtime.HarnessRuntime()
    state = runtime.status()
    assert state["state"] == "unavailable" and "AI_GATEWAY_REQUIRED" in state["message"]
    runtime.client = SimpleNamespace(create_session=AsyncMock(), delete_session=AsyncMock())
    runtime.credential = SimpleNamespace(get_token=AsyncMock())
    with pytest.raises(HarnessError, match="AI_GATEWAY_REQUIRED") as blocked:
        _ = [e async for e in runtime.run("harness-test", "wf-harness-test", "hi", store.RunStore())]
    assert blocked.value.code == "gateway_unavailable"
    runtime.client.create_session.assert_not_awaited()
    runtime.credential.get_token.assert_not_awaited()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/harness/runs", json={"request_id": "8f0f5c2c-7c40-4d5e-8a39-3c4e1e8d6a10",
                                                            "message": "status"})
    assert response.status_code == 503 and "AI_GATEWAY_REQUIRED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_harness_rejects_an_unusable_selected_gateway_before_starting_the_runtime(harness_config):
    harness_config.ai_gateway_model_provider = ""
    runtime = harness_runtime.HarnessRuntime()
    runtime.start = AsyncMock()
    with pytest.raises(HarnessError, match="AI_GATEWAY_MODEL_PROVIDER"):
        _ = [e async for e in runtime.run("harness-test", "wf-harness-test", "hi", store.RunStore())]
    runtime.start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("TEST_COPILOT_RUNTIME") != "1",
                    reason="Set TEST_COPILOT_RUNTIME=1 after staging the pinned runtime.")
async def test_real_sdk_runtime_sends_responses_to_the_gateway_path_with_the_alias(monkeypatch):
    from aiohttp import web

    requests = []

    async def model(request):
        body = await request.json()
        requests.append((request.path, request.query_string, request.headers.get("Authorization"),
                         "api-key" in request.headers, body))
        item = {"id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "Nothing was retrieved.", "annotations": []}]}
        response = {"id": "resp_1", "object": "response", "created_at": int(time.time()),
                    "model": body["model"], "status": "in_progress", "output": []}
        part = item["content"][0]
        events = [
            {"type": "response.created", "response": response},
            {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
            {"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0, "content_index": 0,
             "part": {**part, "text": ""}},
            {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0,
             "delta": part["text"]},
            {"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0, "content_index": 0,
             "text": part["text"]},
            {"type": "response.content_part.done", "item_id": "msg_1", "output_index": 0, "content_index": 0,
             "part": part},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {
                **response, "status": "completed", "output": [item],
                "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}},
        ]
        text = "".join(f"event: {e['type']}\ndata: {json.dumps({**e, 'sequence_number': i})}\n\n"
                       for i, e in enumerate(events))
        return web.Response(text=text, content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/{tail:.*}", model)
    server = web.AppRunner(app)
    await server.setup()
    await web.TCPSite(server, "127.0.0.1", 0).start()
    port = server.addresses[0][1]
    settings = Settings(_env_file=None, COPILOT_HARNESS_TIMEOUT_SECONDS=30,
                        FOUNDRY_PROJECT_ENDPOINT="https://foundry.invalid/api/projects/northwind",
                        AI_GATEWAY_MODEL_ENDPOINT=f"https://127.0.0.1:{port}/default/models/openai/v1/",
                        AI_GATEWAY_MODEL_PROVIDER=PROVIDER)
    real_validate = model_gateway.validate_endpoint
    # The local model server speaks plain HTTP; production endpoints stay HTTPS-only.
    monkeypatch.setattr(model_gateway, "validate_endpoint",
                        lambda value: real_validate(value).replace("https://", "http://", 1))
    monkeypatch.setattr(harness_runtime, "get_settings", lambda: settings)
    runtime = harness_runtime.HarnessRuntime()
    try:
        await runtime.start()
        await runtime.credential.close()
        credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="local-gateway-token")),
                                     close=AsyncMock())
        runtime.credential = credential
        events = [e async for e in runtime.run("harness-local-gateway", "wf-harness-local-gateway",
                                               "Summarise claim CLM-2001.", store.RunStore())]
        assert events[-1].text.startswith("No policy or claim evidence was retrieved")
        path, query, authorization, has_key, body = requests[0]
        assert path == "/default/models/openai/v1/responses" and query == ""
        assert authorization == "Bearer local-gateway-token" and not has_key
        assert body["model"] == f"{PROVIDER}/gpt-5-4"
        # The runtime still recognises the deployment name (wire_model carries the alias), so it keeps
        # configuring reasoning exactly as it does on the direct path.
        assert body["reasoning"].get("effort")
        assert credential.get_token.await_args.args == (SCOPE,)
    finally:
        await runtime.stop()
        await server.cleanup()


# ── Observability and traces ───────────────────────────────────────────────
def test_span_attributes_carry_the_route_and_the_alias_only_when_the_gateway_serves_the_model():
    assert model_gateway.span_attributes(gateway_settings(), "gpt-5.4") == {
        "gateway.route": "gateway", "gateway.model_alias": f"{PROVIDER}/gpt-5-4"}
    assert model_gateway.span_attributes(gateway_settings()) == {"gateway.route": "gateway"}
    assert model_gateway.span_attributes(direct_settings(), "gpt-5.4") == {"gateway.route": "direct"}
    assert model_gateway.span_attributes(gateway_settings(ai_gateway_model_provider=""), "gpt-5.4") == {
        "gateway.route": "gateway"}
    attributes = {}
    model_gateway.stamp_span(SimpleNamespace(set_attribute=attributes.__setitem__), gateway_settings(), "gpt-5.4")
    assert attributes == {"gateway.route": "gateway", "gateway.model_alias": f"{PROVIDER}/gpt-5-4"}


def test_request_gateway_stamp_reaches_every_tracer_span_and_explicit_attributes_win():
    seen = []

    class Inner:
        def start_as_current_span(self, name, *args, **kwargs):
            seen.append(kwargs["attributes"])
            return contextlib.nullcontext()

        def start_span(self, name, *args, **kwargs):
            seen.append(kwargs["attributes"])
            return contextlib.nullcontext()

    def body():
        tracer = observability._BuildInfoTracer(Inner())
        tracer.start_as_current_span("before")
        observability.set_request_gateway("gateway")
        tracer.start_as_current_span("route only")
        observability.set_request_gateway("gateway", f"{PROVIDER}/gpt-5-4")
        tracer.start_span("with alias")
        tracer.start_as_current_span("explicit", attributes={"gateway.route": "custom"})

    contextvars.copy_context().run(body)
    assert "gateway.route" not in seen[0]
    assert seen[1]["gateway.route"] == "gateway" and "gateway.model_alias" not in seen[1]
    assert seen[2]["gateway.route"] == "gateway" and seen[2]["gateway.model_alias"] == f"{PROVIDER}/gpt-5-4"
    assert seen[3]["gateway.route"] == "custom"
    assert all("git_sha" in attributes for attributes in seen)


def trace_table(properties):
    return SimpleNamespace(
        columns=["timestamp", "itemType", "name", "duration", "operation_Id", "operation_ParentId", "id",
                 "customDimensions"],
        rows=[["2026-10-22T10:00:00Z", "AppDependencies", "adjuster_support.model_request", 12.5, "operation",
               "parent", "span", properties]])


def test_trace_rows_surface_the_gateway_route_and_alias(monkeypatch):
    tables = [trace_table({"gateway.route": "gateway", "gateway.model_alias": f"{PROVIDER}/gpt-5-4",
                           "gen_ai.request.model": "gpt-5.4"})]

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def query_workspace(self, **_kwargs):
            return SimpleNamespace(status=LogsQueryStatus.SUCCESS, tables=tables)

    monkeypatch.setenv("APPINSIGHTS_WORKSPACE_ID", "workspace-id")
    monkeypatch.setattr(traces, "LogsQueryClient", Client)
    monkeypatch.setattr(traces, "get_credential", lambda: "managed-identity")
    span = traces.get_trace("gateway-session")["spans"][0]
    assert span["gateway"] == "gateway" and span["gateway_model"] == f"{PROVIDER}/gpt-5-4"
    tables[:] = [trace_table({"gen_ai.request.model": "gpt-5.4"})]
    span = traces.get_trace("direct-session")["spans"][0]
    assert span["gateway"] == "" and span["gateway_model"] == ""


def test_unavailable_trace_has_no_spans_to_carry_a_gateway_stamp(monkeypatch):
    monkeypatch.setattr(traces, "_try_query", lambda *_: None)
    result = traces.get_trace("unavailable")
    assert result["status"] == "unavailable" and result["spans"] == []
