"""Opt-in gateway guard: only requests relayed by API Management with its Entra token get through."""
import base64
import functools
import importlib.util
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.websockets import WebSocketDisconnect

from app import gateway_guard
from app.gateway_guard import GatewayGuardMiddleware

ROOT = Path(__file__).resolve().parents[3]
TENANT = "11111111-1111-4111-8111-111111111111"
AUDIENCE = "22222222-2222-4222-8222-222222222222"
APIM = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
HEADER = "X-Northwind-Gateway-Token"
FLAG = "NORTHWIND_GATEWAY_AUTH_REQUIRED"
ENABLED = {
    FLAG: "true",
    "NORTHWIND_GATEWAY_TENANT_ID": TENANT,
    "NORTHWIND_GATEWAY_AUDIENCE": AUDIENCE,
    "NORTHWIND_GATEWAY_ALLOWED_OIDS": APIM,
}
REJECTED = {
    "error": {"code": "gateway_required", "message": "Requests must come through the Northwind API gateway."},
}


class Keys:
    """Stands in for PyJWKClient so no test reaches login.microsoftonline.com."""

    def __init__(self, private):
        self.private = private
        self.lookups = 0

    def get_signing_key_from_jwt(self, _token):
        self.lookups += 1
        return SimpleNamespace(key=self.private.public_key())


def claims_for(*, drop=(), exp_in=600, nbf_in=-1, iat_in=0, **changes):
    now = int(time.time())
    claims = {
        "iss": f"https://sts.windows.net/{TENANT}/", "aud": f"api://{AUDIENCE}",
        "iat": now + iat_in, "nbf": now + nbf_in, "exp": now + exp_in,
        "tid": TENANT, "oid": APIM, "sub": APIM, **changes,
    }
    for name in drop:
        del claims[name]
    return claims


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def encode_part(value):
    return b64(json.dumps(value).encode())


def sign_raw(claims, private):
    """Signs without PyJWT's encode-time validation, to build tokens PyJWT refuses to create."""
    signing_input = f"{encode_part({'alg': 'RS256', 'kid': 'test-key'})}.{encode_part(claims)}"
    return f"{signing_input}.{b64(private.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()))}"


def unsigned(header):
    return f"{encode_part(header)}.{encode_part({'oid': APIM})}.c2lnbmF0dXJl"


def make_token(private, *, kid="test-key", signing_key=None, algorithm="RS256", **claim_options):
    return jwt.encode(
        claims_for(**claim_options), signing_key or private, algorithm=algorithm,
        headers={"kid": kid} if kid else None,
    )


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def keys(private_key, monkeypatch):
    keys = Keys(private_key)
    monkeypatch.setattr(gateway_guard, "signing_client", lambda _tenant: keys)
    return keys


@pytest.fixture
def token(keys):
    return functools.partial(make_token, keys.private)


def build_app(environ, *, exempt=frozenset({"/health"}), lifespan=None):
    async def mounted(_request):
        return JSONResponse({"mounted": True})

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/data")
    async def data():
        return {"data": True}

    @app.post("/echo")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    @app.websocket("/ws")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")
        await websocket.close()

    app.mount("/mcp", Starlette(routes=[Route("/", mounted, methods=["GET", "POST"])]))
    app.add_middleware(GatewayGuardMiddleware, exempt_paths=exempt, environ=environ)
    return app


def client_for(environ, **options):
    return TestClient(build_app(environ, **options))


def call(client, token_value=None, path="/data"):
    return client.get(path, headers={} if token_value is None else {HEADER: token_value})


def assert_denied(response, status=401, code="gateway_required"):
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert response.headers.get("www-authenticate") == ("Bearer" if status == 401 else None)


def without(environ, name):
    return {key: value for key, value in environ.items() if key != name}


def refused(client, headers=None):
    with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect("/ws", headers=headers or {}):
        pass
    assert closed.value.code == 1008


@pytest.mark.parametrize("scheme", ["", "Bearer ", "bearer ", "BEARER   "])
def test_valid_gateway_token_is_accepted_raw_or_as_bearer(token, scheme):
    assert call(client_for(ENABLED), scheme + token()).json() == {"data": True}


