"""Insurance-only runtime gate for agent inference, plus the draft/records router.

Inference stays unavailable until the insurance agent fleet is explicitly verified."""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from azure.cosmos.exceptions import CosmosHttpResponseError
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .insurance_domain import (
    ACTION_KIND, ClaimActionDraft, InsuranceValidationError, action_payload, fixtures,
    lookup_claim_record, lookup_policy_record, pending_action,
)
from .orchestrator_client import run_turn
from .repositories import enqueue_hitl
from .settings import get_settings

router = APIRouter(prefix="/insurance", tags=["synthetic-insurance"])

# Foundry projects that may host the insurance agent fleet (mirrors scripts/deploy-hosted-agents.py).
INSURANCE_PROJECTS = ("northwind-orders", "nw-insurance-claims")


def insurance_runtime_ready() -> bool:
    settings = get_settings()
    try:
        project = urlsplit(settings.foundry_project_endpoint)
    except ValueError:
        return False
    return (
        settings.insurance_agents_ready
        and settings.insurance_project_name in INSURANCE_PROJECTS
        and settings.hosted_agents_enabled and project.scheme == "https" and bool(project.hostname)
        and not project.username and not project.password and not project.query and not project.fragment
        and project.path.rstrip("/") == f"/api/projects/{settings.insurance_project_name}"
        and settings.insurance_orchestrator_agent_id.startswith("northwind-insurance-")
    )


def require_insurance_runtime() -> None:
    if not insurance_runtime_ready():
        raise HTTPException(
            503, "Insurance agent inference is unavailable. Deploy and verify the insurance agents, "
            "then configure NORTHWIND_INSURANCE_AGENTS_READY=true, HOSTED_AGENTS_ENABLED=true and "
            "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID, with FOUNDRY_PROJECT_ENDPOINT pointing at the "
            "insurance Foundry project named by INSURANCE_PROJECT_NAME.",
        )


@router.get("/fixtures")
async def insurance_fixtures() -> dict:
    return {**fixtures(), "agent_runtime_ready": insurance_runtime_ready()}


@router.get("/policies/{policy_id}")
async def policy_lookup(policy_id: str, policyholder_id: str | None = None,
                        loss_date: str | None = None, state: str | None = None) -> dict:
    try:
        return lookup_policy_record(policy_id, policyholder_id, loss_date, state)
    except InsuranceValidationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "loss_date must be a valid ISO date") from exc


@router.get("/claims/{claim_id}")
async def claim_lookup(claim_id: str, policy_id: str | None = None,
                       policyholder_id: str | None = None) -> dict:
    try:
        return lookup_claim_record(claim_id, policy_id, policyholder_id)
    except InsuranceValidationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/actions/draft")
async def draft_action(req: ClaimActionDraft) -> dict:
    if not get_settings().cosmos_endpoint:
        raise HTTPException(503, "Cosmos approval storage is not configured; no draft was saved.")
    try:
        item = await enqueue_hitl(f"wf-insurance-{uuid.uuid4()}", ACTION_KIND, action_payload(req))
    except CosmosHttpResponseError as exc:
        raise HTTPException(503, "Approval storage is unavailable; no pending approval was confirmed.") from exc
    return pending_action(item)


class InsuranceChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,120}$")


@router.post("/chat")
async def insurance_chat(req: InsuranceChatRequest) -> StreamingResponse:
    require_insurance_runtime()
    sid = req.session_id or str(uuid.uuid4())

    async def stream() -> AsyncIterator[bytes]:
        yield (json.dumps({"kind": "session", "session_id": sid, "business_domain": "insurance"}) + "\n").encode()
        async for event in run_turn(sid, req.message):
            yield (json.dumps(event) + "\n").encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson")
