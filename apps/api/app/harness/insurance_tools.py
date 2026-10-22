"""Insurance-only SDK tools; canonical records, bounded inference, review-only drafts."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..insurance_domain import (
    ACTION_KIND, ClaimActionDraft, action_payload, lookup_claim_record, lookup_policy_record,
)
from ..orchestrator_client import _extract_output_text, invoke_prompt_agent_once
from ..observability import get_tracer
from .models import MAX_RESULT_CHARS, Approval, HarnessError, SpecialistInput, ToolEvent, Usage, redact
from .tools import gateway_headers, mcp_payload
from .diagnostics import log_failure

NAMES = ("triage", "policy_claim_lookup", "policy_lookup", "claim_action_draft")
logger = logging.getLogger(__name__)


class ClaimLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(pattern=r"^CLM-\d+$")
    policy_id: str | None = Field(default=None, pattern=r"^POL-\d+$")
    policyholder_id: str | None = Field(default=None, pattern=r"^PH-\d+$")


class PolicyLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_id: str = Field(pattern=r"^POL-\d+$")
    policyholder_id: str | None = Field(default=None, pattern=r"^PH-\d+$")
    claim_id: str | None = Field(default=None, pattern=r"^CLM-\d+$")
    loss_date: str | None = None
    state: Literal["WA", "CO"] | None = None


class InsuranceTools:
    def __init__(self, settings, run_id, wid, emit, store, message=None):
        self.settings, self.run_id, self.wid = settings, run_id, wid
        self.emit, self.store = emit, store
        self.calls, self.failed, self.draft_attempted = 0, False, False
        self.claims: dict[str, dict] = {}
        self.citations: dict[str, dict] = {}
        self.requested_ids = set(re.findall(r"\b(?:CLM|POL|PH)-\d+\b", message)) if message is not None else None

    def require_requested(self, identifier):
        if self.requested_ids is not None and identifier not in self.requested_ids:
            raise HarnessError("identifier_required", "Supply an exact claim or policy ID; names are not sufficient.")

    def build(self):
        from copilot.tools import Tool
        schemas = (SpecialistInput, ClaimLookup, PolicyLookup, ClaimActionDraft)
        descriptions = (
            "Classify insurance intent only. This cannot retrieve records or decide coverage.",
            "Read a synthetic claim, its linked policy, loss date/state and versioned wording. Never infer IDs.",
            "Read authoritative synthetic policy wording and exact citations; include claim_id for loss-specific context.",
            "Save exactly one structured insurance claim-action draft for human review only. "
            "First retrieve this claim and its applicable policy wording. No execution or approval is possible.",
        )
        return [Tool(name=name, description=description, handler=self.handler(name),
                     parameters=schema.model_json_schema(), skip_permission=True, defer="never")
                for name, schema, description in zip(NAMES, schemas, descriptions)]

    def ground(self, result):
        self.citations.update({c["policy_ref"]: c for c in result["citations"]})
        if self.requested_ids is not None:
            self.requested_ids.update((result["policy"]["policy_id"], result["policy"]["policyholder_id"]))
        if "claim" in result:
            self.claims[result["claim"]["claim_id"]] = result
        return result

    async def native(self, agent, prompt, correlation, **kwargs):
        name = "triage" if agent.endswith("-triage") else "claim_action_draft"
        with get_tracer("northwind.harness").start_as_current_span(f"harness.specialist.{name}", attributes={
            "session_id": self.run_id, "workflow_id": self.wid, "harness": "copilot-sdk",
            "agent_name": agent, "tool_name": name, "business_domain": "insurance",
        }, record_exception=False, set_status_on_exception=False):
            response = await invoke_prompt_agent_once(agent, prompt, correlation, **kwargs)
        usage = getattr(response, "usage", None)
        if usage:
            self.emit(Usage(source=name, model=getattr(response, "model", None),
                            input_tokens=getattr(usage, "input_tokens", None),
                            output_tokens=getattr(usage, "output_tokens", None)))
        return response

    async def draft(self, args):
        if self.draft_attempted:
            raise HarnessError("draft_already_attempted", "A draft was already attempted; inspect its saved review.")
        draft = ClaimActionDraft.model_validate(args)
        context = self.claims.get(draft.claim_id)
        if not context:
            raise HarnessError("grounding_required", "Retrieve the linked claim and policy before drafting.")
        if not draft.policy_refs or any(ref not in self.citations for ref in draft.policy_refs):
            raise HarnessError("grounding_required", "Drafts require applicable policy citations retrieved in this run.")
        if not self.settings.harness_resolution_mcp:
            raise HarnessError("mcp_unconfigured", "MCP_WORK_ORDER_URL must identify the insurance draft service.")
        self.draft_attempted = True
        await self.store.require_active(self.wid, "insurance")
        payload = action_payload(draft)
        response = await self.native(
            "northwind-insurance-claim-action-draft",
            "Save this exact validated draft using draft_claim_action, without changing its fields:\n"
            + json.dumps(payload),
            f"Application-owned workflow_id: {self.wid}. Use only this workflow. "
            "The draft is human-review-only. Never execute payment, coverage, claim or communication decisions.",
            mcp_tool={
                "type": "mcp", "server_label": "northwind-insurance-claim-action-draft",
                "server_url": self.settings.harness_resolution_mcp, "require_approval": "never",
                "allowed_tools": ["draft_claim_action"],
                "headers": {"x-orders-workflow": self.wid, **gateway_headers(self.settings)},
            }, workflow_id=self.wid,
        )
        calls = {item.call_id for item in response.output
                 if getattr(item, "type", "") == "function_call"
                 and getattr(item, "name", "") == "draft_claim_action"}
        results = [
            mcp_payload(item.output) for item in response.output
            if ((getattr(item, "type", "") == "mcp_call"
                 and getattr(item, "name", "") == "draft_claim_action"
                 and getattr(item, "status", "") == "completed")
                or (getattr(item, "type", "") == "function_call_output"
                    and getattr(item, "call_id", None) in calls))
            and isinstance(getattr(item, "output", None), str)
        ]
        saved = await self.store.approval(self.wid)
        if (not results or not saved or saved.get("workflowId") != self.wid
                or saved.get("kind") != ACTION_KIND or saved.get("status") != "pending"
                or saved.get("execution_status") != "not_executed"
                or saved.get("business_domain") != "insurance"
                or saved.get("synthetic") is not True
                or saved.get("payload") != payload
                or any(r.get("status") != "pending_approval" or r.get("warning")
                       or r.get("workflow_id") != self.wid or r.get("hitl_id") != saved["id"]
                       or r.get("kind") != ACTION_KIND or r.get("execution_status") != "not_executed"
                       or r.get("synthetic") is not True
                       or r.get("draft") != payload for r in results)):
            raise HarnessError("draft_unverified", "No matching persisted insurance review was confirmed. "
                               "Inspect Approvals before retrying; a pending draft may exist.")
        ClaimActionDraft.model_validate(saved["payload"])
        self.emit(Approval(hitl_id=saved["id"], workflow_id=self.wid, draft=payload, action_kind=ACTION_KIND))
        return {**results[0], "review_scope": "draft_only"}

    def handler(self, name):
        async def invoke(invocation):
            from copilot.tools import ToolResult
            started = time.monotonic()
            try:
                self.calls += 1
                if name not in NAMES:
                    raise HarnessError("tool_denied", "Retail tools are not exposed in insurance mode.")
                if self.calls > self.settings.harness_max_tools:
                    raise HarnessError("tool_limit", "Insurance tool-call limit exceeded.")
                self.emit(ToolEvent(kind="tool_start", tool=name, call_id=invocation.tool_call_id,
                                    text=redact(json.dumps(invocation.arguments))))
                if name == "triage":
                    params = SpecialistInput.model_validate(invocation.arguments)
                    response = await self.native(
                        "northwind-insurance-triage", params.prompt,
                        "Classify intent only. Do not invent identifiers, determine coverage or execute actions.",
                        workflow_id=self.wid,
                    )
                    text = _extract_output_text(response)
                    if not text.strip():
                        raise HarnessError("empty_specialist", "Insurance triage returned no result.")
                elif name == "policy_claim_lookup":
                    params = ClaimLookup.model_validate(invocation.arguments)
                    self.require_requested(params.claim_id)
                    text = json.dumps(self.ground(lookup_claim_record(**params.model_dump())))
                elif name == "policy_lookup":
                    params = PolicyLookup.model_validate(invocation.arguments)
                    self.require_requested(params.claim_id or params.policy_id)
                    if params.claim_id:
                        result = lookup_claim_record(params.claim_id, params.policy_id, params.policyholder_id)
                        claim = result["claim"]
                        # Explicit user constraints must agree even when claim context supplies defaults.
                        lookup_policy_record(params.policy_id, params.policyholder_id,
                                             params.loss_date or claim["loss_date"], params.state or claim["state"])
                        if ((params.loss_date and params.loss_date != claim["loss_date"])
                                or (params.state and params.state != claim["state"])):
                            raise HarnessError("context_mismatch", "Loss context does not match the claim record.")
                    else:
                        result = lookup_policy_record(params.policy_id, params.policyholder_id,
                                                      params.loss_date, params.state)
                    text = json.dumps(self.ground(result))
                else:
                    text = json.dumps(await self.draft(invocation.arguments))
                if len(text) > MAX_RESULT_CHARS:
                    raise HarnessError("result_limit", "Insurance tool result exceeded its budget.")
                text = redact(text)
                self.emit(ToolEvent(kind="tool_result", tool=name, call_id=invocation.tool_call_id,
                                    text=text, duration_ms=int((time.monotonic() - started) * 1000)))
                return ToolResult(text_result_for_llm=text, result_type="success")
            except Exception as exc:
                self.failed = True
                log_failure(logger, "harness", f"insurance_tool_{name}", exc, self.run_id)
                from pydantic import ValidationError
                from ..insurance_domain import InsuranceValidationError
                message = str(exc) if isinstance(exc, (HarnessError, InsuranceValidationError)) else (
                    "Invalid insurance tool input; supply matching canonical identifiers and action fields."
                    if isinstance(exc, ValidationError) else
                    "Insurance tool unavailable. No successful result was confirmed; inspect saved approvals.")
                self.emit(ToolEvent(kind="tool_error", tool=name, call_id=invocation.tool_call_id,
                                    text=message, duration_ms=int((time.monotonic() - started) * 1000)))
                return ToolResult(text_result_for_llm=message, result_type="failure", error=message)
        return invoke

    def final_text(self, text):
        if not self.citations:
            return ("No policy or claim evidence was retrieved in this SDK run. Supply an exact "
                    "claim or policy ID, or ask an authorised human adjuster. No decision or action was executed.")
        references = set(re.findall(r"NW-AUTO-[A-Z]{2}-\d{4}-v\d+#[a-z-]+", text))
        if references - self.citations.keys():
            raise HarnessError("ungrounded_citation", "The SDK answer cited wording not retrieved in this run.")
        sources = "\n".join(
            f"- {c['policy_ref']} — {c['title']}, version {c['version']}, {c['state']}; {c['source_uri']}"
            for c in self.citations.values()
        )
        return (text + "\n\nRetrieved synthetic sources (not a coverage determination):\n" + sources
                + "\nHuman review only. No claim decision, communication, settlement or payment was executed.")
