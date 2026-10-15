# Copyright (c) Microsoft. All rights reserved.
"""Northwind resolution-draft MCP server.

Exposes a single MCP tool, `propose_resolution(workflow_id, draft)`, over
the streamable-HTTP transport that Azure AI Foundry connects to when a
prompt agent has a `type: mcp` tool wired to this Container App's URL.

The `northwind-resolution-draft` prompt agent (agents/work_order_draft.yaml)
declares `propose_resolution` as its only MCP tool. Every call shows up as
a first-class MCP tool call in Foundry Observability + App Insights.

NOTE ON LEGACY NAMES: this Container App is still called
`ca-mcp-work-order` and its azure.yaml service is still `mcp-work-order`.
Those identifiers are wired through Bicep + azd env and renaming them
would risk breaking `azd up`. Semantically this service is the Resolution
Draft MCP; keep the legacy infra name.

Human-in-the-loop: this tool writes each draft to the Cosmos `hitl_queue`
container (partition key /workflowId) with status='pending' and
kind='resolution'. The web-tech Approvals tab (or any consumer of the
/hitl API in apps/api) picks it up from there and posts approve/reject to
/hitl/decide. Foundry sees a successful tool call returning the queue
item; the actual approval loop lives outside the agent runtime.

No auth on the MCP endpoint itself — it's a public demo. Cosmos writes go
through the shared UAMI (`AZURE_CLIENT_ID`) which has the Cosmos DB
Built-in Data Contributor role from the containerapps bicep module.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceExistsError
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field
if not __package__:
    from apps.api.app.insurance_domain import ACTION_KIND, ClaimActionDraft, action_payload, pending_action, require_insurance_domain
else:
    try:
        from .insurance_domain import ACTION_KIND, ClaimActionDraft, action_payload, pending_action, require_insurance_domain
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.insurance_domain":
            raise
        from apps.api.app.insurance_domain import ACTION_KIND, ClaimActionDraft, action_payload, pending_action, require_insurance_domain

logger = logging.getLogger("mcp-resolution-draft")

if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"],
    )


COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT", "")
COSMOS_DATABASE = os.environ.get("COSMOS_DATABASE", "northwind")
HITL_CONTAINER = "hitl_queue"

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
    name="northwind-resolution-draft",
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


class HarnessDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(pattern=r"^NW-\d+$")
    customer_id: str = Field(min_length=1, max_length=128)
    resolution_type: Literal["refund_full", "refund_partial", "reship", "rma",
                             "store_credit", "address_change", "cancel", "goodwill_credit"]
    amount_usd: float = Field(ge=0, le=100000, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=2000)
    policy_ref: str = Field(min_length=1, max_length=256)
    urgency: Literal["P1", "P2", "P3", "P4"]


@mcp.tool()
async def propose_resolution(workflow_id: str, draft: dict[str, Any], ctx: Context) -> str:
    """Save a draft customer resolution and enqueue it for human approval.

    Args:
        workflow_id: Correlating workflow id (provided by the orchestrator).
        draft: Structured resolution fields (order_id, customer_id,
            resolution_type, amount_usd, reason, policy_ref, urgency).

    Returns:
        JSON-encoded `{ status, hitl_id, workflow_id, draft }` payload. The
        hitl_id is the Cosmos document id — approve/reject at
        POST /hitl/decide on the api service.
    """
    request = ctx.request_context.request
    bound_workflow = request.headers.get("x-harness-workflow") if request is not None else None
    harness = workflow_id.startswith("wf-harness-") or bound_workflow is not None
    if harness and bound_workflow != workflow_id:
        logger.warning("Rejected unbound or cross-run harness proposal")
        return json.dumps({"status": "error", "error": "Harness workflow does not match the application-bound request."})
    hitl_id = f"hitl-{workflow_id}" if harness else str(uuid.uuid4())
    item = {
        "id": hitl_id,
        "workflowId": workflow_id,
        "kind": "resolution",
        "status": "pending",
        "payload": draft,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "source": "mcp-resolution-draft",
    }
    container = _cosmos_container()
    if container is None:
        if harness:
            logger.error("Harness proposal rejected: Cosmos is not configured")
            return json.dumps({"status": "error", "error": "Cosmos is not configured; no draft was saved."})
        logger.warning("COSMOS_ENDPOINT not set — returning ephemeral hitl_id.")
        return json.dumps({
            "status": "pending_approval",
            "hitl_id": hitl_id,
            "workflow_id": workflow_id,
            "draft": draft,
            "warning": "Cosmos not configured; draft was not persisted.",
        })
    try:
        if harness:
            if not re.fullmatch(r"wf-harness-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", workflow_id):
                raise ValueError("Invalid harness workflow ID")
            item["payload"] = HarnessDraft.model_validate(draft).model_dump()
            workflows = _cosmos.get_database_client(COSMOS_DATABASE).get_container_client("workflow_state")
            run = await workflows.read_item(workflow_id, partition_key=workflow_id)
            if (run.get("record_kind") != "harness_run" or run.get("status") != "running"
                    or run.get("deadline", 0) <= time.time()):
                raise ValueError("Harness workflow is not active")
            item["harness"] = "copilot-sdk"
        await container.create_item(item)
    except CosmosResourceExistsError:
        if not harness:
            raise
        existing = await container.read_item(hitl_id, partition_key=workflow_id)
        if existing["payload"] != item["payload"] or existing["status"] != "pending":
            logger.warning("Conflicting harness proposal rejected: %s", workflow_id)
            return json.dumps({"status": "error", "error": "This workflow already has a different or decided draft."})
        return json.dumps({"status": "pending_approval", "hitl_id": hitl_id,
                           "workflow_id": workflow_id, "draft": existing["payload"]})
    except Exception as exc:
        logger.exception("hitl_queue create_item failed")
        return json.dumps({
            "status": "error",
            "hitl_id": hitl_id,
            "workflow_id": workflow_id,
            "draft": draft,
            "error": "Draft could not be persisted." if harness else str(exc),
        })
    return json.dumps({
        "status": "pending_approval",
        "hitl_id": hitl_id,
        "workflow_id": workflow_id,
        "draft": item["payload"],
    })


@mcp.tool()
async def draft_claim_action(workflow_id: str, draft: ClaimActionDraft, ctx: Context) -> str:
    """Persist a synthetic claim-action draft for HUMAN review only.

    Application-owned workflow_id must match the x-orders-workflow request header.
    Allowed actions request evidence, assign adjuster, update customer, escalate
    review, or recommend settlement. No coverage denial/claim approval/payment
    execution is available. Missing storage or validation errors fail the tool.
    """
    require_insurance_domain()
    if not re.fullmatch(r"wf-[A-Za-z0-9][A-Za-z0-9_-]{0,123}", workflow_id):
        raise ValueError("Invalid application-owned workflow ID")
    request = ctx.request_context.request
    if request is None or request.headers.get("x-orders-workflow") != workflow_id:
        raise ValueError("Insurance workflow does not match the application-bound request.")
    proposed = action_payload(ClaimActionDraft.model_validate(draft))
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


# Legacy title kept to match the Container App name — the service is the
# Resolution Draft MCP for the Order Concierge demo.
app = FastAPI(title="northwind-resolution-draft-mcp", lifespan=_lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/mcp", mcp.streamable_http_app())
