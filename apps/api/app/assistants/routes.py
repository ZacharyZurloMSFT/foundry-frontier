"""Authenticated assistant routes, mounted beside the connected insurance business API.

They reuse the business route class, Entra bearer verification and `X-Demo-Run` context, so errors share the
typed envelope and no assistant can be reached with an identity the portal itself would refuse.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import StreamingResponse

from ..business.models import BusinessError, ErrorResponse
from ..business.routes import BusinessRoute, Context
from .extractive import run_help_turn
from .history import append_turn, load_history, new_session_id, session_surface
from .models import AssistantStatus, ChatContext, ChatRequest, Contract, ScopeSummary, SessionEvent, SurfaceStatus, encode
from .profiles import PROFILES, AssistantProfile, enabled_surfaces, unavailable_reason
from .runtime import check_capacity, run_turn, serve
from .scope import open_caller, open_scope

router = APIRouter(
    prefix="/api/v1/assistants", tags=["insurance-assistants"], route_class=BusinessRoute,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 429, 500, 503)},
)
SurfacePath = Annotated[str, Path(pattern=r"^[a-z][a-z_]{0,31}$")]
NOTICE = "Assistants never decide coverage or claims and cannot change anything. A person reviews every claim."
Persist = Callable[[str, dict[str, Any]], Awaitable[None]]
TurnFactory = Callable[[str, list[dict[str, str]], str, Persist], AsyncGenerator[Contract, None]]


def profile_for(surface: str) -> AssistantProfile:
    profile = PROFILES.get(surface)
    if profile is None or surface not in enabled_surfaces():
        raise BusinessError("assistant_not_found", "This assistant is not available.", 404)
    if unavailable_reason(profile):
        raise BusinessError("assistant_unavailable", "This assistant is not configured right now.", 503)
    return profile


async def bind(profile: AssistantProfile, ctx: Context, context: ChatContext) -> tuple[str, ScopeSummary, TurnFactory]:
    """Authorise the caller for this surface; return its actor, what it may read and how to run a turn."""
    if profile.kind == "extractive_help":
        caller = await open_caller(ctx, profile)
        if context.claim_id or context.policy_id:
            raise BusinessError("context_not_supported", "This assistant does not use your claims or policies.", 422)

        def help_turn(message: str, history: list[dict[str, str]], session_id: str, persist: Persist):
            return run_help_turn(profile=profile, caller=caller, message=message, history=history,
                                 session_id=session_id, persist=persist)
        return caller.actor_key, ScopeSummary(records="none"), help_turn

    reader = await open_scope(ctx, profile, context)

    def scoped_turn(message: str, history: list[dict[str, str]], session_id: str, persist: Persist):
        return run_turn(profile=profile, reader=reader, message=message, history=history,
                        session_id=session_id, persist=persist)
    return reader.scope.actor_key, reader.scope.summary(), scoped_turn


@router.get("/status")
async def status(ctx: Context) -> AssistantStatus:
    enabled = enabled_surfaces()
    if not enabled:
        return AssistantStatus(enabled=False, notice="Assistants are not enabled.")
    account = await ctx.service.account_context(ctx.run_id, ctx.subject_id)
    surfaces = []
    for name in sorted(enabled):
        profile = PROFILES[name]
        if account.role not in profile.roles:
            continue
        reason = unavailable_reason(profile)
        surfaces.append(SurfaceStatus(
            surface=name, label=profile.label, available=reason is None, reason=reason,
            suggestions=list(profile.suggestions), accepts=list(profile.accepts),
        ))
    return AssistantStatus(enabled=True, notice=NOTICE, surfaces=surfaces)


@router.post("/{surface}/chat")
async def chat(surface: SurfacePath, body: ChatRequest, ctx: Context, request: Request) -> StreamingResponse:
    profile = profile_for(surface)
    actor, scope, turn_for = await bind(profile, ctx, body.context)
    session_id = body.session_id or new_session_id(surface)
    if body.session_id and session_surface(session_id) != surface:
        raise BusinessError("session_mismatch", "This conversation belongs to a different assistant.", 422)
    history = await load_history(session_id, actor) if body.session_id else []
    check_capacity(actor)

    async def persist(answer: str, meta: dict[str, Any]) -> None:
        await append_turn(session_id, actor, surface, body.message, answer, meta)

    async def stream() -> AsyncIterator[bytes]:
        yield encode(SessionEvent(session_id=session_id, surface=surface, scope=scope))
        events = serve(actor_key=actor, events=turn_for(body.message, history, session_id, persist),
                       is_disconnected=request.is_disconnected)
        try:
            async for event in events:
                yield encode(event)
        finally:
            await events.aclose()

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
