"""FastAPI application entrypoint."""
from __future__ import annotations

# Load `.env` files into os.environ BEFORE any of the app modules import,
# so modules that use `os.environ.get(...)` directly (observability build
# stamp, company manifest endpoints) see the same values pydantic-settings
# sees. pydantic-settings' `env_file=` populates the Settings class only, not
# os.environ. Walks up the directory tree so it picks up either
# apps/api/.env or the repo-root .env, whichever exists.
from pathlib import Path

from dotenv import load_dotenv

for _p in Path(__file__).resolve().parents:
    _env = _p / ".env"
    if _env.exists():
        load_dotenv(_env, override=False)

import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi import Path as APIPath
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import model_gateway  # noqa: E402
from .business.routes import router as business_router
from .adjuster_support import MODELS as SUPPORT_MODELS, router as adjuster_router
from .company import (
    DEFAULT_DEPARTMENT_SLUG,
    canonical_department_slug,
    foundry_project_endpoint_for,
    resolve_department,
    serialize_company,
)
from .content_understanding import router as claims_router
from .gateway_guard import GatewayGuardMiddleware  # noqa: E402
from .harness.routes import router as harness_router
from .harness.runtime import runtime as harness_runtime
from .insurance import router as insurance_router, insurance_runtime_ready, require_insurance_runtime
from .insurance_domain import ACTION_KIND, InsuranceValidationError, lookup_claim_record, lookup_policy_record
from .observability import (
    build_info,
    configure_observability,
    set_request_department,
    set_request_gateway,
)
from .orchestrator_client import run_turn
from .repositories import (
    append_message,
    delete_hitl,
    list_hitl_pending,
    list_hitl_resolved,
    load_conversation,
    resolve_hitl,
)
from .settings import get_settings
from .traces import SESSION_ID_PATTERN, get_trace
from .voice import mint_voice_live_session, router as voice_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

configure_observability()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    problem = model_gateway.configuration_problem(settings)
    if problem:
        logger.error("AI Gateway configuration problem: %s", problem)
    elif model_gateway.gateway_enabled(settings):
        logger.info("Model inference is routed through the AI Gateway (provider %s).",
                    settings.ai_gateway_model_provider or "from AI_GATEWAY_MODEL_ALIASES")
    yield
    try:
        await harness_runtime.stop()
    finally:
        await model_gateway.close_gateway_clients()


app = FastAPI(title="Northwind Copilot API", version="0.1.0", lifespan=lifespan)
app.include_router(harness_router)
app.include_router(insurance_router)
app.include_router(claims_router)
app.include_router(voice_router)
app.include_router(adjuster_router)
app.include_router(business_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Correlation-ID", "ETag", "Location"],
)


@app.middleware("http")
async def stamp_department_middleware(request, call_next):
    """Read `X-Department` from every incoming request and record it in the
    observability context so every span created downstream carries the
    active department slug + foundry_project. Deprecated slugs are mapped to
    their canonical slug; unknown values fall back to Claims Concierge, which
    matches `resolve_department` semantics."""
    header = request.headers.get("x-department")
    try:
        dept = resolve_department(header)
        set_request_department(dept["slug"], dept["foundry_project"])
    except Exception:  # noqa: BLE001 — never fail the request on stamping
        pass
    set_request_gateway(model_gateway.route_for(get_settings()))
    return await call_next(request)


# Registered last so it is the outermost middleware. A no-op unless the
# environment sets NORTHWIND_GATEWAY_AUTH_REQUIRED=true (see gateway_guard.py).
app.add_middleware(GatewayGuardMiddleware, exempt_paths=frozenset({"/health"}))


class VoiceSessionRequest(BaseModel):
    channel: str = "customer"  # accepted for older clients; the insurance intake instructions apply to every channel
    voice: str = "alloy"


class HitlDecision(BaseModel):
    hitl_id: str
    workflow_id: str
    decision: str  # approve | reject
    note: str = ""


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "settings_loaded": bool(get_settings().foundry_endpoint),
            "business_domain": "insurance",
            "insurance_agent_runtime_ready": insurance_runtime_ready()}


@app.get("/gateway/status")
async def gateway_status() -> dict:
    """Where model inference goes — the AI Gateway or Foundry directly — and
    whether the gateway configuration is usable. Non-secret: the endpoint host,
    provider, token scope and the deployment → gateway alias mapping only."""
    return model_gateway.status(get_settings(), list(SUPPORT_MODELS.values()))


