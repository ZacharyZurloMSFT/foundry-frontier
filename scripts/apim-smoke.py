"""Smoke-test the Northwind APIs through apim-northwind, and optionally prove direct access is locked down.

Read-only: no model calls unless --stream-check is given, and MCP checks only call initialize and tools/list.
Checks that need no secret always run (--public-checks keeps that explicit for CI). MCP checks that need the
product subscription key read it from NORTHWIND_MCP_KEY_<STACK> (never printed). Exit code 1 if any check fails.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

STACKS = ("insurance",)
ACA_SUFFIX = "gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io"
WEB_APPS = {"insurance": ("ca-insurance-portal", "ca-insurance-voice")}
# The MCP kinds are the live API Management identifiers (insurance-mcp-tech-lookup, insurance-mcp-work-order).
BACKENDS = {
    "insurance": {"api": "ca-insurance-api", "tech-lookup": "ca-insurance-lookup", "work-order": "ca-insurance-review"},
}
# Tools each stack's MCP servers must expose: policy and claim lookup, and the claim-action draft for human review.
MCP_TOOLS = {
    "insurance": {"tech-lookup": {"lookup_policy", "lookup_claim"}, "work-order": {"draft_claim_action"}},
}
CHAT_PATH = "/insurance/chat"  # the stack prefix is added by the gateway: <gateway>/<stack>/insurance/chat
MIN_MCP_PROTOCOL = "2025-06-18"
PREFLIGHT_HEADERS = "authorization,content-type,x-department,x-demo-run,idempotency-key,x-synthetic-data"
FOREIGN_ORIGIN = "https://evil.example"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": MIN_MCP_PROTOCOL, "capabilities": {}, "clientInfo": {"name": "northwind-smoke", "version": "1"}}}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@dataclass
class Report:
    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, "PASS" if ok else "FAIL", detail))
        return ok

    def skip(self, name: str, detail: str) -> None:
        self.rows.append((name, "SKIP", detail))

    @property
    def failed(self) -> list[tuple[str, str, str]]:
        return [row for row in self.rows if row[1] == "FAIL"]

    def markdown(self) -> str:
        lines = ["| Check | Result | Detail |", "| --- | --- | --- |"]
        lines += [f"| {name} | {status} | {detail.replace('|', '/')} |" for name, status, detail in self.rows]
        return "\n".join(lines) + "\n"


def values(response: httpx.Response, name: str) -> list[str]:
    return response.headers.get_list(name)


def parse_mcp(response: httpx.Response) -> dict | None:
    """MCP streamable HTTP answers with JSON or a single SSE event."""
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        payload = next((line[5:].strip() for line in text.splitlines() if line.startswith("data:")), "")
    else:
        payload = text
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def websocket_handshake(url: str, origin: str, timeout: float = 20.0) -> int:
    """Open a WebSocket handshake and return the HTTP status code (101 = upgraded); stdlib only."""
    parts = urlsplit(url)
    secure = parts.scheme == "wss"
    host, port = parts.hostname, parts.port or (443 if secure else 80)
    path = parts.path or "/"
    request = (f"GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\nSec-WebSocket-Version: 13\r\n"
               f"Origin: {origin}\r\n\r\n")
    with socket.create_connection((host, port), timeout) as raw:
        stream = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if secure else raw
        try:
            stream.sendall(request.encode())
            head = stream.recv(4096)
        finally:
            stream.close()
    return int(head.split(b"\r\n", 1)[0].split()[1])


def check_rest(client: httpx.Client, base: str, label: str, report: Report) -> None:
    health = client.get(f"{base}/health")
    report.check(f"{label} GET /health", health.status_code == 200 and health.json().get("status") == "ok",
                 f"HTTP {health.status_code}")
    company = client.get(f"{base}/company")
    report.check(f"{label} GET /company", company.status_code == 200 and "slug" in company.json(),
                 f"HTTP {company.status_code}")
    missing = client.get(f"{base}/departments/does-not-exist")
    report.check(f"{label} backend 404 reaches the API", missing.status_code == 404 and "detail" in missing.text,
                 f"HTTP {missing.status_code}")
    business = client.get(f"{base}/api/v1/me")
    report.check(f"{label} /api/v1 requires a delegated token", business.status_code in (401, 403, 503),
                 f"HTTP {business.status_code} (404 would mean the route is not published)")


def check_cors(client: httpx.Client, base: str, label: str, origin: str, report: Report) -> None:
    preflight = client.options(f"{base}{CHAT_PATH}", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": PREFLIGHT_HEADERS})
    allowed = {h.strip() for h in preflight.headers.get("access-control-allow-headers", "").lower().split(",")}
    report.check(f"{label} CORS preflight from the web app", preflight.status_code == 200
                 and values(preflight, "access-control-allow-origin") == [origin]
                 and set(PREFLIGHT_HEADERS.split(",")) <= allowed, f"HTTP {preflight.status_code}")
    actual = client.get(f"{base}/health", headers={"Origin": origin})
    report.check(f"{label} CORS single Access-Control-Allow-Origin",
                 values(actual, "access-control-allow-origin") == [origin],
                 f"values={values(actual, 'access-control-allow-origin')}")
    foreign = client.get(f"{base}/health", headers={"Origin": FOREIGN_ORIGIN})
    report.check(f"{label} CORS foreign origin gets no allow header",
                 not values(foreign, "access-control-allow-origin"), f"values={values(foreign, 'access-control-allow-origin')}")


def check_websocket(gateway: str, stack: str, origin: str, report: Report, timeout: float) -> None:
    host = urlsplit(gateway).netloc
    url = f"wss://{host}/{stack}/voice/realtime"
    try:
        foreign = websocket_handshake(url, FOREIGN_ORIGIN, timeout)
        good = websocket_handshake(url, origin, timeout)
    except (OSError, ValueError, IndexError) as exc:
        report.check(f"{stack} WebSocket /voice/realtime", False, f"{type(exc).__name__}: {exc}")
        return
    report.check(f"{stack} WebSocket rejects a foreign Origin", foreign == 403, f"HTTP {foreign} (404 = not published)")
    if good == 101:
        report.check(f"{stack} WebSocket upgrades for the web app Origin", True, "HTTP 101")
    elif good == 403:
        report.skip(f"{stack} WebSocket upgrades for the web app Origin", "backend refused: Voice Live is not ready on this stack")
    else:
        report.check(f"{stack} WebSocket upgrades for the web app Origin", False, f"HTTP {good}")


def mcp_call(client: httpx.Client, url: str, payload: dict, key: str | None = None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if key:
        headers["Ocp-Apim-Subscription-Key"] = key
    return client.post(url, json=payload, headers=headers)


def check_mcp(client: httpx.Client, base: str, kind: str, stack: str, key: str | None, report: Report) -> None:
    label = stack
    url = f"{base}/mcp/{kind}"
    anonymous = mcp_call(client, url, INITIALIZE)
    report.check(f"{label} MCP {kind} requires a subscription key", anonymous.status_code == 401, f"HTTP {anonymous.status_code}")
    wrong = mcp_call(client, url, INITIALIZE, "0" * 32)
    report.check(f"{label} MCP {kind} rejects a wrong key", wrong.status_code == 401, f"HTTP {wrong.status_code}")
    if not key:
        report.skip(f"{label} MCP {kind} initialize/tools", "no subscription key supplied")
        return
    init = mcp_call(client, url, INITIALIZE, key)
    body = parse_mcp(init) or {}
    version = (body.get("result") or {}).get("protocolVersion", "")
    report.check(f"{label} MCP {kind} initialize", init.status_code == 200 and version >= MIN_MCP_PROTOCOL,
                 f"HTTP {init.status_code} protocolVersion={version or '?'}")
    tools = mcp_call(client, url, TOOLS_LIST, key)
    names = {t.get("name") for t in ((parse_mcp(tools) or {}).get("result") or {}).get("tools", [])}
    expected = MCP_TOOLS[stack][kind]
    report.check(f"{label} MCP {kind} tools/list", expected <= names,
                 f"tools={sorted(n for n in names if n)}" + ("" if expected <= names else f" missing={sorted(expected - names)}"))


def check_direct_lockdown(client: httpx.Client, stack: str, aca_suffix: str, report: Report, timeout: float) -> None:
    api = f"https://{BACKENDS[stack]['api']}.{aca_suffix}"
    health = client.get(f"{api}/health")
    report.check(f"{stack} direct /health stays reachable (probes)", health.status_code == 200, f"HTTP {health.status_code}")
    direct = client.get(f"{api}/company")
    report.check(f"{stack} direct API call is rejected", direct.status_code in (401, 403), f"HTTP {direct.status_code}")
    spoof = client.get(f"{api}/company", headers={"X-Northwind-Gateway-Token": "a.b.c"})
    report.check(f"{stack} spoofed gateway token is rejected", spoof.status_code in (401, 403), f"HTTP {spoof.status_code}")
    for kind in ("tech-lookup", "work-order"):
        mcp = f"https://{BACKENDS[stack][kind]}.{aca_suffix}"
        probe = client.get(f"{mcp}/healthz")
        report.check(f"{stack} direct {kind} /healthz stays reachable", probe.status_code == 200, f"HTTP {probe.status_code}")
        blocked = mcp_call(client, f"{mcp}/mcp/", INITIALIZE)
        report.check(f"{stack} direct {kind} MCP is rejected", blocked.status_code in (401, 403), f"HTTP {blocked.status_code}")
    try:
        status = websocket_handshake(f"wss://{BACKENDS[stack]['api']}.{aca_suffix}/voice/realtime",
                                     f"https://{WEB_APPS[stack][1]}.{aca_suffix}", timeout)
        report.check(f"{stack} direct WebSocket is rejected", status in (401, 403), f"HTTP {status}")
    except (OSError, ValueError, IndexError) as exc:
        report.check(f"{stack} direct WebSocket is rejected", False, f"{type(exc).__name__}: {exc}")


STREAM_MIN_TOTAL = 3.0  # a response that finishes sooner cannot prove anything about buffering
STREAM_MIN_GAP = 1.0  # the first line must arrive this long before the last one


def check_streaming(client: httpx.Client, base: str, label: str, report: Report, *,
                    min_total: float = STREAM_MIN_TOTAL, min_gap: float = STREAM_MIN_GAP) -> None:
    """The insurance chat emits its session line at once and the model events seconds later, so a buffering gateway
    shows up as every line arriving together at the end."""
    name = f"{label} NDJSON streaming"
    started = time.monotonic()
    first = last = None
    lines = 0
    with client.stream("POST", f"{base}{CHAT_PATH}", json={"message": "Reply with the single word ready."}) as response:
        if response.status_code in (503, 404):
            report.skip(name, f"{CHAT_PATH} unavailable (HTTP {response.status_code})")
            return
        for line in response.iter_lines():
            if line.strip():
                lines += 1
                last = time.monotonic() - started
                first = last if first is None else first
    if response.status_code != 200 or first is None:
        report.check(f"{name} is not buffered", False, f"HTTP {response.status_code} lines={lines}")
        return
    detail = f"lines={lines} first={first:.2f}s last={last:.2f}s"
    if last < min_total:
        report.skip(name, f"{detail}: answered too fast to tell whether the gateway buffers")
        return
    report.check(f"{name} is not buffered", lines >= 2 and last - first >= min_gap, detail)


def run(args: argparse.Namespace, client: httpx.Client, environ: dict[str, str] | None = None) -> Report:
    environ = os.environ if environ is None else environ
    gateway = args.gateway_url.rstrip("/")
    report = Report()
    for stack in (STACKS if args.stack == "all" else (args.stack,)):
        base, label = f"{gateway}/{stack}", stack
        origin = f"https://{WEB_APPS[stack][0]}.{args.aca_suffix}"
        check_rest(client, base, label, report)
        check_cors(client, base, label, origin, report)
        check_websocket(gateway, stack, f"https://{WEB_APPS[stack][1]}.{args.aca_suffix}", report, args.timeout)
        key = environ.get(f"NORTHWIND_MCP_KEY_{stack.upper()}") or None
        for kind in ("tech-lookup", "work-order"):
            check_mcp(client, base, kind, label, key, report)
        if args.expect_lockdown:
            check_direct_lockdown(client, stack, args.aca_suffix, report, args.timeout)
        if args.stream_check:
            check_streaming(client, base, label, report)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--gateway-url", required=True, help="e.g. https://apim-northwind.azure-api.net")
    result.add_argument("--stack", choices=(*STACKS, "all"), default="all")
    result.add_argument("--public-checks", action="store_true", help="documentation flag: only secretless checks are guaranteed")
    result.add_argument("--expect-lockdown", action="store_true", help="also prove direct Container App calls are rejected")
    result.add_argument("--stream-check", action="store_true", help="opt in to one tiny model call per stack to prove streaming")
    result.add_argument("--aca-suffix", default=ACA_SUFFIX)
    result.add_argument("--timeout", type=float, default=60.0)
    result.add_argument("--json", dest="json_path")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    gateway = urlsplit(args.gateway_url)
    if gateway.scheme != "https" or not gateway.hostname or gateway.path.strip("/") or gateway.query or gateway.username:
        print("--gateway-url must be an https origin without path, query or credentials", file=sys.stderr)
        return 2
    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        report = run(args, client)
    print(report.markdown())
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as stream:
            json.dump([{"check": n, "result": s, "detail": d} for n, s, d in report.rows], stream, indent=2)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## Gateway smoke test\n\n" + report.markdown())
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