@pytest.mark.parametrize("claims,audience_setting", [
    ({}, AUDIENCE),
    ({"aud": AUDIENCE}, AUDIENCE),
    ({"aud": AUDIENCE}, f"api://{AUDIENCE}"),
    ({"aud": f"api://{AUDIENCE}"}, f"api://{AUDIENCE}"),
    ({"aud": f"API://{AUDIENCE.upper()}"}, AUDIENCE),
    ({"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": AUDIENCE}, f"api://{AUDIENCE}"),
    ({"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0"}, AUDIENCE),
])
def test_both_issuer_versions_and_audience_forms_are_accepted(token, claims, audience_setting):
    environ = {**ENABLED, "NORTHWIND_GATEWAY_AUDIENCE": audience_setting}
    assert call(client_for(environ), token(**claims)).status_code == 200


@pytest.mark.parametrize("claims", [
    {"drop": ("nbf",)}, {"drop": ("iat",)}, {"drop": ("sub",)}, {"roles": ["Gateway.Call"]},
    {"exp_in": -10}, {"nbf_in": 10}, {"oid": APIM.upper()}, {"tid": TENANT.upper()},
])
def test_optional_claims_and_clock_skew_within_leeway_are_tolerated(token, claims):
    assert call(client_for(ENABLED), token(**claims)).status_code == 200


def test_allowed_principals_are_a_normalized_comma_separated_list(token):
    environ = {
        **ENABLED, "NORTHWIND_GATEWAY_TENANT_ID": f" {TENANT.upper()} ",
        "NORTHWIND_GATEWAY_ALLOWED_OIDS": f" {OTHER.upper()} , {APIM.upper()},, ",
    }
    client = client_for(environ)
    assert call(client, token()).status_code == 200
    assert call(client, token(oid=OTHER)).status_code == 200
    assert_denied(call(client, token(oid="55555555-5555-4555-8555-555555555555")))


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_only_the_literal_true_enables_the_guard(value):
    assert call(client_for({**ENABLED, FLAG: value})).json() == REJECTED


def test_header_name_is_case_insensitive(token):
    assert client_for(ENABLED).get("/data", headers={"X-NORTHWIND-GATEWAY-TOKEN": token()}).status_code == 200


@pytest.mark.parametrize("value", [
    None, "", "Bearer", "Bearer  ", "garbage", "Basic a.b.c", "a.b.c", "Bearer Bearer x",
])
def test_missing_or_malformed_header_is_rejected(keys, value):
    response = call(client_for(ENABLED), value)
    assert response.json() == REJECTED
    assert_denied(response)
    assert keys.lookups == 0


def test_repeated_header_is_rejected_even_if_each_value_is_valid(token):
    response = client_for(ENABLED).get("/data", headers=[(HEADER, token()), (HEADER, token())])
    assert_denied(response)


@pytest.mark.parametrize("changes", [
    {"aud": f"api://{OTHER}"}, {"aud": OTHER}, {"aud": [f"api://{AUDIENCE}"]}, {"aud": ""},
    {"aud": f"api://{AUDIENCE}/"}, {"aud": f"https://{AUDIENCE}"},
    {"iss": f"https://sts.windows.net/{OTHER}/"}, {"iss": f"https://login.microsoftonline.com/{OTHER}/v2.0"},
    {"iss": "https://evil.example/"}, {"iss": f"https://sts.windows.net/{TENANT}"},
    {"tid": OTHER}, {"tid": "not-a-guid"}, {"tid": 7}, {"tid": f"{{{TENANT}}}"},
    {"oid": OTHER}, {"oid": "not-a-guid"}, {"oid": 7}, {"oid": [APIM]}, {"oid": f"{{{APIM}}}"},
    {"exp": 1}, {"exp_in": -60}, {"nbf_in": 600}, {"iat_in": 600},
    {"scp": "access_as_user"}, {"scp": ""}, {"scp": None},
    {"drop": ("exp",)}, {"drop": ("aud",)}, {"drop": ("iss",)}, {"drop": ("tid",)}, {"drop": ("oid",)},
    {"drop": ("nbf", "iat")},
])
def test_token_claims_that_do_not_prove_the_gateway_identity_are_rejected(token, changes):
    assert call(client_for(ENABLED), token(**changes)).json() == REJECTED


