"""Bounded, fail-closed model turn over identity-bound tools.

Nothing the model writes is shown until it parses, cites only what it was shown, stays within the ids it
read, and avoids coverage/claim/payment determinations. Any failure yields an error event and no answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Any

from opentelemetry import trace

from ..business.models import BusinessError
from ..company import resolve_department
from ..harness.diagnostics import log_failure
from ..observability import get_tracer, set_request_department
from ..orchestrator_client import _extract_output_text, _project_client
from ..settings import get_settings
from .grounding import Grounding
from .guards import GuardFailure, parse_answer, vet_answer
from .models import CitationsEvent, Contract, DeltaEvent, DoneEvent, ErrorEvent, Provenance, ToolEvent
from .profiles import AssistantProfile, load_prompt, model_name
from .scope import ScopedReader
from .tools import ToolBox, ToolOutcome, dump

logger = logging.getLogger(__name__)
tracer = get_tracer("northwind.assistants")
MAX_OUTPUT_TOKENS = 1800
UNAVAILABLE = ("The assistant is unavailable or its answer could not be verified. "
               "No fallback answer was generated.")
FILTERED = GuardFailure(
    "content_filtered",
    "The safety filters blocked this request or its answer. Rephrase your question; no answer was generated.")
_active = 0
_busy_actors: set[str] = set()


def check_capacity(actor_key: str) -> None:
    if actor_key in _busy_actors or _active >= get_settings().assistant_concurrency:
        raise BusinessError("assistant_busy", "An assistant request is already running. Try again in a moment.", 429)


def _reserve(actor_key: str) -> None:
    global _active
    check_capacity(actor_key)
    _busy_actors.add(actor_key)
    _active += 1


def _release(actor_key: str) -> None:
    global _active
    if actor_key in _busy_actors:
        _busy_actors.discard(actor_key)
        _active = max(0, _active - 1)


def is_filtered(error: BaseException) -> bool:
    if getattr(error, "code", None) == "content_filter":
        return True
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(inner, dict) and inner.get("code") == "content_filter":
            return True
    return "content_filter" in str(error).casefold()


def message_item(role: str, payload: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": payload}]}


def _function_calls(response: Any) -> list[Any]:
    return [item for item in getattr(response, "output", None) or [] if getattr(item, "type", "") == "function_call"]


def require_completed(response: Any) -> None:
    if getattr(response, "status", None) == "completed":
        return
    if getattr(getattr(response, "incomplete_details", None), "reason", None) == "content_filter":
        raise GuardFailure(FILTERED.code, FILTERED.message)
    raise RuntimeError("The assistant model did not complete; no answer was confirmed.")


def stamp_department(profile: AssistantProfile) -> None:
    try:
        department = resolve_department(profile.department)
        set_request_department(department["slug"], department["foundry_project"])
    except Exception:  # noqa: BLE001 - never fail a turn because telemetry stamping failed
        pass


class TurnTrace:
    """One span and one `AI-conversation` log line per turn, closed however the stream ends."""

    def __init__(self, profile: AssistantProfile, role: str, actor_key: str, session_id: str) -> None:
        self.profile, self.session_id = profile, session_id
        self.outcome, self.calls = "failed", 0
        stamp_department(profile)
        self.span = tracer.start_span("assistant.turn")
        self.span.set_attribute("assistant.surface", profile.surface)
        self.span.set_attribute("assistant.role", role)
        self.span.set_attribute("assistant.actor", actor_key[:12])
        self.span.set_attribute("session_id", session_id)

    def active(self) -> AbstractContextManager[Any]:
        """Make the span current around awaited provider calls only; a generator must never yield inside it."""
        return trace.use_span(self.span, end_on_exit=False)

    def close(self) -> None:
        self.span.set_attribute("assistant.outcome", self.outcome)
        self.span.set_attribute("assistant.tool_calls", self.calls)
        self.span.end()
        logger.info("AI-conversation", extra={
            "assistant": self.profile.surface, "department": self.profile.department, "session_id": self.session_id,
            "workflow": "assistant", "execution_status": self.outcome, "tool_calls": self.calls,
        })


def failure_event(error: Exception, stage: str, session_id: str) -> tuple[str, ErrorEvent]:
    """Map any failure to a bounded error event; nothing a provider or model said is ever repeated."""
    if isinstance(error, GuardFailure):
        return error.code, ErrorEvent(code=error.code, message=error.message)
    log_failure(logger, "assistant", stage, error, session_id)
    if isinstance(error, TimeoutError):
        return "timeout", ErrorEvent(code="timeout", message="The assistant took too long, so nothing was shown. Try again.")
    if is_filtered(error):
        return "content_filtered", ErrorEvent(code=FILTERED.code, message=FILTERED.message)
    return "assistant_failed", ErrorEvent(code="assistant_failed", message=UNAVAILABLE)


async def run_turn(
    *, profile: AssistantProfile, reader: ScopedReader, message: str, history: list[dict[str, str]],
    session_id: str, persist: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> AsyncGenerator[Contract, None]:
    """Yield only validated events. `persist` runs before an answer is shown, so a shown answer is always saved."""
    settings = get_settings()
    limit = settings.assistant_max_tool_calls
    stage, calls = "prompt", 0
    turn = TurnTrace(profile, reader.scope.role, reader.scope.actor_key, session_id)
    try:
        instructions, prompt_sha = load_prompt(profile)
        model = model_name()
        grounding = Grounding()
        toolbox = ToolBox(reader, grounding, profile.tools)
        answer_text = ""
        async with asyncio.timeout(settings.assistant_timeout):
            stage = "pinned_context"
            pinned = await toolbox.pinned_context()
            items: list[dict[str, Any]] = [
                message_item("developer", instructions),
                message_item("user", json.dumps({
                    "current_question": message, "pinned_context": pinned, "history_not_evidence": history[-12:],
                }, ensure_ascii=False)),
            ]
            async with _project_client() as project:
                async with project.get_openai_client() as client:
                    for _ in range(limit + 1):
                        stage = "model_request"
                        request: dict[str, Any] = {
                            "model": model, "store": False, "max_output_tokens": MAX_OUTPUT_TOKENS,
                            "input": items, "tools": toolbox.specs(),
                            "tool_choice": "none" if calls >= limit else "auto",
                        }
                        with turn.active():
                            response = await client.responses.create(**request)
                        require_completed(response)
                        requested = _function_calls(response)
                        if not requested:
                            answer_text = _extract_output_text(response)
                            break
                        stage = "tool_execution"
                        for call in requested:
                            if calls >= limit:
                                result = ToolOutcome(
                                    dump({"error": "tool_limit", "message": "The tool-call budget for this answer is spent."}),
                                    ToolEvent(name="limit", status="error", label="Tool-call limit reached"))
                            else:
                                with turn.active():
                                    result = await toolbox.call(str(call.name), str(call.arguments))
                            calls += 1
                            yield result.event
                            items.append({"type": "function_call", "call_id": call.call_id,
                                          "name": call.name, "arguments": call.arguments})
                            items.append({"type": "function_call_output", "call_id": call.call_id,
                                          "output": result.output})
            stage = "answer_validation"
            answer = parse_answer(answer_text)
            citations = vet_answer(answer, grounding, message)
            stage = "history_save"
            await persist(answer.answer, {"model": model, "prompt_sha": prompt_sha[:12], "tool_calls": calls,
                                          "citations": [c.policy_ref for c in citations]})
        turn.outcome = "answered"
        yield DeltaEvent(text=answer.answer, agent=profile.agent)
        yield CitationsEvent(citations=citations)
        yield DoneEvent(provenance=Provenance(
            model=model, prompt_sha=prompt_sha[:12], tool_calls=calls, human_review=answer.human_review))
    except asyncio.CancelledError:
        turn.outcome = "cancelled"
        raise
    except Exception as error:  # noqa: BLE001 - every provider/SDK failure fails closed with a bounded stream
        turn.outcome, event = failure_event(error, stage, session_id)
        yield event
        yield DoneEvent()
    finally:
        turn.calls = calls
        turn.close()


async def serve(
    *, actor_key: str, events: AsyncGenerator[Contract, None], is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[Contract, None]:
    """Reserve capacity for the whole stream and release it however the stream ends."""
    try:
        _reserve(actor_key)
    except BusinessError as error:
        await events.aclose()
        yield ErrorEvent(code=error.code, message=str(error))
        yield DoneEvent()
        return
    try:
        async for event in events:
            if await is_disconnected():
                raise asyncio.CancelledError
            yield event
    finally:
        try:
            await events.aclose()
        finally:
            _release(actor_key)
