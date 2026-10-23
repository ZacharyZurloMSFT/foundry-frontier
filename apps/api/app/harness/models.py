from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

Specialist = Literal["triage", "policy_lookup", "policy_claim_lookup", "claim_action_draft"]
RunState = Literal["running", "succeeded", "failed", "cancelled", "interrupted"]
MAX_EVENTS = 4096
MAX_EVENT_BYTES = 1024 * 1024
MAX_RESULT_CHARS = 32000


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(text: str) -> str:
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    return re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b",
                  "[REDACTED]", text)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    message: str = Field(min_length=1, max_length=8000)

    @field_validator("message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value.strip()


class SpecialistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=16000)


class EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = 0
    ts: str = Field(default_factory=now)
    run_id: str = ""


class Started(EventBase):
    kind: Literal["run_started"] = "run_started"
    workflow_id: str
    model: str
    prompt_sha: str


class Text(EventBase):
    kind: Literal["assistant_delta", "assistant_message"]
    text: str


class ToolEvent(EventBase):
    kind: Literal["tool_start", "tool_result", "tool_error"]
    tool: Specialist
    call_id: str
    text: str = ""
    duration_ms: int | None = None


class Usage(EventBase):
    kind: Literal["usage"] = "usage"
    source: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class Approval(EventBase):
    kind: Literal["approval_pending"] = "approval_pending"
    hitl_id: str
    workflow_id: str
    draft: dict
    action_kind: Literal["resolution", "insurance_claim_action"] = "resolution"
    execution_status: Literal["not_executed"] = "not_executed"


class Error(EventBase):
    kind: Literal["error"] = "error"
    code: str
    message: str


class Finished(EventBase):
    kind: Literal["done"] = "done"
    status: RunState


Event = Annotated[Started | Text | ToolEvent | Usage | Approval | Error | Finished,
                  Field(discriminator="kind")]
EVENT_ADAPTER = TypeAdapter(Event)


class HarnessError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