@pytest.mark.parametrize("changes", [
    {"iss": [f"https://sts.windows.net/{TENANT}/"]}, {"exp": "soon"}, {"nbf": "later"}, {"iat": "now"},
    {"exp": None}, {"aud": None}, {"iss": None}, {"tid": None}, {"oid": None},
])
def test_validly_signed_tokens_with_malformed_claim_types_are_rejected(keys, changes):
    assert_denied(call(client_for(ENABLED), sign_raw(claims_for(**changes), keys.private)))


def test_tokens_signed_by_another_key_or_tampered_are_rejected(token):
    client = client_for(ENABLED)
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert_denied(call(client, token(signing_key=stranger)))
    header, _, signature = token().split(".")
    assert_denied(call(client, f"{header}.{encode_part(claims_for(oid=OTHER))}.{signature}"))


@pytest.mark.parametrize("header", [
    {"alg": "none", "kid": "k"}, {"alg": "HS256", "kid": "k"}, {"alg": "RS512", "kid": "k"},
    {"alg": "ES256", "kid": "k"}, {"alg": "RS256"}, {"alg": "RS256", "kid": ""}, {"alg": "RS256", "kid": 7},
    {"alg": "RS256", "kid": "k" * 201}, {"kid": "k"},
])
def test_unsupported_signing_metadata_is_rejected_before_any_key_lookup(keys, header):
    assert_denied(call(client_for(ENABLED), unsigned(header)))
    assert keys.lookups == 0


def test_symmetric_and_unsigned_tokens_cannot_pass(token, keys):
    client = client_for(ENABLED)
    assert_denied(call(client, token(signing_key="s" * 64, algorithm="HS256")))
    assert_denied(call(client, jwt.encode(claims_for(), None, algorithm="none", headers={"kid": "test-key"})))
    assert keys.lookups == 0


def test_oversized_tokens_are_rejected_before_any_key_lookup(token, keys):
    client = client_for(ENABLED)
    assert_denied(call(client, "x" * 16385))
    padded = token(padding="x" * 17000)
    assert len(padded) > gateway_guard.MAX_TOKEN_LENGTH
    assert_denied(call(client, padded))
    assert keys.lookups == 0


@pytest.mark.parametrize("path", ["/health", "/health?probe=1"])
def test_exempt_paths_need_no_token_and_ignore_bad_ones(path, token):
    client = client_for(ENABLED)
    assert call(client, path=path).json() == {"status": "ok"}
    assert call(client, "garbage", path=path).status_code == 200
    assert call(client, token(oid=OTHER), path=path).status_code == 200


@pytest.mark.parametrize("path", ["/health/", "/health/extra", "/Health", "/healthz", "//health", "/data", "/echo"])
def test_only_exact_exempt_paths_are_open(path):
    assert_denied(client_for(ENABLED).get(path))


def test_exempt_paths_are_configurable_per_service():
    assert_denied(call(client_for(ENABLED, exempt=frozenset({"/healthz", "/ready"})), path="/health"))
    assert_denied(call(client_for(ENABLED, exempt=frozenset()), path="/health"))
    assert call(client_for(ENABLED, exempt=frozenset({"/data"})), path="/data").json() == {"data": True}


def test_mounted_sub_apps_are_guarded(token):
    client = client_for(ENABLED)
    assert_denied(client.get("/mcp/"))
    assert_denied(client.post("/mcp/", json={"jsonrpc": "2.0"}))
    assert_denied(client.get("/mcp"))
    assert_denied(client.get("/mcp/", headers={HEADER: token(oid=OTHER)}))
    assert client.get("/mcp/", headers={HEADER: token()}).json() == {"mounted": True}
    assert client.post("/mcp/", headers={HEADER: "Bearer " + token()}, json={}).json() == {"mounted": True}


def test_request_bodies_reach_the_app_untouched(token):
    response = client_for(ENABLED).post("/echo", content=b"x" * 1000, headers={HEADER: token()})
    assert response.json() == {"size": 1000}


