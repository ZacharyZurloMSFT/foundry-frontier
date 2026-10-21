"""FastAPI application entrypoint."""
from __future__ import annotations

# Load `.env` files into os.environ BEFORE any of the app modules import,
# so modules that use `os.environ.get(...)` directly (bakeoff, safety,
# observability build stamp) see the same values pydantic-settings sees.
# pydantic-settings' `env_file=` populates the Settings class only, not
# os.environ — that's why FOUNDRY_BAKEOFF_DEPLOYMENTS from .env used to
# silently miss. Walks up the directory tree so it picks up either
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

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi import Path as APIPath
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .bakeoff import ModelSpec, stream_bakeoff
from .business.routes import router as business_router
from .adjuster_support import SupportRequest, router as adjuster_router, support_chat
from .company import (
    DEFAULT_DEPARTMENT_SLUG,
    foundry_project_endpoint_for,
    resolve_department,
    serialize_company,
)
from .content_understanding import router as claims_router
from .harness.routes import router as harness_router
from .harness.runtime import runtime as harness_runtime
from .insurance import router as insurance_router, insurance_runtime_ready, require_insurance_runtime
from .insurance_domain import ACTION_KIND, InsuranceValidationError, lookup_claim_record, lookup_policy_record
from .observability import (
    build_info,
    configure_observability,
    set_request_department,
)
from .orchestrator_client import ask_single_turn, run_agent_turn, run_turn
from .repositories import (
    append_message,
    delete_hitl,
    list_hitl_pending,
    list_hitl_resolved,
    load_conversation,
    resolve_hitl,
)
from .safety import CANNED_ATTACKS
from .safety import check as safety_check
from .settings import get_settings
from .traces import SESSION_ID_PATTERN, get_trace
from .voice import mint_voice_live_session, router as voice_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

configure_observability()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await harness_runtime.stop()


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
    active department slug + foundry_project. Invalid insurance departments
    must not silently fall through to another project's handlers."""
    header = request.headers.get("x-department")
    try:
        dept = resolve_department(header)
        set_request_department(dept["slug"], dept["foundry_project"])
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except (KeyError, RuntimeError, ValueError):
        logging.getLogger(__name__).exception("Department context is unavailable")
        return JSONResponse(status_code=503, content={"detail": "Department configuration is unavailable."})
    return await call_next(request)


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


class VoiceSessionRequest(BaseModel):
    channel: str = "customer"  # customer | agent
    voice: str = "alloy"


class QARequest(BaseModel):
    question: str


class HitlDecision(BaseModel):
    hitl_id: str
    workflow_id: str
    decision: str  # approve | reject
    note: str = ""


class BakeoffRequest(BaseModel):
    message: str
    system: str | None = None


class SafetyCheckRequest(BaseModel):
    text: str


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "settings_loaded": bool(get_settings().foundry_endpoint),
            "business_domain": get_settings().business_domain,
            "insurance_agent_runtime_ready": insurance_runtime_ready()}


# ── Company manifest + department routing (multi-project story) ────────────
@app.get("/company")
async def company() -> dict:
    """Return the Northwind Industries manifest — company name, brand,
    platform tabs, and every department (slug, pillar, Foundry project,
    Search index, enabled tabs). Consumed by both web-tech (landing
    page + scenario picker) and web-voice (branding)."""
    return serialize_company()


@app.get("/departments")
async def departments() -> dict:
    """Convenience shortcut for the departments array."""
    return {"departments": serialize_company()["departments"]}


@app.get("/departments/{slug}")
async def department_detail(slug: str) -> dict:
    """Resolve a department by slug. 404 if unknown so callers can't rely
    on the silent Order Concierge fallback that other endpoints use."""
    company_dict = serialize_company()
    for d in company_dict["departments"]:
        if d["slug"] == slug:
            return {**d, "foundry_project_endpoint": foundry_project_endpoint_for(slug)}
    raise HTTPException(status_code=404, detail=f"unknown department: {slug}")


