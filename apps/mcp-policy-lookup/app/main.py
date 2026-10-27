# Copyright (c) Microsoft. All rights reserved.
"""Northwind insurance policy/claim lookup MCP server.

Exposes the read-only MCP tools `lookup_policy` and `lookup_claim` over the
streamable-HTTP transport that Azure AI Foundry connects to when an agent has
a `type: mcp` tool wired to this Container App's URL. Every call shows up as a
first-class MCP tool call in Foundry Observability + App Insights.

The records are canonical synthetic fixtures (data/northwind/insurance-fixtures.json);
nothing here can decide coverage, approve a claim or execute a payment.

No auth by default — this is a demo server behind a public Container Apps ingress.
Setting NORTHWIND_GATEWAY_AUTH_REQUIRED=true (see gateway_guard.py) makes it accept
only calls relayed by the API gateway.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
if not __package__:
    from apps.api.app.insurance_domain import lookup_claim_record, lookup_policy_record
else:
    try:
        from .insurance_domain import lookup_claim_record, lookup_policy_record
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.insurance_domain":
            raise
        from apps.api.app.insurance_domain import lookup_claim_record, lookup_policy_record
if not __package__:
    from apps.api.app.gateway_guard import GatewayGuardMiddleware
else:
    try:
        from .gateway_guard import GatewayGuardMiddleware
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.gateway_guard":
            raise
        from apps.api.app.gateway_guard import GatewayGuardMiddleware

# Optional App Insights wiring — mirrors apps/api. Configured only when the
# usual OTEL env vars are present so local `uvicorn` runs stay quiet.
if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"],
    )


# DNS-rebinding protection is on by default in FastMCP's streamable-HTTP
# transport, which rejects the Host header ACA ingress passes through and
# returns 421. We're a public no-auth demo endpoint, so disable it.
mcp = FastMCP(
    name="northwind-insurance-policy-claim-lookup",
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def lookup_policy(policy_id: str, policyholder_id: str | None = None,
                  loss_date: str | None = None, state: str | None = None) -> str:
    """Read a synthetic policy and its authoritative fictional wording/citations.

    Supply exact POL and optionally PH IDs, ISO loss_date and two-letter state.
    Unknown IDs, mismatches and inapplicable wording raise errors; never decide coverage.
    """
    return json.dumps(lookup_policy_record(policy_id, policyholder_id, loss_date, state))


@mcp.tool()
def lookup_claim(claim_id: str, policy_id: str | None = None,
                 policyholder_id: str | None = None) -> str:
    """Read an exact synthetic CLM ID with verified linked policy and policyholder.

    Returns incident facts, outstanding evidence and effective, jurisdiction-specific
    fictional policy citations. Unknown or mismatched IDs raise surfaced errors.
    This read-only tool cannot approve or deny a claim.
    """
    return json.dumps(lookup_claim_record(claim_id, policy_id, policyholder_id))


# FastAPI wrapper so the Container App health probe has something to hit
# and so we mount the MCP streamable-HTTP transport at /mcp. FastMCP's
# streamable-HTTP transport needs its session_manager task group started
# via lifespan — see https://github.com/modelcontextprotocol/python-sdk
# (RuntimeError: "Task group is not initialized" otherwise).
@asynccontextmanager
async def _lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="northwind-insurance-lookup-mcp", lifespan=_lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# FastMCP.streamable_http_app() returns a Starlette app that serves the
# MCP streamable-HTTP transport at its root (we set streamable_http_path="/"
# above). Mounting at /mcp gives us the endpoint URL Foundry expects:
# https://<fqdn>/mcp.
app.mount("/mcp", mcp.streamable_http_app())

# Registered last so it is the outermost middleware (it also covers the mounted MCP
# transport). A no-op unless NORTHWIND_GATEWAY_AUTH_REQUIRED=true; see gateway_guard.py.
app.add_middleware(GatewayGuardMiddleware, exempt_paths=frozenset({"/healthz"}))
