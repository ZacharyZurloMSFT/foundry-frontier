"""Unit tests for scripts/apim-smoke.py using an in-memory fake gateway (no network)."""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("apim_smoke", ROOT / "scripts" / "apim-smoke.py")
smoke = importlib.util.module_from_spec(_spec)
sys.modules["apim_smoke"] = smoke  # dataclasses resolve string annotations through sys.modules
_spec.loader.exec_module(smoke)

GATEWAY = "https://apim.example.test"
KEY = "k" * 32
SSE_INIT = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}\n\n'


def sse_tools(kind: str) -> str:
    tools = [{"name": name} for name in sorted(smoke.MCP_TOOLS[kind])]
    return "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}) + "\n\n"


class FakeGateway:
    """Behaves like a correctly configured gateway unless a flag breaks one behaviour."""

    def __init__(self, **broken):
        self.broken = broken

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, origin = request.url.path, request.headers.get("origin")
        allowed = {f"https://{app}.{smoke.ACA_SUFFIX}" for apps in smoke.WEB_APPS.values() for app in apps}
        cors = {}
        if origin and (origin in allowed or self.broken.get("reflect_foreign")):
            cors["access-control-allow-origin"] = origin
        if self.broken.get("duplicate_cors") and origin in allowed:
            return httpx.Response(200, json={"status": "ok"}, headers=[("access-control-allow-origin", origin), ("access-control-allow-origin", "*")])
        if request.method == "OPTIONS":
            cors["access-control-allow-headers"] = request.headers.get("access-control-request-headers", "")
            return httpx.Response(200, headers=cors)
        if path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok"}, headers=cors)
        if path.endswith("/company"):
            return httpx.Response(200, json={"slug": "northwind"})
        if "/departments/" in path:
            return httpx.Response(404, json={"detail": "unknown department"})
        if path.endswith("/api/v1/me"):
            return httpx.Response(self.broken.get("business_status", 401), json={})
        if "/mcp/" in path and request.method == "POST":
            kind = path.rsplit("/", 1)[-1]
            key = request.headers.get("ocp-apim-subscription-key")
            if self.broken.get("open_mcp"):
                key = KEY
            if key != KEY:
                return httpx.Response(401, json={"message": "Access denied"})
            method = json.loads(request.content)["method"]
            body = SSE_INIT.replace("2025-06-18", self.broken.get("protocol", "2025-06-18")) if method == "initialize" else sse_tools(kind)
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"message": "Resource not found"})


def args(**overrides):
    base = dict(gateway_url=GATEWAY, stack="northwind", expect_lockdown=False, stream_check=False,
                aca_suffix=smoke.ACA_SUFFIX, timeout=5.0)
    return SimpleNamespace(**{**base, **overrides})


def run(gateway, monkeypatch, *, ws=(403, 101), environ=None, **overrides):
    codes = iter(ws)
    monkeypatch.setattr(smoke, "websocket_handshake", lambda url, origin, timeout=20: next(codes))
    with httpx.Client(transport=httpx.MockTransport(gateway)) as client:
        return smoke.run(args(**overrides), client, environ if environ is not None else {"NORTHWIND_MCP_KEY_NORTHWIND": KEY})


def failed(report):
    return {name for name, status, _ in report.failed}


def test_a_correct_gateway_passes_every_check(monkeypatch):
    report = run(FakeGateway(), monkeypatch)
    assert not report.failed, report.markdown()
    assert sum(status == "PASS" for _, status, _ in report.rows) >= 14


def test_both_stacks_are_checked_by_default(monkeypatch):
    codes = iter([403, 101, 403, 101])
    monkeypatch.setattr(smoke, "websocket_handshake", lambda url, origin, timeout=20: next(codes))
    with httpx.Client(transport=httpx.MockTransport(FakeGateway())) as client:
        report = smoke.run(args(stack="all"), client, {"NORTHWIND_MCP_KEY_NORTHWIND": KEY, "NORTHWIND_MCP_KEY_INSURANCE": KEY})
    assert not report.failed
    assert {name.split()[0] for name, _, _ in report.rows} == {"northwind", "insurance"}


def test_duplicate_cors_headers_fail(monkeypatch):
    report = run(FakeGateway(duplicate_cors=True), monkeypatch)
    assert any("single Access-Control-Allow-Origin" in name for name in failed(report))


def test_foreign_origin_reflection_fails(monkeypatch):
    report = run(FakeGateway(reflect_foreign=True), monkeypatch)
    assert any("foreign origin" in name for name in failed(report))


def test_unpublished_business_route_fails_but_disabled_backend_is_fine(monkeypatch):
    assert any("/api/v1" in name for name in failed(run(FakeGateway(business_status=404), monkeypatch)))
    assert not run(FakeGateway(business_status=503), monkeypatch).failed