@pytest.mark.parametrize("environ,invalid", [
    (without(ENABLED, "NORTHWIND_GATEWAY_TENANT_ID"), "NORTHWIND_GATEWAY_TENANT_ID"),
    ({**ENABLED, "NORTHWIND_GATEWAY_TENANT_ID": "not-a-guid"}, "NORTHWIND_GATEWAY_TENANT_ID"),
    (without(ENABLED, "NORTHWIND_GATEWAY_AUDIENCE"), "NORTHWIND_GATEWAY_AUDIENCE"),
    ({**ENABLED, "NORTHWIND_GATEWAY_AUDIENCE": "api://not-a-guid"}, "NORTHWIND_GATEWAY_AUDIENCE"),
    ({**ENABLED, "NORTHWIND_GATEWAY_AUDIENCE": f"https://{AUDIENCE}"}, "NORTHWIND_GATEWAY_AUDIENCE"),
    ({**ENABLED, "NORTHWIND_GATEWAY_AUDIENCE": "api://"}, "NORTHWIND_GATEWAY_AUDIENCE"),
    (without(ENABLED, "NORTHWIND_GATEWAY_ALLOWED_OIDS"), "NORTHWIND_GATEWAY_ALLOWED_OIDS"),
    ({**ENABLED, "NORTHWIND_GATEWAY_ALLOWED_OIDS": " , "}, "NORTHWIND_GATEWAY_ALLOWED_OIDS"),
    ({**ENABLED, "NORTHWIND_GATEWAY_ALLOWED_OIDS": f"{APIM},not-a-guid"}, "NORTHWIND_GATEWAY_ALLOWED_OIDS"),
    ({FLAG: "true"}, "NORTHWIND_GATEWAY_TENANT_ID"),
])
def test_enabled_but_misconfigured_fails_closed_yet_keeps_health_open(token, keys, caplog, environ, invalid):
    with caplog.at_level(logging.DEBUG):
        client = client_for(environ)
        assert client.get("/health").json() == {"status": "ok"}
        assert_denied(call(client, token()), 503, "gateway_guard_misconfigured")
        assert_denied(call(client), 503, "gateway_guard_misconfigured")
        assert_denied(client.get("/mcp/", headers={HEADER: token()}), 503, "gateway_guard_misconfigured")
        refused(client, {HEADER: token()})
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1 and invalid in errors[0].getMessage()
    assert keys.lookups == 0


def test_misconfiguration_error_names_the_settings_but_never_their_values(caplog):
    with caplog.at_level(logging.DEBUG):
        GatewayGuardMiddleware(None, environ={**ENABLED, "NORTHWIND_GATEWAY_TENANT_ID": "private-looking-value"})
    (record,) = caplog.records
    message = record.getMessage()
    assert record.levelno == logging.ERROR and "NORTHWIND_GATEWAY_TENANT_ID" in message
    assert all(secret not in message for secret in ("private-looking-value", AUDIENCE, APIM))


@pytest.mark.parametrize("error", [
    jwt.PyJWKClientConnectionError("https://private.invalid/keys unreachable"),
    OSError("private network detail"), TimeoutError("private timeout"),
])
def test_signing_key_outage_is_503_without_leaking_details(token, monkeypatch, error):
    def unavailable(_token):
        raise error
    monkeypatch.setattr(gateway_guard, "signing_client", lambda _: SimpleNamespace(get_signing_key_from_jwt=unavailable))
    response = call(client_for(ENABLED), token())
    assert_denied(response, 503, "gateway_identity_unavailable")
    assert "private" not in response.text


def test_unknown_signing_key_is_a_rejection_not_an_outage(token, monkeypatch):
    def unknown(_token):
        raise jwt.PyJWKClientError("Unable to find a signing key that matches")
    monkeypatch.setattr(gateway_guard, "signing_client", lambda _: SimpleNamespace(get_signing_key_from_jwt=unknown))
    assert_denied(call(client_for(ENABLED), token()))


@pytest.mark.parametrize("value", [None, "", "false", "False", "0", "1", "yes", "on", "truee", "garbage"])
def test_guard_is_a_complete_passthrough_unless_explicitly_enabled(monkeypatch, value):
    monkeypatch.setattr(gateway_guard, "signing_client", lambda _: pytest.fail("No key lookup while disabled"))
    environ = without(ENABLED, FLAG)
    if value is not None:
        environ[FLAG] = value
    client = client_for(environ)
    assert call(client).json() == {"data": True}
    assert call(client, "garbage").status_code == 200
    assert client.get("/mcp/").json() == {"mounted": True}
    assert client.post("/echo", content=b"abc").json() == {"size": 3}
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_text() == "connected"


