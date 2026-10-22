from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import anyio
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import StreamingResponse

from .. import model_gateway
from ..observability import get_tracer, set_request_department
from ..settings import get_settings
from .diagnostics import log_failure
from .models import (
    MAX_EVENT_BYTES,
    MAX_EVENTS,
    MAX_RESULT_CHARS,
    Error,
    Event,
    Finished,
    HarnessError,
    RunRequest,
    Started,
    Text,
)
from .runtime import instructions, runtime
from .store import RunStore

router = APIRouter(prefix="/harness", tags=["Agent Harness"])
store = RunStore()
logger = logging.getLogger(__name__)
RunId = Annotated[str, Path(pattern=r"^harness-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]


@router.get("/status")
async def status() -> dict:
    return runtime.status()


@router.get("/runs/{run_id}")
async def get_run(run_id: RunId) -> dict:
    try:
        return await store.snapshot(run_id)
    except CosmosResourceNotFoundError as exc:
        raise HTTPException(404, "Saved harness run not found or expired.") from exc
    except Exception as exc:
        log_failure(logger, "harness", "store_snapshot", exc, run_id)
        raise HTTPException(503, "Run storage is unavailable.") from exc


@router.post("/runs")
async def run(request: Request, body: RunRequest) -> StreamingResponse:
    settings = get_settings()
    availability = runtime.status()
    if availability["state"] in {"disabled", "unavailable"}:
        raise HTTPException(503, availability["message"] if settings.harness_enabled
                            else "Agent Harness is disabled. Set COPILOT_HARNESS_ENABLED=true.")
    if runtime.active >= settings.harness_concurrency:
        raise HTTPException(429, "Harness capacity reached; wait for an active run to finish.")
    _, prompt_sha = instructions(settings)
    model = settings.harness_model or settings.planner_deployment
    try:
        saved, created = await store.create(body, model, prompt_sha, settings.harness_timeout)
    except HarnessError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        log_failure(logger, "harness", "store_create", exc)
        raise HTTPException(503, "Run storage is unavailable; no model was invoked.") from exc
    run_id, wid = saved["sessionId"], saved["workflowId"]
    if not created:
        raise HTTPException(409, {"message": "Request already exists; load the saved run instead.",
                                  "run_id": run_id, "status": saved["status"]})

    async def stream():
        seq, byte_count = 0, 0
        output = ""
        final_state = "failed"
        failure: str | None = None
        acquired = False
        events = None
        set_request_department("order-concierge", settings.order_project_name)

        async def encode(event: Event) -> bytes:
            nonlocal seq, byte_count
            seq += 1
            event = event.model_copy(update={"seq": seq, "run_id": run_id})
            wire = (event.model_dump_json() + "\n").encode()
            byte_count += len(wire)
            if seq > MAX_EVENTS or byte_count > MAX_EVENT_BYTES:
                raise HarnessError("event_limit", "The run exceeded its saved-event budget.")
            await store.append(run_id, event)
            return wire

        try:
            yield await encode(Started(workflow_id=wid, model=model, prompt_sha=prompt_sha))
            if runtime.active >= settings.harness_concurrency:
                raise HarnessError("capacity", "Harness capacity reached; no model was invoked.")
            runtime.active += 1
            acquired = True
            with get_tracer("northwind.harness").start_as_current_span("harness.run", attributes={
                "session_id": run_id, "workflow_id": wid, "harness": "copilot-sdk",
                "model": model, "prompt_sha.copilot-harness": prompt_sha,
                **model_gateway.span_attributes(settings, model),
            }, record_exception=False, set_status_on_exception=False):
                async with asyncio.timeout(settings.harness_timeout):
                    events = runtime.run(run_id, wid, body.message, store)
                    async for event in events:
                        if await request.is_disconnected():
                            raise asyncio.CancelledError
                        if isinstance(event, Text):
                            output = event.text if event.kind == "assistant_message" else output + event.text
                            if len(output) > MAX_RESULT_CHARS:
                                raise HarnessError("output_limit", "The assistant exceeded its output budget.")
                        yield await encode(event)
                final_state = "succeeded"
        except (asyncio.CancelledError, GeneratorExit):
            final_state = "cancelled"
            failure = "Stopped or disconnected. A draft may already exist; inspect saved approval status."
            raise
        except Exception as exc:
            log_failure(logger, "harness", "stream_run", exc, run_id)
            failure = (str(exc) if isinstance(exc, HarnessError) else
                       "Run deadline exceeded." if isinstance(exc, TimeoutError) else
                       "Run failed. Check runtime/provider/storage access and server logs.")
            error = Error(run_id=run_id, seq=seq + 1,
                          code=exc.code if isinstance(exc, HarnessError) else "run_failed",
                          message=failure)
            try:
                await store.append(run_id, error)
            except Exception as persistence_error:
                log_failure(logger, "harness", "store_error_event", persistence_error, run_id)
            seq += 1
            yield (error.model_dump_json() + "\n").encode()
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    if events is not None:
                        await events.aclose()
                except Exception as cleanup_error:
                    log_failure(logger, "harness", "stream_cleanup", cleanup_error, run_id)
                    final_state, failure = "failed", "Runtime cleanup failed; inspect server logs."
                finally:
                    if acquired:
                        runtime.active -= 1
                    try:
                        async with asyncio.timeout(15):
                            saved = await store.finish(run_id, final_state, output, failure)
                            final_state = saved["status"]
                            await store.append(run_id, Finished(run_id=run_id, seq=seq + 1, status=final_state))
                            logger.info("AI-conversation", extra={
                                "harness": "copilot-sdk", "department": "order-concierge",
                                "foundry_project": settings.order_project_name, "session_id": run_id,
                                "workflow": "harness", "execution_status": final_state,
                                "prompt_sha": prompt_sha,
                            })
                    except Exception as persistence_error:
                        log_failure(logger, "harness", "store_terminal_state", persistence_error, run_id)
                        final_state = "interrupted"
        if final_state == "interrupted":
            seq += 1
            yield (Error(run_id=run_id, seq=seq, code="persistence_uncertain",
                         message="Completion could not be confirmed. Inspect the saved run and approval before retrying.")
                   .model_dump_json() + "\n").encode()
        yield (Finished(run_id=run_id, seq=seq + 1, status=final_state).model_dump_json() + "\n").encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
