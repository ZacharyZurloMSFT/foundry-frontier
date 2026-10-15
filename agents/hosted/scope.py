"""Per-request authority and streamed child events, never model-controlled state."""
from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

WORKFLOW = re.compile(r"wf-[A-Za-z0-9][A-Za-z0-9_-]{0,123}")


@dataclass
class RequestScope:
    workflow_id: str | None = None
    events: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=128))
    failures: list[str] = field(default_factory=list)
    approval: dict | None = None
    tool_results: int = 0
    relay_enabled: bool = False
    provider_model: str | None = None
    provider_response_id: str | None = None

    @classmethod
    def from_metadata(cls, metadata: Any) -> RequestScope:
        metadata = metadata or {}
        if not isinstance(metadata, dict):
            raise ValueError("Response metadata must be an object")
        workflow = metadata.get("workflow_id")
        if workflow is not None and (not isinstance(workflow, str) or not WORKFLOW.fullmatch(workflow)):
            raise ValueError("Invalid application-owned workflow_id")
        return cls(workflow_id=workflow)

    def require_workflow(self) -> str:
        if self.workflow_id is None:
            raise ValueError("Response metadata.workflow_id is required to propose a resolution")
        return self.workflow_id


current_scope: ContextVar[RequestScope] = ContextVar("northwind_order_request")
