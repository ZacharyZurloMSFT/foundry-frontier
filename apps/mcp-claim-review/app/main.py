# Copyright (c) Microsoft. All rights reserved.
"""Northwind insurance claim-review MCP server.

Exposes the MCP tool `draft_claim_action(workflow_id, draft)` over the
streamable-HTTP transport that Azure AI Foundry connects to when an agent has
a `type: mcp` tool wired to this Container App's URL. Every call shows up as
a first-class MCP tool call in Foundry Observability + App Insights.

Human-in-the-loop: the tool writes each draft to the Cosmos `hitl_queue`
container (partition key /workflowId) with status='pending' and the insurance
claim-action kind. The web Approvals tab (or any consumer of the /hitl API in
apps/api) picks it up from there and posts approve/reject to /hitl/decide.
Foundry sees a successful tool call returning the queue item; the actual
approval loop lives outside the agent runtime. Nothing here can approve a
claim, decide coverage or execute a payment.

In default compatibility mode the workflow matches the `x-workflow-id` header.
CLAIM_WORKFLOW_BINDING=signed instead requires a short-lived RSA-signed workflow_proof
bound to the workflow and exact canonical draft. No header fallback is allowed.

No auth on the MCP endpoint itself by default — it's a public demo. Setting
NORTHWIND_GATEWAY_AUTH_REQUIRED=true (see gateway_guard.py) makes it accept only
calls relayed by the API gateway. Cosmos writes go through the shared UAMI
(`AZURE_CLIENT_ID`) which has the Cosmos DB Built-in Data Contributor role from
the containerapps bicep module.
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceExistsError
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
if not __package__:
    from apps.api.app.insurance_domain import ACTION_KIND, ClaimActionDraft, action_payload, pending_action
else:
    try:
        from .insurance_domain import ACTION_KIND, ClaimActionDraft, action_payload, pending_action
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.insurance_domain":
            raise
        from apps.api.app.insurance_domain import ACTION_KIND, ClaimActionDraft, action_payload, pending_action
if not __package__:
    from apps.api.app.gateway_guard import GatewayGuardMiddleware
else:
    try:
        from .gateway_guard import GatewayGuardMiddleware
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.gateway_guard":
            raise
        from apps.api.app.gateway_guard import GatewayGuardMiddleware

if not __package__:
    from apps.api.app.workflow_binding import verify_workflow
else:
    try:
        from .workflow_binding import verify_workflow
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.workflow_binding":
            raise
        from apps.api.app.workflow_binding import verify_workflow

logger = logging.getLogger("mcp-insurance-review")

if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"],
    )


COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT", "")
COSMOS_DATABASE = os.environ.get("COSMOS_DATABASE", "northwind")
HITL_CONTAINER = "hitl_queue"
# Canonical workflow-binding header first, then the deprecated name still sent by older callers.
WORKFLOW_HEADERS = ("x-workflow-id", "x-orders-workflow")

_credential: DefaultAzureCredential | None = None
_cosmos: CosmosClient | None = None


def _cosmos_container():
    """Lazy-init the Cosmos container client (UAMI-backed)."""
    global _credential, _cosmos
    if not COSMOS_ENDPOINT:
        return None
    if _cosmos is None:
        _credential = DefaultAzureCredential(
            managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID") or None
        )
        _cosmos = CosmosClient(COSMOS_ENDPOINT, credential=_credential)
    return _cosmos.get_database_client(COSMOS_DATABASE).get_container_client(HITL_CONTAINER)


# DNS-rebinding protection is on by default in FastMCP's streamable-HTTP
# transport, which rejects the Host header ACA ingress passes through and
# returns 421. We're a public no-auth demo endpoint, so disable it.
mcp = FastMCP(
    name="northwind-insurance-claim-action-draft",
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def draft_claim_action(workflow_id: str, draft: ClaimActionDraft, ctx: Context,
                             workflow_proof: str | None = None) -> str:
    """Persist a synthetic claim-action draft for HUMAN review only.

    Workflow binding is required: request headers in compatibility mode, or a
    trusted application-signed workflow_proof in signed mode. The model never signs.
    Allowed actions request evidence, assign adjuster, update customer, escalate
    review, or recommend settlement. No coverage denial/claim approval/payment
    execution is available. Missing storage or validation errors fail the tool.
    """
    if not re.fullmatch(r"wf-[A-Za-z0-9][A-Za-z0-9_-]{0,123}", workflow_id):
        raise ValueError("Invalid application-owned workflow ID")
    proposed = action_payload(ClaimActionDraft.model_validate(draft))
    binding = os.environ.get("CLAIM_WORKFLOW_BINDING", "headers").strip().lower()
    if binding == "signed":
        verify_workflow(workflow_proof, workflow_id, proposed, os.environ.get("CLAIM_WORKFLOW_PUBLIC_KEY", ""))
    elif binding == "headers" and workflow_proof is None:
        request = ctx.request_context.request
        bound = {request.headers.get(name) for name in WORKFLOW_HEADERS} - {None} if request is not None else set()
        if bound != {workflow_id}:
            raise ValueError("Insurance workflow does not match the application-bound request.")
    else:
        raise ValueError("Claim workflow binding configuration does not match the caller.")
    container = _cosmos_container()
    if container is None:
        raise RuntimeError("Cosmos approval storage is not configured; no draft was saved.")
    item = {
        "id": f"hitl-{workflow_id}", "workflowId": workflow_id, "kind": ACTION_KIND,
        "status": "pending", "payload": proposed, "createdAt": datetime.now(timezone.utc).isoformat(),
        "business_domain": "insurance", "synthetic": True, "execution_status": "not_executed",
        "source": "mcp-claim-action-draft",
        "sessionId": (workflow_id.removeprefix("wf-insurance-") if workflow_id.startswith("wf-insurance-")
                      else workflow_id.removeprefix("wf-")),
    }
    try:
        await container.create_item(item)
    except CosmosResourceExistsError:
        existing = await container.read_item(item["id"], partition_key=workflow_id)
        if existing.get("kind") != ACTION_KIND or existing["payload"] != proposed or existing["status"] != "pending":
            raise ValueError("This workflow already has a different or decided draft.")
        item = existing
    return json.dumps(pending_action(item))


# FastMCP streamable-HTTP transport needs its session_manager task group
# started via the ASGI lifespan (otherwise: "Task group is not initialized").
@asynccontextmanager
async def _lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield
    if _cosmos is not None:
        await _cosmos.close()
    if _credential is not None:
        await _credential.close()


app = FastAPI(title="northwind-insurance-review-mcp", lifespan=_lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/mcp", mcp.streamable_http_app())

# Registered last so it is the outermost middleware (it also covers the mounted MCP
# transport). A no-op unless NORTHWIND_GATEWAY_AUTH_REQUIRED=true; see gateway_guard.py.
app.add_middleware(GatewayGuardMiddleware, exempt_paths=frozenset({"/healthz"}))
