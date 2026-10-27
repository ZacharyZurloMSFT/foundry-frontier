"""Customer Help Center: extractive answers over an isolated, general-purpose help index.

No personal record is read here. The model only *selects* verbatim excerpts from the retrieved articles and the
server renders every word shown from those articles, so an answer is exactly what Northwind published. Anything
that is missing, unverifiable or that reads like a coverage/claim/payment decision fails closed.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import model_gateway
from ..foundry_client import _async_credential, _extract_output_text, model_client
from ..settings import get_settings
from .guards import GuardFailure, strip_fence, unsafe_determination
from .models import Contract, DeltaEvent, DoneEvent, HelpSource, Provenance, SourcesEvent
from .profiles import HELP_INDEX, AssistantProfile, load_prompt, model_name
from .runtime import MAX_OUTPUT_TOKENS, TurnTrace, message_item, require_completed
from .scope import Caller

INDEX = HELP_INDEX
OWNER = "northwind-help-center-customer-v1"
SOURCE_PREFIX = "urn:northwind:synthetic:help-center:"
SEARCH_API = "2024-07-01"
SEARCH_SCOPE = "https://search.azure.com/.default"
FILTER = f"publisher eq '{OWNER}' and business_domain eq 'insurance' and synthetic eq true and audience eq 'customer'"
FIELDS = ("id", "title", "content", "source_uri", "version", "topic", "effective_from")
SOURCE_FIELDS = ("id", "title", "version", "topic", "effective_from", "source_uri")
MAX_EVIDENCE = 5
MAX_CONTENT = 20000
MAX_SELECTIONS = 4
# Four quotes plus titles and framing stay below the history content limit, so a shown answer is saved whole.
MAX_QUOTE = 600
MAX_TITLE = 80
MIN_QUOTE_WORDS = 4
MAX_TERMS = 24
LEAD = "Here is what the Help Center says:"
UNKNOWN = ("I couldn't find that in the Help Center articles, so I won't guess. For questions about your own "
           "policy or claim, open it in the portal or contact Northwind Claims. I can't decide coverage, "
           "approve claims or promise payment.")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_TOPIC = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TERM = re.compile(r"[^\W_]+")


class SearchError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("Help retrieval failed; no evidence or model answer was produced.")
        self.status_code = status_code


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(min_length=1, max_length=64)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE)


class EvidenceAnswer(BaseModel):
    """The only shape the model may return: which excerpts to show, never free text."""

    model_config = ConfigDict(extra="forbid", strict=True)
    selections: list[Selection] = Field(max_length=MAX_SELECTIONS)
    unknown: bool


def _ungrounded() -> GuardFailure:
    return GuardFailure("ungrounded_answer", "The Help Center answer could not be checked against its articles, so nothing was shown.")


def _withheld() -> GuardFailure:
    return GuardFailure(
        "unsafe_determination",
        "That excerpt could be read as a decision about coverage or a claim, so it was withheld. "
        "A person makes those decisions; contact Northwind Claims.")


def search_text(question: str) -> str:
    """Plain words only: Azure Search operators in a question must not change how the query is parsed."""
    return " ".join(_TERM.findall(question)[:MAX_TERMS])


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=False)


async def _headers() -> dict[str, str]:
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    if settings.help_center_search_auth == "query_key":
        key = settings.help_center_query_key.get_secret_value()
        if not key:
            raise RuntimeError("Explicit read-only Search query key is missing.")
        headers["api-key"] = key
    else:
        # Foundry and adjuster support also use this cached credential; a request must not close it.
        token = await _async_credential().get_token(SEARCH_SCOPE)
        headers["Authorization"] = "Bearer " + token.token
    return headers


def validate_evidence(rows: Any) -> list[dict[str, str]]:
    """Trust nothing from the index: every row must carry full, well-formed provenance or the turn fails."""
    if not isinstance(rows, list):
        raise RuntimeError("Help retrieval returned malformed evidence.")
    evidence: list[dict[str, str]] = []
    for row in rows[:MAX_EVIDENCE]:
        if not isinstance(row, dict) or not isinstance(row.get("@search.score"), (int, float)):
            raise RuntimeError("Help evidence has invalid search provenance.")
        if row["@search.score"] <= 0:
            continue
        if (any(not isinstance(row.get(key), str) or not row[key].strip() for key in FIELDS)
                or not _ID.fullmatch(row["id"]) or not _TOPIC.fullmatch(row["topic"])
                or not _DATE.fullmatch(row["effective_from"]) or len(row["version"]) > 32 or len(row["title"]) > MAX_TITLE
                or not row["source_uri"].startswith(SOURCE_PREFIX) or len(row["content"]) > MAX_CONTENT):
            raise RuntimeError("Help evidence has invalid source metadata.")
        evidence.append({key: row[key] for key in FIELDS})
    if len({row["id"] for row in evidence}) != len(evidence):
        raise RuntimeError("Help evidence contains ambiguous source identifiers.")
    return evidence


async def retrieve(question: str) -> list[dict[str, str]]:
    query = search_text(question)
    if not query:
        return []
    settings = get_settings()
    endpoint = (settings.help_center_search_endpoint or settings.search_endpoint).rstrip("/")
    headers = await _headers()
    async with _http() as client:
        response = await client.post(
            f"{endpoint}/indexes/{INDEX}/docs/search?api-version={SEARCH_API}", headers=headers,
            json={"search": query, "queryType": "simple", "searchMode": "any", "top": MAX_EVIDENCE,
                  "select": ",".join(FIELDS), "filter": FILTER},
        )
    if response.status_code != 200:
        raise SearchError(response.status_code)
    body = response.json()
    return validate_evidence(body.get("value") if isinstance(body, dict) else None)


def parse_selection(text: str) -> EvidenceAnswer:
    try:
        return EvidenceAnswer.model_validate_json(strip_fence(text))
    except ValidationError as error:
        raise _ungrounded() from error


async def select_evidence(
    question: str, evidence: list[dict[str, str]], history: list[dict[str, str]], model: str, instructions: str,
) -> EvidenceAnswer:
    """One tool-less model call. Evidence and history are data; the prompt never lets them act as instructions."""
    request: dict[str, Any] = {
        "model": model, "store": False, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "input": [
            message_item("developer", instructions),
            message_item("user", json.dumps({
                "current_question": question, "history_not_evidence": history[-12:],
                "retrieved_evidence": evidence,
            }, ensure_ascii=False)),
        ],
    }
    async with model_client(get_settings(), model) as (client, outbound_model):
        request["model"] = outbound_model
        response = await client.responses.create(**request)
    require_completed(response)
    return parse_selection(_extract_output_text(response))


def render_answer(answer: EvidenceAnswer, evidence: list[dict[str, str]]) -> tuple[str, list[HelpSource]]:
    """Return text built only from verbatim, retrieved article excerpts, plus the articles they came from."""
    if answer.unknown:
        if answer.selections:
            raise _ungrounded()
        return UNKNOWN, []
    if not answer.selections:
        raise _ungrounded()
    articles = {row["id"]: row for row in evidence}
    blocks: list[str] = []
    sources: dict[str, HelpSource] = {}
    for selection in answer.selections:
        article = articles.get(selection.source_id)
        quote = selection.quote.strip()
        if article is None or len(quote.split()) < MIN_QUOTE_WORDS or quote not in article["content"]:
            raise _ungrounded()
        block = f"\u201c{quote}\u201d\n\u2014 {article['title']}"
        if block not in blocks:
            blocks.append(block)
        sources.setdefault(article["id"], HelpSource(**{key: article[key] for key in SOURCE_FIELDS}))
    text = "\n\n".join([LEAD, *blocks])
    if unsafe_determination(text):
        raise _withheld()
    return text, list(sources.values())


async def run_help_turn(
    *, profile: AssistantProfile, caller: Caller, message: str, history: list[dict[str, str]],
    session_id: str, persist: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> AsyncGenerator[Contract, None]:
    """Yield only validated events. As with scoped turns, `persist` runs before anything is shown."""
    settings = get_settings()
    stage = "prompt"
    turn = TurnTrace(profile, caller.role, caller.actor_key, session_id)
    try:
        instructions, prompt_sha = load_prompt(profile)
        model = model_name()
        model_gateway.stamp_span(turn.span, settings, model)
        async with asyncio.timeout(settings.assistant_timeout):
            stage = "search_retrieval"
            with turn.active():
                evidence = await retrieve(message)
            turn.span.set_attribute("assistant.evidence", len(evidence))
            used = "none"
            if evidence:
                stage = "model_selection"
                with turn.active():
                    selected = await select_evidence(message, evidence, history, model, instructions)
                stage = "answer_validation"
                text, sources = render_answer(selected, evidence)
                used = model
            else:
                text, sources = UNKNOWN, []
            stage = "history_save"
            await persist(text, {"model": used, "prompt_sha": prompt_sha[:12], "index": INDEX,
                                 "sources": [source.id for source in sources]})
        turn.outcome = "answered" if sources else "unknown"
        yield DeltaEvent(text=text, agent=profile.agent)
        yield SourcesEvent(sources=sources)
        yield DoneEvent(provenance=Provenance(model=used, prompt_sha=prompt_sha[:12], tool_calls=0, human_review=False))
    except asyncio.CancelledError:
        turn.outcome = "cancelled"
        raise
    except Exception as error:  # noqa: BLE001 - every provider/SDK failure fails closed with a bounded stream
        yield turn.fail(error, stage)
        yield DoneEvent()
    finally:
        turn.close()