def test_empty_environment_is_a_passthrough():
    assert call(client_for({})).json() == {"data": True}


def test_disabled_guard_ignores_broken_settings_and_logs_nothing_for_unset_or_false(caplog):
    with caplog.at_level(logging.DEBUG):
        GatewayGuardMiddleware(None, environ={"NORTHWIND_GATEWAY_TENANT_ID": "broken"})
        GatewayGuardMiddleware(None, environ={FLAG: "false"})
    assert not caplog.records


def test_unrecognised_flag_warns_once_with_control_characters_escaped(caplog):
    with caplog.at_level(logging.DEBUG):
        guard = GatewayGuardMiddleware(None, environ={**ENABLED, FLAG: "ye\ns"})
    (record,) = caplog.records
    assert not guard.enabled and record.levelno == logging.WARNING
    assert "ye\\ns" in record.getMessage() and "\n" not in record.getMessage()


def test_process_environment_is_used_when_no_mapping_is_injected(monkeypatch, token):
    for name, value in ENABLED.items():
        monkeypatch.setenv(name, value)
    client = client_for(None)
    assert_denied(call(client))
    assert call(client, token()).status_code == 200
    monkeypatch.delenv(FLAG)
    assert call(client_for(None)).status_code == 200


@pytest.mark.parametrize("headers", [None, {HEADER: "garbage"}, {HEADER: "Bearer"}])
def test_websocket_handshake_is_refused_before_accept_without_a_token(headers):
    refused(client_for(ENABLED), headers)


@pytest.mark.parametrize("changes", [{"oid": OTHER}, {"aud": OTHER}, {"exp": 1}, {"scp": "x"}])
def test_websocket_handshake_is_refused_for_invalid_tokens(token, changes):
    refused(client_for(ENABLED), {HEADER: token(**changes)})


def test_websocket_with_a_valid_token_is_accepted(token):
    client = client_for(ENABLED)
    with client.websocket_connect("/ws", headers={HEADER: token()}) as socket:
        assert socket.receive_text() == "connected"
    with client.websocket_connect("/ws", headers={HEADER: "Bearer " + token()}) as socket:
        assert socket.receive_text() == "connected"


def test_websocket_handshake_is_refused_during_a_signing_key_outage(token, monkeypatch):
    def unavailable(_token):
        raise jwt.PyJWKClientConnectionError("down")
    monkeypatch.setattr(gateway_guard, "signing_client", lambda _: SimpleNamespace(get_signing_key_from_jwt=unavailable))
    refused(client_for(ENABLED), {HEADER: token()})


def test_lifespan_runs_even_when_the_guard_is_enabled_and_misconfigured():
    events = []

    @asynccontextmanager
    async def lifespan(_app):
        events.append("startup")
        yield
        events.append("shutdown")

    with TestClient(build_app({FLAG: "true"}, lifespan=lifespan)):
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]


@pytest.mark.asyncio
async def test_exempt_and_non_request_scopes_reach_the_app_untouched():
    seen = []

    async def app(scope, receive, send):
        seen.append((scope, receive, send))

    async def receive():
        raise AssertionError("The guard must not read request bodies")

    async def send(_message):
        raise AssertionError("The guard must not answer these requests")

    guard = GatewayGuardMiddleware(app, exempt_paths=frozenset({"/health"}), environ={FLAG: "true"})
    scopes = [
        {"type": "lifespan"}, {"type": "http", "path": "/health", "headers": []},
        {"type": "websocket", "path": "/health", "headers": []},
    ]
    for scope in scopes:
        await guard(scope, receive, send)
    assert len(seen) == len(scopes)
    assert all(got[0] is scope and got[1] is receive and got[2] is send for got, scope in zip(seen, scopes))


