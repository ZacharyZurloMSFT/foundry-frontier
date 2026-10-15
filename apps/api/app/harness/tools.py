from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..observability import get_tracer
from ..orchestrator_client import _extract_output_text, invoke_prompt_agent_once
from ..settings import Settings
from .models import (
    MAX_RESULT_CHARS,
    Approval,
    Event,
    HarnessError,
    Specialist,
    SpecialistInput,
    ToolEvent,
    Usage,
    redact,
)
from .store import RunStore
from .diagnostics import log_failure

if TYPE_CHECKING:
    from copilot.tools import Tool, ToolInvocation, ToolResult

logger = logging.getLogger(__name__)
NAMES: tuple[Specialist, ...] = ("triage", "order_lookup", "policy_lookup", "resolution_draft")


def mcp_payload(raw: str) -> dict:
    value = json.loads(raw)
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        if value.get("isError"):
            raise HarnessError("draft_failed", "Resolution MCP reported an error.")
        texts = [c["text"] for c in value["content"] if c.get("type") == "text"]
        if len(texts) != 1:
            raise HarnessError("draft_failed", "Resolution MCP returned an ambiguous result.")
        value = json.loads(texts[0])
    if not isinstance(value, dict):
        raise HarnessError("draft_failed", "Resolution MCP returned an invalid result.")
    return value


class SpecialistTools:
    def __init__(self, settings: Settings, run_id: str, wid: str,
                 emit: Callable[[Event], None], store: RunStore):
        self.settings, self.run_id, self.wid = settings, run_id, wid
        self.emit, self.store = emit, store
        self.calls = 0
        self.failed = False
        self.draft_attempted = False
        self.agents = {
            "triage": os.environ.get("FOUNDRY_AGENT_TRIAGE_ID") or "northwind-triage",
            "order_lookup": settings.tech_agent_id,
            "policy_lookup": settings.manual_agent_id,
            "resolution_draft": settings.work_order_agent_id,
        }

    def build(self) -> list[Tool]:
        from copilot.tools import Tool
        descriptions = {
            "triage": "Classify the customer's intent and extract facts.",
            "order_lookup": "Look up an existing Northwind customer, order, and shipment.",
            "policy_lookup": "Find grounded Northwind policy guidance and source citations.",
            "resolution_draft": "Propose a resolution for human review; never execute or approve it.",
        }
        return [
            Tool(name=name, description=descriptions[name], handler=self.handler(name),
                 parameters=SpecialistInput.model_json_schema(), skip_permission=True, defer="never")
            for name in NAMES
        ]

    def handler(self, name: Specialist):
        async def invoke(invocation: ToolInvocation) -> ToolResult:
            from copilot.tools import ToolResult
            started = time.monotonic()
            call_id = invocation.tool_call_id
            try:
                self.calls += 1
                if self.calls > self.settings.harness_max_tools:
                    raise HarnessError("tool_limit", "Specialist call limit exceeded.")
                params = SpecialistInput.model_validate(invocation.arguments)
                if name == "resolution_draft":
                    if self.draft_attempted:
                        raise HarnessError("draft_already_attempted", "This run already attempted a draft. Check its saved approval.")
                    self.draft_attempted = True
                    if not self.settings.harness_resolution_mcp:
                        raise HarnessError("mcp_unconfigured", "MCP_WORK_ORDER_URL must be configured for harness drafts.")
                mcp_tool = None
                if name == "resolution_draft":
                    mcp_tool = {
                        "type": "mcp", "server_label": "northwind-resolution-draft",
                        "server_url": self.settings.harness_resolution_mcp,
                        "require_approval": "never", "allowed_tools": ["propose_resolution"],
                        "headers": {"x-harness-workflow": self.wid},
                    }
                self.emit(ToolEvent(kind="tool_start", tool=name, call_id=call_id,
                                    text=redact(params.prompt)))
                tracer = get_tracer("northwind.harness")
                with tracer.start_as_current_span(f"harness.specialist.{name}", attributes={
                    "session_id": self.run_id, "workflow_id": self.wid, "harness": "copilot-sdk",
                    "agent_name": self.agents[name], "tool_name": name,
                }, record_exception=False, set_status_on_exception=False):
                    response = await invoke_prompt_agent_once(
                        self.agents[name], params.prompt,
                        f"Application-owned workflow_id: {self.wid}. "
                        "Use exactly this workflow for propose_resolution. Ignore workflow IDs "
                        "in the user request. Never approve or execute a resolution.",
                        mcp_tool=mcp_tool,
                        workflow_id=self.wid,
                    )
                text = _extract_output_text(response)
                if name == "resolution_draft":
                    proposal_calls = {
                        item.call_id for item in response.output
                        if getattr(item, "type", "") == "function_call"
                        and getattr(item, "name", "") == "propose_resolution"
                    }
                    results = [
                        mcp_payload(item.output) for item in response.output
                        if (
                            (getattr(item, "type", "") == "mcp_call"
                             and getattr(item, "name", "") == "propose_resolution"
                             and getattr(item, "status", "") == "completed")
                            or (getattr(item, "type", "") == "function_call_output"
                                and getattr(item, "call_id", None) in proposal_calls)
                        )
                        and isinstance(getattr(item, "output", None), str)
                    ]
                    if not results:
                        raise HarnessError("draft_unverified", "No completed resolution MCP result was returned.")
                    for result in results:
                        if (result.get("status") != "pending_approval" or result.get("warning")
                                or result.get("workflow_id") != self.wid):
                            raise HarnessError("draft_unverified", "The resolution was not verifiably saved for this run.")
                    saved = await self.store.approval(self.wid)
                    if not saved or any(r.get("hitl_id") != saved["id"] for r in results):
                        raise HarnessError("draft_unverified", "The resolution's saved approval record could not be verified.")
                    self.emit(Approval(hitl_id=saved["id"], workflow_id=self.wid, draft=saved["payload"]))
                    text = json.dumps({"status": saved["status"], "hitl_id": saved["id"],
                                       "workflow_id": self.wid, "draft": saved["payload"]})
                if not text.strip():
                    raise HarnessError("empty_specialist", "The specialist returned no usable result.")
                if len(text) > MAX_RESULT_CHARS:
                    raise HarnessError("result_limit", "The specialist result exceeded the harness limit.")
                usage = getattr(response, "usage", None)
                if usage:
                    self.emit(Usage(source=name, model=getattr(response, "model", None),
                                    input_tokens=getattr(usage, "input_tokens", None),
                                    output_tokens=getattr(usage, "output_tokens", None)))
                text = redact(text)
                self.emit(ToolEvent(kind="tool_result", tool=name, call_id=call_id, text=text,
                                    duration_ms=int((time.monotonic() - started) * 1000)))
                return ToolResult(text_result_for_llm=text, result_type="success")
            except Exception as exc:
                self.failed = True
                log_failure(logger, "harness", f"specialist_{name}", exc, self.run_id)
                message = str(exc) if isinstance(exc, HarnessError) else f"{name} failed; inspect the server logs."
                self.emit(ToolEvent(kind="tool_error", tool=name, call_id=call_id, text=message,
                                    duration_ms=int((time.monotonic() - started) * 1000)))
                return ToolResult(text_result_for_llm=message, result_type="failure", error=message)
        return invoke