class HandoffRequest(BaseModel):
    """Voice Live -> Order Concierge multi-agent handoff payload."""
    summary: str
    caller: str | None = None
    order_id: str | None = None
    customer_hint: str | None = None
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
    """Cross-project handoff — the Customer Contact Center console posts a
    running call summary here, we synthesize an equivalent chat turn, and
    start an Order Concierge orchestrator run against it. The response is
    the same NDJSON wire format as `/chat` so the web-tech Multi-Agent
    tab can consume it directly and appear to pick up mid-conversation.

    `X-Department` is accepted only for observability stamping; the
    handoff target is always Order Concierge (that's where the flagship
    orchestrator lives).
    """
    if get_settings().business_domain == "insurance":
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
    # order-lookup + policy-lookup + resolution-draft chain on this
    # input just as it would on a chat message.
    lines = [f"[Handoff from {resolved_from['name']}] {req.summary}"]
    if req.caller:
        lines.append(f"Caller: {req.caller}")
    if req.order_id:
        lines.append(f"Order: {req.order_id}")
    if req.customer_hint:
        lines.append(f"Customer: {req.customer_hint}")
    if req.intent:
        lines.append(f"Intent: {req.intent}")
    if req.urgency:
        lines.append(f"Urgency: {req.urgency}")
    if get_settings().business_domain == "insurance":
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


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    if get_settings().business_domain == "insurance":
        from .insurance import InsuranceChatRequest, insurance_chat
        return await insurance_chat(InsuranceChatRequest(**req.model_dump()))
    sid = req.session_id or str(uuid.uuid4())

    async def gen() -> AsyncIterator[bytes]:
        yield (json.dumps({"kind": "session", "session_id": sid}) + "\n").encode()
        async for event in run_turn(sid, req.message):
            yield (json.dumps(event) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/qa")
async def qa(req: QARequest) -> StreamingResponse:
    if get_settings().business_domain == "insurance":
        require_insurance_runtime()
    async def gen() -> AsyncIterator[bytes]:
        async for chunk in ask_single_turn(req.question):
            yield (json.dumps({"kind": "assistant_delta", "text": chunk}) + "\n").encode()
        yield (json.dumps({"kind": "done"}) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# Slug → settings-field mapping for the direct-to-agent chat endpoint. Only
# these slugs are exposed; anything else 404s so callers can't reach arbitrary
# agents in the Foundry project.
def _agent_name_for_slug(slug: str) -> str | None:
    s = get_settings()
    mapping = {
        "manual": s.manual_agent_id or "northwind-policy-lookup",
        "tech": s.tech_agent_id or "northwind-order-lookup",
        "workorder": s.work_order_agent_id or "northwind-resolution-draft",
        # Department-owned prompt agents. Each lives in its own Foundry
        # project (see infra/modules/foundry.bicep additionalProjects
        # loop) — see ``_department_for_slug`` for the routing.
        "hr": "northwind-hr-assistant",
        # Ask Northwind's model dropdown resolves to one of three
        # model-specific HR agents. Same instructions + same nw-hr RAG
        # grounding — only the underlying Foundry deployment differs
        # (see data/northwind/departments/employee-help-desk/prompts/*.yaml).
        "hr-gpt-5-4": "northwind-hr-assistant-gpt-5-4",
        "hr-gpt-5-4-mini": "northwind-hr-assistant-gpt-5-4-mini",
        "hr-kimi": "northwind-hr-assistant-kimi",
        "claims": "northwind-claims-triage",
        "intent": "northwind-intent-classifier",
    }
    return mapping.get(slug)


# Agent-slug → owning department slug. Determines which Foundry project the
# agent lives in so ``run_agent_turn`` targets the right endpoint. Order
# Concierge flagship (manual/tech/workorder) uses the default project so it
# isn't listed here.
_AGENT_SLUG_TO_DEPARTMENT = {
    "hr": "employee-help-desk",
    "hr-gpt-5-4": "employee-help-desk",
    "hr-gpt-5-4-mini": "employee-help-desk",
    "hr-kimi": "employee-help-desk",
    "claims": "claims-warranty",
    "intent": "contact-center",
}


@app.post("/agent/{slug}/chat")
async def agent_chat(slug: str, req: ChatRequest, request: Request) -> StreamingResponse:
    """Direct chat with one prompt agent (Policy Lookup, Order Lookup,
    Resolution Draft tabs). Wire format matches ``/chat`` so the front-end
    reuses the same stream reader.
    """
    insurance = get_settings().business_domain == "insurance"
    if insurance:
        if slug in {"hr", "hr-gpt-5-4", "hr-gpt-5-4-mini", "hr-kimi"}:
            return await support_chat(
                SupportRequest(message=req.message, session_id=req.session_id),
                slug="hr-gpt-5-4" if slug == "hr" else slug, request=request,
            )
        require_insurance_runtime()
        mapping = {
            "manual": "northwind-insurance-policy-wording",
            "tech": "northwind-insurance-policy-claim-lookup",
            "workorder": "northwind-insurance-claim-action-draft",
        }
        if slug not in mapping:
            raise HTTPException(503, "This department agent has not been verified for insurance.")
        agent_name = mapping[slug]
    else:
        agent_name = _agent_name_for_slug(slug)
    if not agent_name:
        raise HTTPException(status_code=404, detail=f"unknown agent slug: {slug}")
    sid = req.session_id or str(uuid.uuid4())

    # Route to the Foundry project that owns this agent. Order Concierge agents
    # fall through to the flagship project (default in ``run_agent_turn``).
    dept_slug = _AGENT_SLUG_TO_DEPARTMENT.get(slug)
    project_endpoint = foundry_project_endpoint_for(dept_slug) if dept_slug else None

    async def gen() -> AsyncIterator[bytes]:
        yield (json.dumps({"kind": "session", "session_id": sid, "agent": agent_name}) + "\n").encode()
        workflow_id = f"wf-{sid}" if slug in {"manual", "tech", "workorder"} else None
        async for event in run_agent_turn(sid, req.message, agent_name,
                                         project_endpoint=project_endpoint,
                                         workflow_id=workflow_id,
                                         hosted=False if dept_slug else None):
            yield (json.dumps(event) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/voice/session")
async def voice_session(req: VoiceSessionRequest, origin: str | None = Header(None, alias="Origin")) -> dict:
    if get_settings().business_domain == "insurance":
        require_insurance_runtime()
        instructions = (
            "You are the fictional Northwind Insurance intake assistant. Capture policyholder, policy "
            "and claim IDs, loss date and state, incident facts and missing evidence; confirm details. "
            "Never decide coverage, approve a claim, offer a settlement or execute payment. "
            "Policy terms require authoritative versioned jurisdiction-specific citations from tools, "
            "not model memory or web search. If unavailable, defer to a qualified human adjuster."
        )
    elif req.channel == "customer":
        instructions = (
            "You are the Northwind Order Concierge customer intake voice agent. "
            "Warmly greet the caller, capture their order id (or a customer "
            "identifier), the intent (order status / return / cancel / modify / "
            "complaint / refund request), and any details. Then confirm the "
            "details back and let them know an agent will follow up with the "
            "resolution."
        )
    else:
        instructions = (
            "You are the Northwind Order Concierge back-office assistant. Answer "
            "policy questions using the policy lookup tool; keep responses under "
            "30 seconds and always cite the policy title and section."
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


@app.get("/work-orders")
async def work_orders() -> dict:
    """Resolved customer resolutions (fed by the HITL queue). Powers the
    "Resolutions" tab — one card per resolved decision. The endpoint URL is
    kept as `/work-orders` for backwards compatibility with the legacy
    Field Services scaffold; semantically these are resolution cases."""
    return {"items": await list_hitl_resolved()}


@app.delete("/work-orders/{hitl_id}")
async def delete_work_order(hitl_id: str, workflow_id: str) -> dict:
    """Prune a resolved case from the HITL queue. `workflow_id` is passed
    as a query param since it's the Cosmos partition key. Idempotent:
    already-missing rows still return {status: ok} so the UI can safely
    retry a partial failure without surfacing a 404 to the user."""
    await delete_hitl(hitl_id, workflow_id)
    return {"status": "ok", "hitl_id": hitl_id, "workflow_id": workflow_id}


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
    # Multi-Agent tab visibly closes the loop. Workflow ids are minted as
    # ``wf-{session_id}`` in run_turn(), so we recover the session id here.
    session_id = req.workflow_id[len("wf-"):] if req.workflow_id.startswith("wf-") else None
    if resolved.get("kind") == ACTION_KIND:
        session_id = resolved.get("sessionId")
    if session_id:
        payload = resolved.get("payload") or {}
        resolution_type = payload.get("resolution_type") or "the resolution"
        order_id = payload.get("order_id") or "the order"
        amount = payload.get("amount_usd")
        amount_str = f" (${amount:.2f})" if isinstance(amount, (int, float)) and amount else ""
        message = (
            f"✅ Human approved the {resolution_type}{amount_str} for {order_id}. "
            "Action queued — details are in the Resolutions tab."
            if req.decision == "approve"
            else f"🛑 Human rejected the {resolution_type} for {order_id}. "
                 "No action will be taken."
        )
        if resolved.get("kind") == ACTION_KIND:
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

    return {**resolved, "chat_message": message if session_id else None,
            "session_id": session_id}


# ── Model bake-off (A1) ────────────────────────────────────────────────────
@app.get("/bakeoff/models")
async def bakeoff_models() -> dict:
    """Return the set of Azure-direct deployments currently in the bake-off,
    with published $/1M in/out pricing so the UI can render a cost column."""
    return {
        "models": [
            {
                "deployment": m.deployment,
                "price_in_per_mtok": m.price_in_per_mtok,
                "price_out_per_mtok": m.price_out_per_mtok,
            }
            for m in ModelSpec.resolve_all()
        ],
    }


@app.post("/bakeoff/chat")
async def bakeoff_chat(req: BakeoffRequest) -> StreamingResponse:
    """Fan the same message out to every bake-off deployment in parallel and
    stream deltas, per-model latency, tokens, and cost as NDJSON."""
    async def gen() -> AsyncIterator[bytes]:
        async for event in stream_bakeoff(req.message, system=req.system):
            yield (json.dumps(event) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ── Content Safety / Prompt Shields (A5) ───────────────────────────────────
@app.get("/safety/attacks")
async def safety_attacks() -> dict:
    """Canned attacks for the 'Try to jailbreak' demo tab."""
    return {"attacks": CANNED_ATTACKS}


@app.post("/safety/check")
async def safety_endpoint(req: SafetyCheckRequest) -> dict:
    """Run Foundry Guardrails (Microsoft.DefaultV2 policy attached to the
    configured deployment) on `text`. Falls back to a local heuristic if
    the Foundry project isn't reachable so the demo tab always lights up."""
    verdict = await safety_check(req.text)
    return verdict.to_dict()


# ── In-app observability (A8, B6) ──────────────────────────────────────────
@app.get("/build")
async def build() -> dict:
    """Git SHA + prompt version manifest currently running. Rendered in the
    web-tech footer so demo audiences can see the exact commit that shipped
    every prompt in the current turn (this is the human-readable half of B6)."""
    return {"build": build_info()}


@app.get("/traces/{sid}")
def traces(sid: str = APIPath(pattern=SESSION_ID_PATTERN), minutes: int = 15) -> dict:
    """Trace panel data for session `sid` — span tree, per-agent latency +
    reported token/cost metrics and a handoff diagram. Insurance never uses
    the retail compatibility stub when live telemetry is unavailable."""
    return get_trace(sid, minutes=max(1, min(minutes, 60)))