def test_open_mcp_endpoint_fails(monkeypatch):
    report = run(FakeGateway(open_mcp=True), monkeypatch)
    assert any("requires a subscription key" in name for name in failed(report))
    assert any("rejects a wrong key" in name for name in failed(report))


def test_old_mcp_protocol_fails(monkeypatch):
    report = run(FakeGateway(protocol="2024-11-05"), monkeypatch)
    assert any("initialize" in name for name in failed(report))


def test_mcp_without_key_checks_only_the_public_behaviour(monkeypatch):
    report = run(FakeGateway(), monkeypatch, environ={})
    assert not report.failed
    assert {name for name, status, _ in report.rows if status == "SKIP"} >= {
        "northwind MCP tech-lookup initialize/tools", "northwind MCP work-order initialize/tools"}


def test_websocket_outcomes(monkeypatch):
    assert any("foreign Origin" in n for n in failed(run(FakeGateway(), monkeypatch, ws=(404, 101))))
    skipped = run(FakeGateway(), monkeypatch, ws=(403, 403))
    assert not skipped.failed and any(s == "SKIP" and "WebSocket" in n for n, s, _ in skipped.rows)
    assert any("upgrades" in n for n in failed(run(FakeGateway(), monkeypatch, ws=(403, 502))))


def locked(unlocked: bool):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/health", "/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200 if unlocked else 401, json={})
    return handler


@pytest.mark.parametrize("unlocked", [False, True])
def test_lockdown_checks(monkeypatch, unlocked):
    monkeypatch.setattr(smoke, "websocket_handshake", lambda url, origin, timeout=20: 200 if unlocked else 403)
    report = smoke.Report()
    with httpx.Client(transport=httpx.MockTransport(locked(unlocked))) as client:
        smoke.check_direct_lockdown(client, "insurance", smoke.ACA_SUFFIX, report, 5)
    assert bool(report.failed) is unlocked
    assert all("stays reachable" not in name for name, _, _ in report.failed)


def test_streaming_check_detects_buffering(monkeypatch):
    def stream(delay: float):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text='{"kind":"assistant_delta"}\n{"kind":"done"}\n')
        return handler
    report = smoke.Report()
    with httpx.Client(transport=httpx.MockTransport(stream(0))) as client:
        smoke.check_streaming(client, GATEWAY + "/x", "x", report)
    assert report.rows[0][0].endswith("not buffered") and report.rows[0][1] == "FAIL"
    unavailable = smoke.Report()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
        smoke.check_streaming(client, GATEWAY + "/x", "x", unavailable)
    assert unavailable.rows[0][1] == "SKIP"


def test_parse_mcp_handles_sse_and_json():
    sse = httpx.Response(200, text=SSE_INIT, headers={"content-type": "text/event-stream"})
    assert smoke.parse_mcp(sse)["result"]["protocolVersion"] == "2025-06-18"
    assert smoke.parse_mcp(httpx.Response(200, json={"a": 1})) == {"a": 1}
    assert smoke.parse_mcp(httpx.Response(200, text="not json")) is None


@pytest.mark.parametrize("status_line,expected", [(b"HTTP/1.1 101 Switching Protocols", 101), (b"HTTP/1.1 403 Forbidden", 403)])
def test_websocket_handshake_reads_the_status(status_line, expected):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    seen = {}

    def serve():
        conn, _ = server.accept()
        seen["request"] = conn.recv(4096).decode()
        conn.sendall(status_line + b"\r\n\r\n")
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    status = smoke.websocket_handshake(f"ws://127.0.0.1:{server.getsockname()[1]}/voice/realtime", "https://app.example", 5)
    thread.join(5)
    server.close()
    assert status == expected
    assert "Origin: https://app.example" in seen["request"] and seen["request"].startswith("GET /voice/realtime HTTP/1.1")


def test_main_validates_the_gateway_url_and_exit_codes(monkeypatch, tmp_path, capsys):
    assert smoke.main(["--gateway-url", "https://apim.example.test/northwind"]) == 2
    assert smoke.main(["--gateway-url", "http://apim.example.test"]) == 2
    monkeypatch.setattr(smoke, "websocket_handshake", lambda url, origin, timeout=20: 403)
    real_client = httpx.Client
    for broken, code in ((FakeGateway(), 0), (FakeGateway(duplicate_cors=True), 1)):
        monkeypatch.setattr(smoke.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(broken)))
        out = tmp_path / "result.json"
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        assert smoke.main(["--gateway-url", GATEWAY, "--stack", "northwind", "--json", str(out)]) == code
        assert json.loads(out.read_text())[0]["check"].startswith("northwind")
        assert "## Gateway smoke test" in summary.read_text()
        summary.unlink()


def test_report_markdown_escapes_pipes():
    report = smoke.Report()
    report.check("a", True, "x|y")
    assert "x/y" in report.markdown()
