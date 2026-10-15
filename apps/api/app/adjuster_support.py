"""Read-only, extractive Search + identity-authenticated model RAG. No native HR fallback."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from azure.core.exceptions import AzureError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from openai import APIError

from .harness.diagnostics import log_failure
from .orchestrator_client import _async_credential, _extract_output_text, _project_client
from .repositories import _container, append_message
from .settings import get_settings

INDEX = "nw-insurance-adjuster-support"
OWNER = "northwind-adjuster-support-v1"
SEARCH_API = "2024-07-01"
MODELS = {"hr-gpt-5-4": "gpt-5.4", "hr-gpt-5-4-mini": "gpt-5.4-mini", "hr-kimi": "Kimi-K2.6"}
UNKNOWN = ("I don't have sufficient evidence in the current fictional employee and adjuster-support "
           "corpus. Ask the appropriate HR/IT contact or claims supervisor. I cannot determine coverage "
           "or claim outcomes, or execute communications, assignments or payments.")
router = APIRouter(prefix="/adjuster-support", tags=["adjuster-support"])
_active = 0
logger = logging.getLogger(__name__)
SUPPORT_ERRORS = (AzureError, httpx.HTTPError, APIError, TimeoutError, OSError, ValueError,
                  RuntimeError, TypeError, LookupError, yaml.YAMLError)


class SupportRetrievalError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__("Support retrieval failed; no evidence or model answer was produced.")
        self.status_code = status_code


def support_error_event():
    return {"kind": "error", "message": "Grounded support is unavailable or evidence failed validation. "
            "No fallback answer was generated; contact HR/IT or the claims supervisor."}


class SupportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, pattern=r"^adjuster-[a-z0-9-]{1,100}$")


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    quote: str = Field(min_length=1, max_length=3000)


class EvidenceAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    selections: list[Selection] = Field(max_length=4)
    unknown: bool


def _setting(name, default=None):
    return getattr(get_settings(), name, default)


def _query_key():
    value = _setting("adjuster_support_query_key", "")
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def prompt() -> str:
    relative = Path("data") / "northwind" / "departments" / "employee-help-desk" / "prompts" / "northwind-adjuster-support.yaml"
    canonical = next((p / relative for p in Path(__file__).resolve().parents if (p / relative).is_file()), None)
    path = canonical or Path(__file__).resolve().parent / "_agent_prompts" / "northwind-adjuster-support.yaml"
    definition = yaml.safe_load(path.read_text(encoding="utf-8"))
    if definition["index"] != INDEX:
        raise ValueError("Adjuster prompt must use the isolated support index.")
    return definition["instructions"]


def availability() -> dict:
    settings = get_settings()
    reasons = []
    if settings.business_domain != "insurance":
        reasons.append("Adjuster support requires insurance mode.")
    if _setting("adjuster_support_index", INDEX) != INDEX:
        reasons.append("The isolated adjuster-support index must be configured.")
    endpoint = urlsplit(_setting("adjuster_support_search_endpoint", "") or settings.search_endpoint)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path.rstrip("/")):
        reasons.append("An HTTPS Azure Search endpoint is required.")
    auth = _setting("adjuster_support_search_auth", "entra_id")
    if auth not in ("entra_id", "query_key") or (auth == "query_key" and not _query_key()):
        reasons.append("Explicit Search authentication must be configured; no key fallback is performed.")
    if not settings.foundry_project_endpoint or not settings.cosmos_endpoint:
        reasons.append("Foundry model and conversation storage endpoints are required.")
    try:
        prompt()
    except (OSError, ValueError, KeyError, yaml.YAMLError):
        reasons.append("The canonical adjuster-support prompt was not packaged.")
    enabled = _setting("adjuster_support_enabled", False)
    return {"enabled": enabled, "state": "disabled" if not enabled else "unavailable" if reasons else "configured",
            "message": "; ".join(reasons) or "Read-only Search + model retrieval; no native retail HR agents.",
            "index": INDEX, "models": MODELS, "business_domain": "insurance", "review_scope": "read_only"}


@router.get("/status")
async def status():
    return availability()


async def retrieve(question: str) -> list[dict]:
    settings = get_settings()
    endpoint = (_setting("adjuster_support_search_endpoint", "") or settings.search_endpoint).rstrip("/")
    headers = {"Content-Type": "application/json"}
    if _setting("adjuster_support_search_auth", "entra_id") == "query_key":
        if not _query_key():
            raise RuntimeError("Explicit read-only Search query key is missing.")
        headers["api-key"] = _query_key()
    else:
        try:
            token = await _async_credential().get_token("https://search.azure.com/.default")
        except (AzureError, OSError, RuntimeError) as error:
            log_failure(logger, "adjuster_support", "search_authentication", error)
            raise
        headers["Authorization"] = "Bearer " + token.token
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(
            f"{endpoint}/indexes/{INDEX}/docs/search?api-version={SEARCH_API}",
            headers=headers, json={
                "search": question, "queryType": "simple", "searchMode": "any", "top": 5,
                "select": "id,title,content,source_uri,version,category,effective_from",
                "filter": f"publisher eq '{OWNER}' and business_domain eq 'insurance' and synthetic eq true",
            },
        )
    if response.status_code != 200:
        raise SupportRetrievalError(response.status_code)
    rows = response.json().get("value")
    if not isinstance(rows, list):
        raise RuntimeError("Support retrieval returned malformed evidence.")
    evidence = []
    for row in rows[:5]:
        if not isinstance(row, dict) or not isinstance(row.get("@search.score"), (float, int)):
            raise RuntimeError("Support evidence has invalid search provenance.")
        if row["@search.score"] <= 0:
            continue
        if (any(not isinstance(row.get(key), str) or not row[key].strip()
                for key in ("id", "title", "content", "source_uri", "version", "category", "effective_from"))
                or not row["source_uri"].startswith("urn:northwind:synthetic:employee-support:")
                or row["category"] not in ("hr", "it", "adjuster")
                or len(row["content"]) > 20000):
            raise RuntimeError("Support evidence has invalid source metadata.")
        evidence.append({key: row[key] for key in
                         ("id", "title", "content", "source_uri", "version", "category", "effective_from")})
    if len({d["id"] for d in evidence}) != len(evidence):
        raise RuntimeError("Support evidence contains ambiguous source identifiers.")
    return evidence


async def select_evidence(question: str, evidence: list[dict], history: list[dict], model: str) -> EvidenceAnswer:
    # No key kwargs, hosted agent_name, native HR agent_reference or execution tools.
    stage = "model_client"
    try:
        async with _project_client() as project:
            async with project.get_openai_client() as client:
                stage = "model_request"
                response = await client.responses.create(
                    model=model, store=False, max_output_tokens=1800,
                    input=[
                        {"type": "message", "role": "developer",
                         "content": [{"type": "input_text", "text": prompt()}]},
                        {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": json.dumps({
                             "current_question": question, "history_not_evidence": history[-12:],
                             "retrieved_evidence": evidence,
                         })}]},
                    ],
                )
        stage = "model_output_validation"
        if response.status != "completed":
            raise RuntimeError("Support model did not complete; no answer was confirmed.")
        return EvidenceAnswer.model_validate_json(_extract_output_text(response))
    except SUPPORT_ERRORS as error:
        log_failure(logger, "adjuster_support", stage, error)
        raise


async def load_support_history(session_id: str) -> list[dict]:
    container = await _container("conversations")
    rows = [
        {"role": row["role"], "content": row["content"][:3000]}
        async for row in container.query_items(
            query="SELECT TOP 12 c.role, c.content FROM c WHERE c.sessionId = @sid ORDER BY c.ts DESC",
            parameters=[{"name": "@sid", "value": session_id}], partition_key=session_id,
        ) if row.get("role") in ("user", "assistant") and isinstance(row.get("content"), str)
    ]
    return list(reversed(rows))


def render_answer(answer: EvidenceAnswer, evidence: list[dict]) -> tuple[str, list[dict]]:
    if answer.unknown:
        if answer.selections:
            raise RuntimeError("Support model returned contradictory evidence.")
        return UNKNOWN, []
    if not answer.selections:
        raise RuntimeError("Support model returned no grounding.")
    sources = {source["id"]: source for source in evidence}
    citations, excerpts = [], []
    for selection in answer.selections:
        source = sources.get(selection.source_id)
        if source is None or selection.quote not in source["content"]:
            raise RuntimeError("Support model produced an ungrounded excerpt; no answer was shown.")
        citation = {key: source[key] for key in
                    ("id", "title", "source_uri", "version", "category", "effective_from")}
        if citation not in citations:
            citations.append(citation)
        excerpts.append(f'"{selection.quote}"\n[{source["title"]}, version {source["version"]}, '
                        f'effective {source["effective_from"]}] ({source["source_uri"]})')
    return ("Retrieved fictional guidance:\n\n" + "\n\n".join(excerpts)
            + "\n\nRead-only employee/adjuster support, not policy adjudication or execution."), citations


async def run_support_turn(session_id: str, message: str, slug: str = "hr-gpt-5-4") -> AsyncIterator[dict]:
    """Evidence-only turn; HTTP integrations must use support_chat for capacity/cancellation controls."""
    state = availability()
    if state["state"] not in ("configured", "ready"):
        raise HTTPException(503, "Grounded support is not configured and enabled.")
    if slug not in MODELS:
        raise HTTPException(404, "Unknown support model.")
    if not session_id.startswith(f"adjuster-{slug}-"):
        raise HTTPException(422, "Support sessions are isolated per model.")
    yield {"kind": "session", "session_id": session_id, "business_domain": "insurance"}
    stage = "history_load"
    try:
        async with asyncio.timeout(_setting("adjuster_support_timeout", 60)):
            history = await load_support_history(session_id)
            stage = "history_save_user"
            await append_message(session_id, "user", message, {"support": INDEX, "model": MODELS[slug]})
            stage = "search_retrieval"
            evidence = await retrieve(message)
            if evidence:
                stage = "model_selection"
                answer = await select_evidence(message, evidence, history, MODELS[slug])
                stage = "answer_validation"
                text, citations = render_answer(answer, evidence)
            else:
                text, citations = UNKNOWN, []
            stage = "history_save_assistant"
            await append_message(session_id, "assistant", text,
                                 {"support": INDEX, "model": MODELS[slug], "citations": citations})
            stage = "stream_emit"
            yield {"kind": "assistant_delta", "text": text, "agent": "northwind-insurance-adjuster-support"}
            yield {"kind": "citations", "citations": citations}
            yield {"kind": "done"}
    except asyncio.CancelledError:
        raise
    except SUPPORT_ERRORS as error:
        log_failure(logger, "adjuster_support", stage, error, session_id)
        yield support_error_event()
        yield {"kind": "done"}
    except Exception as error:
        # Keep the HTTP stream bounded even for unexpected SDK failures, without recording their contents.
        log_failure(logger, "adjuster_support", stage, error, session_id)
        yield support_error_event()
        yield {"kind": "done"}


async def support_chat(req: SupportRequest, slug: str = "hr-gpt-5-4", request: Request | None = None):
    global _active
    state = availability()
    if state["state"] not in ("configured", "ready"):
        raise HTTPException(503, state["message"] if state["enabled"] else "Adjuster support is operator-disabled.")
    if slug not in MODELS:
        raise HTTPException(404, "Unknown support model.")
    sid = req.session_id or f"adjuster-{slug}-{uuid.uuid4()}"
    if not sid.startswith(f"adjuster-{slug}-"):
        raise HTTPException(422, "Support sessions are isolated per model.")
    if _active >= _setting("adjuster_support_concurrency", 4):
        raise HTTPException(429, "Support capacity reached; try again after the active request finishes.")

    async def stream():
        global _active
        if _active >= _setting("adjuster_support_concurrency", 4):
            yield (json.dumps({"kind": "error", "message": "Support capacity reached."}) + "\n").encode()
            yield b'{"kind":"done"}\n'
            return
        _active += 1
        events = run_support_turn(sid, req.message, slug)
        try:
            async for event in events:
                if request and await request.is_disconnected():
                    raise asyncio.CancelledError
                yield (json.dumps(event) + "\n").encode()
        finally:
            try:
                await events.aclose()
            finally:
                _active -= 1

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/chat")
async def chat(req: SupportRequest, request: Request):
    return await support_chat(req, request=request)


@router.post("/{slug}/chat")
async def model_chat(slug: str, req: SupportRequest, request: Request):
    return await support_chat(req, slug, request)