# ── Company manifest + department routing (multi-project story) ────────────
@app.get("/company")
async def company() -> dict:
    """Return the Northwind Insurance manifest — company name, brand,
    platform tabs, and every department (slug, pillar, Foundry project,
    Search index, enabled tabs). Consumed by both web-portal (landing
    page + scenario picker) and web-voice (branding)."""
    return serialize_company()


@app.get("/departments")
async def departments() -> dict:
    """Convenience shortcut for the departments array."""
    return {"departments": serialize_company()["departments"]}


@app.get("/departments/{slug}")
async def department_detail(slug: str) -> dict:
    """Resolve a department by slug (deprecated slugs are accepted). 404 if
    unknown so callers can't rely on the silent Claims Concierge fallback
    that other endpoints use."""
    canonical = canonical_department_slug(slug)
    for d in serialize_company()["departments"]:
        if d["slug"] == canonical:
            return {**d, "foundry_project_endpoint": foundry_project_endpoint_for(canonical)}
    raise HTTPException(status_code=404, detail=f"unknown department: {slug}")


class HandoffRequest(BaseModel):
    """Voice Live -> Claims Concierge multi-agent handoff payload."""
    summary: str
    caller: str | None = None
    intent: str | None = None
    urgency: str | None = None
    claim_id: str | None = Field(default=None, pattern=r"^CLM-\d+$")
    policy_id: str | None = Field(default=None, pattern=r"^POL-\d+$")
    policyholder_id: str | None = Field(default=None, pattern=r"^PH-\d+$")
    loss_date: str | None = None
    state: str | None = None