@pytest.mark.asyncio
async def test_rejections_never_read_the_request_body_or_reach_the_app():
    sent = []

    async def app(_scope, _receive, _send):
        raise AssertionError("The application must not run")

    async def receive():
        raise AssertionError("The guard must not read request bodies")

    async def send(message):
        sent.append(message)

    guard = GatewayGuardMiddleware(app, exempt_paths=frozenset({"/health"}), environ=ENABLED)
    scope = {"type": "http", "method": "POST", "path": "/echo", "headers": [(b"content-length", b"9")]}
    await guard(scope, receive, send)
    start, body = sent
    headers = dict(start["headers"])
    assert start["type"] == "http.response.start" and start["status"] == 401
    assert headers[b"www-authenticate"] == b"Bearer" and headers[b"cache-control"] == b"no-store"
    assert headers[b"content-type"] == b"application/json"
    assert json.loads(body["body"]) == REJECTED


@pytest.mark.asyncio
async def test_websocket_rejection_sends_close_before_any_accept():
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        raise AssertionError("The guard must not wait for the client")

    guard = GatewayGuardMiddleware(None, environ=ENABLED)
    await guard({"type": "websocket", "path": "/ws", "headers": []}, receive, send)
    assert [(message["type"], message["code"]) for message in sent] == [("websocket.close", 1008)]


@pytest.mark.asyncio
async def test_accepted_requests_pass_through_untouched_after_an_off_loop_key_lookup(token, keys):
    lookup_threads = []
    original = keys.get_signing_key_from_jwt

    def lookup(raw):
        lookup_threads.append(threading.get_ident())
        return original(raw)

    keys.get_signing_key_from_jwt = lookup
    seen = []

    async def app(scope, receive, send):
        seen.append((scope, receive, send))

    scope = {"type": "http", "path": "/data", "headers": [(b"x-northwind-gateway-token", token().encode())]}
    await GatewayGuardMiddleware(app, environ=ENABLED)(scope, "receive", "send")
    assert len(seen) == 1 and seen[0][0] is scope and seen[0][1:] == ("receive", "send")
    assert lookup_threads and lookup_threads[0] != threading.get_ident()


def test_tokens_and_their_parts_are_never_logged(token, keys, caplog):
    client = client_for(ENABLED)
    rejected = token(oid=OTHER)
    original = keys.get_signing_key_from_jwt

    def lookup(raw):
        kid = jwt.get_unverified_header(raw)["kid"]
        if kid == "unknown-key-id":
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return original(raw)

    keys.get_signing_key_from_jwt = lookup
    with caplog.at_level(logging.DEBUG):
        assert_denied(call(client, rejected))
        assert_denied(call(client, "Bearer eyJsecret.payload.signature"))
        assert_denied(call(client, token(signing_key="s" * 64, algorithm="HS256")))
        assert_denied(client.get("/data", headers=[(HEADER, rejected), (HEADER, rejected)]))
        assert_denied(call(client, token(kid="unknown-key-id")))
        assert call(client, token()).status_code == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "principal not allowed" in logged
    leaks = (*rejected.split("."), "eyJsecret", "payload", "signature", "unknown-key-id")
    assert not any(part in logged for part in leaks)


def test_signing_client_is_cached_per_tenant_and_uses_the_tenant_jwks_endpoint():
    gateway_guard.signing_client.cache_clear()
    client = gateway_guard.signing_client(TENANT)
    assert isinstance(client, gateway_guard._ThrottledJWKClient)
    assert gateway_guard.signing_client(TENANT) is client
    assert gateway_guard.signing_client(OTHER) is not client
    assert client.uri == f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
    assert client.timeout == 5


