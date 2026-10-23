# Copyright (c) Microsoft. All rights reserved.
"""Northwind order-lookup MCP server.

Exposes a single MCP tool, `lookup_order(query)`, over the streamable-HTTP
transport that Azure AI Foundry connects to when a prompt agent has a
`type: mcp` tool wired to this Container App's URL.

The `northwind-order-lookup` prompt agent (agents/tech_lookup.yaml) declares
`lookup_order` as its only MCP tool. Every call shows up as a first-class
MCP tool call in Foundry Observability + App Insights.

NOTE ON LEGACY NAMES: this Container App is still called
`ca-mcp-tech-lookup` and its azure.yaml service is still `mcp-tech-lookup`.
Those identifiers are wired through Bicep + azd env and renaming them
would risk breaking `azd up`. Semantically this service is the Order
Lookup MCP; keep the legacy infra name.

No auth by default — this is a demo server behind a public Container Apps ingress.
Setting NORTHWIND_GATEWAY_AUTH_REQUIRED=true (see gateway_guard.py) makes it accept
only calls relayed by the API gateway.

With NORTHWIND_MCP_INSURANCE_ONLY=true (the ca-insurance-lookup app) the server exposes
only `lookup_policy` and `lookup_claim`; the retail `lookup_order` tool is neither
registered nor callable, so the insurance project never serves retail data. Without it
(the ca-mcp-tech-lookup app, which runs in the insurance domain too) every tool is served.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
if not __package__:
    from apps.api.app.insurance_domain import lookup_claim_record, lookup_policy_record, require_insurance_domain
else:
    try:
        from .insurance_domain import lookup_claim_record, lookup_policy_record, require_insurance_domain
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.insurance_domain":
            raise
        from apps.api.app.insurance_domain import lookup_claim_record, lookup_policy_record, require_insurance_domain
if not __package__:
    from apps.api.app.gateway_guard import GatewayGuardMiddleware
else:
    try:
        from .gateway_guard import GatewayGuardMiddleware
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.gateway_guard":
            raise
        from apps.api.app.gateway_guard import GatewayGuardMiddleware

INSURANCE_ONLY = os.environ.get("NORTHWIND_MCP_INSURANCE_ONLY", "").strip().lower() == "true"

# Optional App Insights wiring — mirrors apps/api. Configured only when the
# usual OTEL env vars are present so local `uvicorn` runs stay quiet.
if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"],
    )


# Static fixture — same shape as the _ORDERS fixture in
# agents/orchestrator/main.py, kept here so this service has no external
# dependency for the demo. Swap for a Cosmos read when we're ready.
_ORDERS: list[dict[str, Any]] = [
    {
        "order_id": "NW-40021",
        "customer": {"id": "C-8801", "name": "Priya Patel",  "loyalty_tier": "gold",   "email": "priya@example.com",  "phone": "+1-206-555-0141"},
        "status":   "in_transit",
        "placed_on": "2026-09-02",
        "eta":       "2026-09-10",
        "total":     189.00,
        "items":     [{"sku": "SKU-2201", "name": "Northwind Cascade Espresso Machine", "qty": 1, "price": 189.00}],
        "shipment":  {"carrier": "NW-Freight", "tracking": "NWF9922188", "last_scan": "Chicago DC", "delivered": False},
    },
    {
        "order_id": "NW-40022",
        "customer": {"id": "C-9102", "name": "Marcus Chen",  "loyalty_tier": "silver", "email": "marcus@example.com", "phone": "+1-303-555-0178"},
        "status":   "delivered",
        "placed_on": "2026-08-15",
        "eta":       "2026-08-20",
        "total":     74.50,
        "items":     [{"sku": "SKU-1108", "name": "Northwind Cast-Iron Skillet", "qty": 1, "price": 74.50}],
        "shipment":  {"carrier": "NW-Freight", "tracking": "NWF9901133", "last_scan": "Seattle, WA", "delivered": True},
    },
    {
        "order_id": "NW-40023",
        "customer": {"id": "C-7455", "name": "Sofia Alvarez","loyalty_tier": "bronze", "email": "sofia@example.com",  "phone": "+1-312-555-0192"},
        "status":   "processing",
        "placed_on": "2026-09-08",
        "eta":       "2026-09-14",
        "total":     239.97,
        "items":     [
            {"sku": "SKU-3040", "name": "Northwind Merino Throw Blanket", "qty": 3, "price": 79.99},
        ],
        "shipment":  {"carrier": None, "tracking": None, "last_scan": None, "delivered": False},
    },
    {
        "order_id": "NW-40024",
        "customer": {"id": "C-8801", "name": "Priya Patel",  "loyalty_tier": "gold",   "email": "priya@example.com",  "phone": "+1-206-555-0141"},
        "status":   "delivered",
        "placed_on": "2026-07-18",
        "eta":       "2026-07-22",
        "total":     58.00,
        "items":     [{"sku": "SKU-4501", "name": "Northwind Ceramic Mug (set of 4)", "qty": 1, "price": 58.00}],
        "shipment":  {"carrier": "NW-Freight", "tracking": "NWF9800224", "last_scan": "Seattle, WA", "delivered": True},
    },
]


# DNS-rebinding protection is on by default in FastMCP's streamable-HTTP
# transport, which rejects the Host header ACA ingress passes through and
# returns 421. We're a public no-auth demo endpoint, so disable it.
mcp = FastMCP(
    name="northwind-insurance-policy-claim-lookup" if INSURANCE_ONLY else "northwind-order-lookup",
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def lookup_order(query: str) -> str:
    """Look up Northwind customer + order + shipment by id or free-text.

    Args:
        query: An order id (e.g., "NW-40021"), customer id (e.g., "C-8801"),
            customer name / email / phone fragment, or free-text like
            "priya's last order".

    Returns:
        JSON-encoded list of matching orders. Each row includes the full
        customer, order line items, and shipment status.
    """
    if INSURANCE_ONLY:
        raise ValueError("Retail order lookup is unavailable in insurance mode.")
    q = (query or "").strip().lower()
    if not q:
        return json.dumps([])
    hits = [
        o for o in _ORDERS
        if (
            q in o["order_id"].lower()
            or q in o["customer"]["id"].lower()
            or q in o["customer"]["name"].lower()
            or q in o["customer"]["email"].lower()
            or q in o["customer"].get("phone", "").lower()
        )
    ]
    return json.dumps(hits[:5])


if not INSURANCE_ONLY:
    mcp.tool()(lookup_order)


@mcp.tool()
def lookup_policy(policy_id: str, policyholder_id: str | None = None,
                  loss_date: str | None = None, state: str | None = None) -> str:
    """Read a synthetic policy and its authoritative fictional wording/citations.

    Supply exact POL and optionally PH IDs, ISO loss_date and two-letter state.
    Unknown IDs, mismatches and inapplicable wording raise errors; never decide coverage.
    """
    require_insurance_domain()
    return json.dumps(lookup_policy_record(policy_id, policyholder_id, loss_date, state))


@mcp.tool()
def lookup_claim(claim_id: str, policy_id: str | None = None,
                 policyholder_id: str | None = None) -> str:
    """Read an exact synthetic CLM ID with verified linked policy and policyholder.

    Returns incident facts, outstanding evidence and effective, jurisdiction-specific
    fictional policy citations. Unknown or mismatched IDs raise surfaced errors.
    This read-only tool cannot approve or deny a claim.
    """
    require_insurance_domain()
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


# Legacy title kept to match the Container App name — the service is the
# Order Lookup MCP for the Order Concierge demo (the insurance lookup when insurance-only).
app = FastAPI(
    title="northwind-insurance-lookup-mcp" if INSURANCE_ONLY else "northwind-order-lookup-mcp",
    lifespan=_lifespan,
)


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
