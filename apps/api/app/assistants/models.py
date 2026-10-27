"""Wire contracts for identity-bound assistants.

Identity, role and customer IDs are never request fields: the server derives them from the
verified Entra token and the run's actor mapping. Callers may only *hint* which of their own
records a conversation is about; every hint is re-authorised before a model sees anything.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

Surface = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,31}$")]
ClaimId = Annotated[str, StringConstraints(pattern=r"^CLM-\d+$")]
PolicyId = Annotated[str, StringConstraints(pattern=r"^POL-\d+$")]
SESSION_PATTERN = r"^asst-[a-z][a-z_]{0,31}-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
SessionId = Annotated[str, StringConstraints(pattern=SESSION_PATTERN)]
STANDING_NOTICE = ("Synthetic demonstration. This is not a coverage decision, legal advice or a promise to pay; "
                   "a person reviews every claim.")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChatContext(Contract):
    claim_id: ClaimId | None = None
    policy_id: PolicyId | None = None


class ChatRequest(Contract):
    message: str = Field(min_length=1, max_length=4000)
    session_id: SessionId | None = None
    context: ChatContext = Field(default_factory=ChatContext)


class SurfaceStatus(Contract):
    surface: str
    label: str
    available: bool
    reason: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    accepts: list[Literal["claim", "policy"]] = Field(default_factory=list)


class AssistantStatus(Contract):
    enabled: bool
    notice: str
    surfaces: list[SurfaceStatus] = Field(default_factory=list)


class ScopeSummary(Contract):
    """`own` = the caller's own records; `none` = a surface that reads no personal records at all."""

    records: Literal["own", "none"] = "own"
    claim_id: str | None = None
    policy_id: str | None = None


class SessionEvent(Contract):
    kind: Literal["session"] = "session"
    session_id: str
    surface: str
    scope: ScopeSummary


class ToolEvent(Contract):
    kind: Literal["tool"] = "tool"
    name: str
    status: Literal["ok", "not_found", "invalid", "error"]
    label: str


class DeltaEvent(Contract):
    kind: Literal["assistant_delta"] = "assistant_delta"
    text: str
    agent: str


class Citation(Contract):
    policy_ref: str
    title: str
    version: str
    state: str
    effective_from: str
    effective_to: str
    section_id: str
    source_uri: str
    text: str


class CitationsEvent(Contract):
    kind: Literal["citations"] = "citations"
    citations: list[Citation]


class HelpSource(Contract):
    id: str
    title: str
    version: str
    topic: str
    effective_from: str
    source_uri: str


class SourcesEvent(Contract):
    """The help articles an answer was quoted from; one entry per article, in the order first quoted."""

    kind: Literal["sources"] = "sources"
    sources: list[HelpSource]


class ErrorEvent(Contract):
    kind: Literal["error"] = "error"
    code: str
    message: str


class Provenance(Contract):
    model: str
    prompt_sha: str
    tool_calls: int = Field(ge=0)
    human_review: bool


class DoneEvent(Contract):
    kind: Literal["done"] = "done"
    notice: str = STANDING_NOTICE
    provenance: Provenance | None = None


Event = Annotated[
    SessionEvent | ToolEvent | DeltaEvent | CitationsEvent | SourcesEvent | ErrorEvent | DoneEvent,
    Field(discriminator="kind"),
]
EVENTS: TypeAdapter[Event] = TypeAdapter(Event)


class AssistantAnswer(Contract):
    """The only shape a model may return; the server renders citations itself."""

    answer: str = Field(min_length=1, max_length=2400)
    citations: list[str] = Field(default_factory=list, max_length=6)
    human_review: bool


def encode(event: Contract) -> bytes:
    return (event.model_dump_json() + "\n").encode()
