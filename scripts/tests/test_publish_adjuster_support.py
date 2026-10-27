import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("publish_adjuster_support", ROOT / "scripts" / "publish-adjuster-support.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


def test_preview_is_offline_and_publishes_only_adjuster_procedures(monkeypatch):
    monkeypatch.setattr(publisher.httpx, "Client", lambda **_: pytest.fail("Preview must not create network clients"))
    monkeypatch.setattr(Path, "write_text", lambda *_args, **_kwargs: pytest.fail("Sources must not be overwritten"))
    result = publisher.publish()
    assert result["mode"] == "preview" and result["categories"] == ["adjuster"]
    assert result["index"] == "nw-insurance-adjuster-support"
    docs = publisher.documents()
    assert sorted(d["id"] for d in docs) == [
        "ADJ-DUTY-001", "ADJ-ESCALATION-001", "ADJ-EVIDENCE-001", "ADJ-PRIVACY-001"]
    assert all(d["category"] == "adjuster" and d["version"] == "1" for d in docs)
    assert all(d["synthetic"] and d["publisher"] == publisher.OWNER
               and d["source_uri"].startswith("urn:northwind:synthetic:adjuster-support:") for d in docs)


@pytest.fixture
def mock_search(monkeypatch):
    monkeypatch.setenv("ADJUSTER_SUPPORT_PUBLISH_AUTH", "admin_key")
    monkeypatch.setenv("ADJUSTER_SUPPORT_SEARCH_ADMIN_KEY", "explicit-memory-only")
    calls = []
    def install(existing=None, foreign=False, incomplete=False):
        def handle(request):
            calls.append(request)
            if request.method == "GET":
                return httpx.Response(404 if existing is None else 200, json=existing)
            body = json.loads(request.content)
            if request.method == "PUT":
                assert request.headers["if-none-match"] == "*"
                return httpx.Response(201, json=body)
            if request.url.path.endswith("/docs/search"):
                return httpx.Response(200, json={"value": [{"id": "foreign"}] if foreign else []})
            assert request.url.path.endswith("/docs/index")
            return httpx.Response(200, json={"value": [] if incomplete else [
                {"key": doc["id"], "status": True, "statusCode": 201} for doc in body["value"]
            ]})
        original = httpx.Client
        monkeypatch.setattr(publisher.httpx, "Client",
                            lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
        return calls
    return install


def test_explicit_apply_only_creates_fresh_owned_index(mock_search):
    calls = mock_search()
    assert publisher.publish(apply=True)["mode"] == "applied"
    assert [request.method for request in calls] == ["GET", "PUT", "POST"]
    assert all(request.url.host == "srch-zurlo-northwind.search.windows.net" for request in calls)
    assert all("/indexes/nw-insurance-adjuster-support" in request.url.path for request in calls)
    assert all(request.method != "DELETE" for request in calls)


@pytest.mark.parametrize("corruption", ["owner", "schema", "foreign"])
def test_existing_index_ownership_guard_blocks_writes(mock_search, corruption):
    existing = publisher.index_body()
    if corruption == "owner":
        existing["description"] = "someone-else"
    elif corruption == "schema":
        existing["fields"] = []
    calls = mock_search(existing, foreign=corruption == "foreign")
    with pytest.raises(RuntimeError, match="no writes performed"):
        publisher.publish(apply=True)
    assert not any(request.method in ("PUT", "DELETE") or request.url.path.endswith("/docs/index") for request in calls)


def test_partial_upload_is_not_reported_as_success(mock_search):
    mock_search(incomplete=True)
    with pytest.raises(RuntimeError, match="incomplete"):
        publisher.publish(apply=True)


def test_explicit_auth_does_not_fall_back_or_fetch_keys(monkeypatch):
    monkeypatch.setenv("ADJUSTER_SUPPORT_PUBLISH_AUTH", "admin_key")
    monkeypatch.delenv("ADJUSTER_SUPPORT_SEARCH_ADMIN_KEY", raising=False)
    with pytest.raises(ValueError, match="no automatic key lookup"):
        publisher.publish(apply=True)


@pytest.mark.parametrize("error", [
    RuntimeError("Adjuster Search POST /indexes/owned/docs/index failed (HTTP 403); no secret details logged."),
    RuntimeError("Existing support index contains foreign documents; no writes performed."),
    ValueError("Explicit publisher admin key is missing; no automatic key lookup or fallback."),
    FileNotFoundError("Canonical source file is missing."),
])
def test_cli_preserves_actionable_safe_error_classes_and_messages(monkeypatch, capsys, error):
    monkeypatch.setattr(sys, "argv", ["publish-adjuster-support.py", "--apply"])
    def fail(**kwargs):
        assert kwargs == {"apply": True}
        raise error
    monkeypatch.setattr(publisher, "publish", fail)
    assert publisher.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert type(error).__name__ in output.err and str(error) in output.err
    assert '"mode": "applied"' not in output.err


@pytest.mark.parametrize("kind", ["http_status", "transport", "authentication"])
def test_cli_exposes_safe_status_without_server_bodies_headers_or_secrets(monkeypatch, capsys, kind):
    request = httpx.Request("POST", "https://search.unit.test/private?api-key=secret-key",
                            headers={"Authorization": "Bearer secret-token"})
    if kind == "http_status":
        error = httpx.HTTPStatusError(
            "server body and api-key=secret-key", request=request,
            response=httpx.Response(403, request=request, json={"error": "secret-server-body"}),
        )
    elif kind == "transport":
        error = httpx.ConnectError("Authorization: Bearer secret-token; api-key=secret-key", request=request)
    else:
        class AuthenticationFailure(Exception):
            status_code = 403
        error = AuthenticationFailure("secret-server-body; Bearer secret-token")
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(publisher, "publish", fail)
    monkeypatch.setattr(sys, "argv", ["publish-adjuster-support.py", "--apply"])
    assert publisher.main() == 1
    output = capsys.readouterr()
    assert output.out == "" and type(error).__name__ in output.err
    if kind != "transport":
        assert "HTTP 403" in output.err
    assert all(secret not in output.err for secret in
               ("secret-key", "secret-token", "secret-server-body", "Authorization", "https://"))


def test_preflight_failure_includes_http_status_without_writes(monkeypatch):
    monkeypatch.setenv("ADJUSTER_SUPPORT_PUBLISH_AUTH", "admin_key")
    monkeypatch.setenv("ADJUSTER_SUPPORT_SEARCH_ADMIN_KEY", "memory-only")
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(403, json={"error": "private server response"})
    original = httpx.Client
    monkeypatch.setattr(publisher.httpx, "Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(RuntimeError, match="preflight GET failed \\(HTTP 403\\)"):
        publisher.publish(apply=True)
    assert [request.method for request in calls] == ["GET"]