class FakeJWKS:
    """Microsoft's key endpoint behind PyJWT's real client: only the HTTP exchange is simulated."""

    def __init__(self):
        self.published = []
        self.fetches = 0
        self.outage = False
        self._lock = threading.Lock()

    def publish(self, private, kid):
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
        self.published.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})

    def build_opener(self, *_handlers):
        return SimpleNamespace(open=self.open)

    def open(self, _request, timeout=None):
        with self._lock:
            self.fetches += 1
        if self.outage:
            raise urllib.error.URLError("simulated outage")
        return io.BytesIO(json.dumps({"keys": self.published}).encode())


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def endpoint(private_key, monkeypatch):
    jwks = FakeJWKS()
    jwks.publish(private_key, "test-key")
    monkeypatch.setattr(urllib.request, "build_opener", jwks.build_opener)
    gateway_guard.signing_client.cache_clear()
    yield jwks
    gateway_guard.signing_client.cache_clear()


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(gateway_guard, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


@pytest.fixture
def own_throttle(endpoint, clock):
    """PyJWT's built-in refresh cooldown runs on the real clock; switch it off to exercise the guard's."""
    gateway_guard.signing_client(TENANT).cooldown_duration = 0


def unknown_key_flood(client, sign, count=60):
    return [call(client, sign(kid=f"unknown-{number}")) for number in range(count)]


def assert_all_rejected(responses):
    assert all(response.status_code == 401 and response.json() == REJECTED for response in responses)


def test_jwks_unknown_key_ids_force_at_most_one_refresh_per_window(private_key, endpoint, clock, own_throttle, caplog):
    assert gateway_guard.MIN_REFRESH_SECONDS == 60
    sign = functools.partial(make_token, private_key)
    client = client_for(ENABLED)
    assert call(client, sign()).status_code == 200
    assert endpoint.fetches == 1
    with caplog.at_level(logging.DEBUG):
        assert_all_rejected(unknown_key_flood(client, sign))
        assert endpoint.fetches == 2
        clock.advance(59)
        assert_all_rejected(unknown_key_flood(client, sign))
        assert endpoint.fetches == 2
        clock.advance(1)
        assert_all_rejected(unknown_key_flood(client, sign))
        assert endpoint.fetches == 3
        assert_all_rejected(unknown_key_flood(client, sign))
        assert endpoint.fetches == 3
    logged = "\n".join(record.getMessage() for record in caplog.records if record.name == gateway_guard.logger.name)
    assert "_RefreshThrottled" in logged and "unknown-" not in logged


def test_jwks_rotated_key_is_picked_up_by_the_next_allowed_refresh(private_key, endpoint, clock, own_throttle):
    sign = functools.partial(make_token, private_key)
    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sign_rotated = functools.partial(make_token, rotated_key, kid="rotated-key")
    client = client_for(ENABLED)
    assert call(client, sign()).status_code == 200
    assert_denied(call(client, sign(kid="unknown")))
    assert endpoint.fetches == 2
    endpoint.publish(rotated_key, "rotated-key")
    assert_denied(call(client, sign_rotated()))
    assert endpoint.fetches == 2
    clock.advance(61)
    assert call(client, sign_rotated()).status_code == 200
    assert endpoint.fetches == 3
    assert call(client, sign_rotated()).status_code == 200
    assert call(client, sign()).status_code == 200
    assert endpoint.fetches == 3


def test_jwks_known_key_ids_never_refetch(private_key, endpoint, clock, own_throttle):
    sign = functools.partial(make_token, private_key)
    client = client_for(ENABLED)
    for _ in range(50):
        assert call(client, sign()).status_code == 200
        clock.advance(61)
    assert endpoint.fetches == 1


def test_jwks_failed_refresh_counts_as_an_attempt_and_stays_a_503(private_key, endpoint, clock, own_throttle):
    sign = functools.partial(make_token, private_key)
    client = client_for(ENABLED)
    assert call(client, sign()).status_code == 200
    endpoint.outage = True
    assert_denied(call(client, sign(kid="unknown")), 503, "gateway_identity_unavailable")
    assert endpoint.fetches == 2
    assert_all_rejected(unknown_key_flood(client, sign))
    assert call(client, sign()).status_code == 200
    assert endpoint.fetches == 2
    clock.advance(61)
    assert_denied(call(client, sign(kid="unknown")), 503, "gateway_identity_unavailable")
    assert endpoint.fetches == 3
    assert_all_rejected(unknown_key_flood(client, sign))
    assert endpoint.fetches == 3
    endpoint.outage = False
    clock.advance(61)
    assert_denied(call(client, sign(kid="unknown")))
    assert endpoint.fetches == 4


def test_jwks_first_request_with_an_empty_cache_still_fetches(private_key, endpoint, clock, own_throttle):
    assert endpoint.fetches == 0
    assert call(client_for(ENABLED), make_token(private_key)).status_code == 200
    assert endpoint.fetches == 1


def test_jwks_first_fetch_failure_is_a_503_and_the_next_request_fetches_again(private_key, endpoint, clock, own_throttle):
    sign = functools.partial(make_token, private_key)
    client = client_for(ENABLED)
    endpoint.outage = True
    assert_denied(call(client, sign()), 503, "gateway_identity_unavailable")
    endpoint.outage = False
    assert call(client, sign()).status_code == 200
    assert endpoint.fetches == 2


def test_jwks_cold_flood_with_the_default_client_costs_at_most_one_refresh(private_key, endpoint):
    assert_all_rejected(unknown_key_flood(client_for(ENABLED), functools.partial(make_token, private_key)))
    assert 1 <= endpoint.fetches <= 2


class SlowFloat(float):
    """A timestamp whose comparison dawdles, so threads overlap between reading and recording an attempt."""

    def __sub__(self, other):
        time.sleep(0.02)
        return float(self) - other


def test_jwks_concurrent_refreshes_cannot_all_refetch(endpoint, clock, own_throttle, monkeypatch):
    client = gateway_guard.signing_client(TENANT)
    client.get_signing_keys()
    client.get_signing_keys(refresh=True)
    assert endpoint.fetches == 2
    clock.advance(100)
    monkeypatch.setattr(gateway_guard, "time", SimpleNamespace(monotonic=lambda: SlowFloat(clock.now)))
    barrier = threading.Barrier(8)
    outcomes = []

    def refresh():
        barrier.wait()
        try:
            client.get_signing_keys(refresh=True)
            outcomes.append("refreshed")
        except jwt.PyJWKClientError:
            outcomes.append("throttled")

    workers = [threading.Thread(target=refresh) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    assert sorted(outcomes) == ["refreshed"] + ["throttled"] * 7
    assert endpoint.fetches == 3


def load_mcp(service):
    spec = importlib.util.spec_from_file_location(
        "gateway_test_" + service.replace("-", "_"), ROOT / "apps" / service / "app" / "main.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def deployed_stack(application, monkeypatch):
    """The real middleware stack, built after the environment is set as it would be in a container."""
    for name, value in ENABLED.items():
        monkeypatch.setenv(name, value)
    transport = httpx.ASGITransport(app=application.build_middleware_stack())
    async with httpx.AsyncClient(transport=transport, base_url="http://unit.test") as client:
        yield client


@pytest.mark.asyncio
async def test_api_app_registers_the_guard_outermost_and_exempts_only_health(monkeypatch, token):
    from app import main

    registered = main.app.user_middleware[0]
    assert registered.cls is GatewayGuardMiddleware
    assert registered.kwargs == {"exempt_paths": frozenset({"/health"})}
    async with deployed_stack(main.app, monkeypatch) as client:
        assert (await client.get("/health")).status_code == 200
        blocked = await client.get("/safety/attacks")
        assert blocked.status_code == 401 and blocked.json() == REJECTED
        assert (await client.post("/hitl/decide", json={})).status_code == 401
        allowed = await client.get("/safety/attacks", headers={HEADER: token()})
        assert allowed.status_code == 200 and "attacks" in allowed.json()


@pytest.mark.asyncio
async def test_api_app_is_unchanged_while_the_guard_is_not_enabled(monkeypatch):
    from app import main

    monkeypatch.delenv(FLAG, raising=False)
    transport = httpx.ASGITransport(app=main.app.build_middleware_stack())
    async with httpx.AsyncClient(transport=transport, base_url="http://unit.test") as client:
        assert (await client.get("/safety/attacks")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["mcp-tech-lookup", "mcp-work-order"])
async def test_mcp_apps_guard_the_mounted_transport_and_exempt_only_healthz(service, monkeypatch):
    module = load_mcp(service)
    registered = module.app.user_middleware[0]
    assert registered.cls.__name__ == "GatewayGuardMiddleware"
    assert registered.kwargs == {"exempt_paths": frozenset({"/healthz"})}
    async with deployed_stack(module.app, monkeypatch) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        blocked = await client.post("/mcp/", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert blocked.status_code == 401 and blocked.json() == REJECTED