@app.post("/handoff")
async def handoff(
    req: HandoffRequest,
    x_department: str | None = Header(None, alias="X-Department"),
) -> StreamingResponse:
    """Cross-project handoff — the Policyholder Contact Center console posts a
    running call summary here, we synthesize an equivalent chat turn, and
    start a Claims Concierge orchestrator run against it. The response is
    NDJSON in the same wire format as `/insurance/chat` so the claims copilot
    tab can consume it directly and appear to pick up mid-conversation.

    `X-Department` is accepted only for observability stamping; the
    handoff target is always Claims Concierge (that's where the flagship
    orchestrator lives).
    """
    require_insurance_runtime()
    try:
        if req.claim_id:
            record = lookup_claim_record(req.claim_id, req.policy_id, req.policyholder_id)
            if (req.loss_date and req.loss_date != record["claim"]["loss_date"]) or (
                    req.state and req.state != record["claim"]["state"]):
                raise InsuranceValidationError("Handoff loss date or jurisdiction does not match this claim.")
        elif req.policy_id:
            lookup_policy_record(req.policy_id, req.policyholder_id, req.loss_date, req.state)
    except InsuranceValidationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, "loss_date must be a valid ISO date") from exc
    resolved_from = resolve_department(x_department)  # for stamping only
    sid = str(uuid.uuid4())

    # Compose a first-turn message that reads like the call transcript
    # summary. The multi-agent orchestrator will re-run its triage +
    # policy/claim lookup + claim-action draft chain on this input just as it
    # would on a chat message.
    lines = [f"[Handoff from {resolved_from['name']}] {req.summary}"]
    if req.caller:
        lines.append(f"Caller: {req.caller}")
    if req.intent:
        lines.append(f"Intent: {req.intent}")
    if req.urgency:
        lines.append(f"Urgency: {req.urgency}")
    for label, value in (("Claim", req.claim_id), ("Policy", req.policy_id),
                         ("Policyholder", req.policyholder_id), ("Loss date", req.loss_date),
                         ("State", req.state)):
        if value:
            lines.append(f"{label}: {value}")
    composed = "\n".join(lines)

    async def gen() -> AsyncIterator[bytes]:
        yield (json.dumps({
            "kind": "session",
            "session_id": sid,
            "handoff_from": resolved_from["slug"],
            "handoff_to": DEFAULT_DEPARTMENT_SLUG,
        }) + "\n").encode()
        async for event in run_turn(sid, composed):
            yield (json.dumps(event) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/session")
async def new_session() -> dict:
    return {"session_id": str(uuid.uuid4())}


@app.get("/session/{sid}/messages")
async def get_messages(sid: str) -> dict:
    return {"messages": await load_conversation(sid)}


@app.post("/voice/session")
async def voice_session(req: VoiceSessionRequest, origin: str | None = Header(None, alias="Origin")) -> dict:
    require_insurance_runtime()
    instructions = (
        "You are the fictional Northwind Insurance intake assistant. Capture policyholder, policy "
        "and claim IDs, loss date and state, incident facts and missing evidence; confirm details. "
        "Never decide coverage, approve a claim, offer a settlement or execute payment. "
        "Policy terms require authoritative versioned jurisdiction-specific citations from tools, "
        "not model memory or web search. If unavailable, defer to a qualified human adjuster."
    )
    try:
        return await mint_voice_live_session(instructions=instructions, voice=req.voice, origin=origin)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("voice session mint failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/hitl")
async def hitl_pending(workflow_id: str | None = Query(default=None, min_length=1, max_length=256)) -> dict:
    return {"items": await list_hitl_pending(workflow_id=workflow_id)}


@app.get("/hitl/resolved")
async def hitl_resolved() -> dict:
    """Approved or rejected claim-action drafts (fed by the HITL queue).
    Powers the reviewed-drafts tab — one card per reviewed draft."""
    return {"items": await list_hitl_resolved()}


@app.delete("/hitl/{hitl_id}")
async def hitl_delete(hitl_id: str, workflow_id: str) -> dict:
    """Prune a reviewed draft from the HITL queue. `workflow_id` is passed
    as a query param since it's the Cosmos partition key. Idempotent:
    already-missing rows still return {status: ok} so the UI can safely
    retry a partial failure without surfacing a 404 to the user."""
    await delete_hitl(hitl_id, workflow_id)
    return {"status": "ok", "hitl_id": hitl_id, "workflow_id": workflow_id}


# Deprecated aliases kept for already-deployed clients; use the /hitl routes above.
@app.get("/work-orders", deprecated=True)
async def work_orders() -> dict:
    """Deprecated alias of `GET /hitl/resolved`."""
    return await hitl_resolved()


@app.delete("/work-orders/{hitl_id}", deprecated=True)
async def delete_work_order(hitl_id: str, workflow_id: str) -> dict:
    """Deprecated alias of `DELETE /hitl/{hitl_id}`."""
    return await hitl_delete(hitl_id, workflow_id)


@app.post("/hitl/decide")
async def hitl_decide(req: HitlDecision) -> dict:
    if req.decision not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    try:
        resolved = await resolve_hitl(req.hitl_id, req.workflow_id, req.decision, req.note)
    except CosmosResourceNotFoundError as exc:
        raise HTTPException(404, "Pending review record does not exist for this workflow.") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    replayed = resolved.pop("_decision_replayed", False)

    # Post a confirmation back into the originating chat session so the
    # claims copilot visibly closes the loop. The draft records the session
    # it was created from.
    session_id = resolved.get("sessionId") if resolved.get("kind") == ACTION_KIND else None
    message = None
    if session_id:
        payload = resolved.get("payload") or {}
        message = (
            f"Human {'approved' if req.decision == 'approve' else 'rejected'} the "
            f"{payload['action_type']} draft for synthetic claim {payload['claim_id']}. "
            "Review decision only: no coverage decision, claim approval, settlement, or payment was executed."
        )
        try:
            if not replayed:
                await append_message(
                    session_id, "assistant", message,
                    meta={"kind": "hitl_resolution", "decision": req.decision,
                          "hitl_id": req.hitl_id, "workflow_id": req.workflow_id},
                )
        except Exception:  # noqa: BLE001 — best-effort; don't fail the approval on write error.
            logger.exception("failed to append hitl resolution message to conversation %s", session_id)

    return {**resolved, "chat_message": message, "session_id": session_id}


# ── In-app observability (A8, B6) ──────────────────────────────────────────
@app.get("/build")
async def build() -> dict:
    """Git SHA + prompt version manifest currently running. Rendered in the
    web-portal footer so demo audiences can see the exact commit that shipped
    every prompt in the current turn (this is the human-readable half of B6)."""
    return {"build": build_info()}


@app.get("/traces/{sid}")
def traces(sid: str = APIPath(pattern=SESSION_ID_PATTERN), minutes: int = 15) -> dict:
    """Trace panel data for session `sid` — span tree, per-agent latency +
    reported token/cost metrics and a handoff diagram. When live telemetry is
    unavailable the response says so; no synthetic trace is substituted."""
    return get_trace(sid, minutes=max(1, min(minutes, 60)))
