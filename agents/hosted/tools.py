"""Bound proposal MCP and independently hosted Responses delegation."""
from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import httpx
from agent_framework import AgentResponseUpdate, Content, tool
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field

from .config import INSURANCE_SPECIALISTS, SPECIALISTS, Settings
from .scope import RequestScope, current_scope
from ..insurance.domain import ClaimActionDraft, action_payload


class ResolutionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str | None = Field(pattern=r"^NW-\d+$")
    customer_id: str | None = Field(max_length=128)
    resolution_type: Literal["refund_full", "refund_partial", "reship", "rma",
                             "store_credit", "address_change", "cancel", "goodwill_credit"]
    amount_usd: float = Field(ge=0, le=100000, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=2000)
    policy_ref: str | None = Field(max_length=256)
    urgency: Literal["P1", "P2", "P3", "P4"]


def payload(value: Any) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and set(value) == {"result"} and isinstance(value["result"], str):
        value = json.loads(value["result"])
    if isinstance(value, dict) and "content" in value and "status" not in value:
        texts = [c["text"] for c in value["content"] if c.get("type") == "text"]
        if len(texts) != 1:
            raise ValueError("Expected one structured MCP result")
        value = json.loads(texts[0])
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object from the MCP service")
    return value


def pending_approval(value: Any, workflow_id: str) -> dict:
    result = payload(value)
    if (result.get("status") != "pending_approval" or result.get("workflow_id") != workflow_id
            or not isinstance(result.get("hitl_id"), str) or not result["hitl_id"]
            or result.get("warning") or result.get("error") or not isinstance(result.get("draft"), dict)):
        raise RuntimeError("Resolution service did not confirm a persisted, workflow-bound pending approval")
    return result


async def call_proposal_mcp(url: str, workflow_id: str, draft: dict,
                            tool_name: str = "propose_resolution") -> dict:
    headers = {"x-orders-workflow": workflow_id}
    if workflow_id.startswith("wf-harness-"):
        headers["x-harness-workflow"] = workflow_id
    async with httpx.AsyncClient(headers=headers, timeout=60, follow_redirects=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    tool_name, {"workflow_id": workflow_id, "draft": draft},
                )
                if result.isError:
                    raise RuntimeError("Resolution MCP call failed; no approval is confirmed")
                data = result.structuredContent
                if data is None:
                    texts = [c.text for c in result.content if c.type == "text"]
                    if len(texts) != 1:
                        raise ValueError("Resolution MCP did not return one structured result")
                    data = json.loads(texts[0])
                return pending_approval(data, workflow_id)


def proposal_tool(settings: Settings):
    spec = settings.mcp_spec()

    if settings.business_domain == "insurance":
        @tool(name="draft_claim_action", approval_mode="never_require",
              description="Persist a synthetic claim-action draft for HUMAN review only; NEVER execute coverage/payment.")
        async def draft_claim_action(draft: ClaimActionDraft) -> str:
            scope = current_scope.get()
            workflow = scope.require_workflow()
            proposed = action_payload(ClaimActionDraft.model_validate(draft))
            if scope.approval is not None:
                if scope.approval["draft"] != proposed:
                    raise RuntimeError("Only one distinct pending draft is permitted per response")
                return json.dumps(scope.approval)
            result = await call_proposal_mcp(spec["server_url"], workflow, proposed, "draft_claim_action")
            if (result.get("draft") != proposed or result.get("kind") != "insurance_claim_action"
                    or result.get("execution_status") != "not_executed" or result.get("synthetic") is not True):
                raise RuntimeError("Insurance MCP did not confirm a non-executed synthetic draft")
            scope.approval = pending_approval(result, workflow)
            scope.tool_results += 1
            return json.dumps(scope.approval)
        return draft_claim_action

    @tool(name="propose_resolution", approval_mode="never_require",
          description="Persist a draft for HUMAN approval. Workflow identity is bound by the application, not arguments.")
    async def propose_resolution(draft: ResolutionDraft) -> str:
        scope = current_scope.get()
        try:
            workflow = scope.require_workflow()
            proposed = ResolutionDraft.model_validate(draft).model_dump()
            if scope.approval is not None:
                if scope.approval["draft"] != proposed:
                    raise RuntimeError("Only one distinct pending draft is permitted per response")
                return json.dumps(scope.approval)
            result = await call_proposal_mcp(spec["server_url"], workflow, proposed)
            if result.get("draft") != proposed:
                raise RuntimeError("Resolution MCP persisted a different draft")
            scope.approval = pending_approval(result, workflow)
            scope.tool_results += 1
            return json.dumps(scope.approval)
        except Exception:
            scope.failures.append("Resolution persistence failed")
            raise

    return propose_resolution


def _obj(value: Any) -> dict:
    return value if isinstance(value, dict) else value.model_dump()


def child_tool_updates(item: dict, agent_name: str, namespace: str, *, done: bool) -> list[AgentResponseUpdate]:
    """Only forward tool events; specialist prose is returned to the manager."""
    kind = item.get("type")
    call = f"{namespace}_{item.get('call_id') or item.get('id', '')}"
    props = {"specialist": agent_name}
    contents = []
    if kind == "function_call":
        contents = [Content.from_function_call(call, item["name"], arguments=item.get("arguments", ""),
                                               informational_only=True)]
    elif kind == "function_call_output" and done:
        contents = [Content.from_function_result(call, result=item.get("output", ""))]
    elif kind == "mcp_call":
        contents = [Content.from_mcp_server_tool_call(
            call_id=call, tool_name=item.get("name"), server_name=item.get("server_label"),
            arguments=item.get("arguments", ""))]
        if done:
            if item.get("error") or item.get("status") == "failed":
                raise RuntimeError(f"{agent_name} MCP call failed")
            contents.append(Content.from_mcp_server_tool_result(call_id=call, output=item.get("output")))
    return [AgentResponseUpdate(contents=contents, author_name=agent_name, additional_properties=props)] if contents else []


async def invoke_hosted_specialist(project, agent_name: str, prompt: str, scope: RequestScope,
                                  *, resolution: bool = False) -> str:
    metadata = {"workflow_id": scope.workflow_id} if scope.workflow_id else {}
    if resolution:
        scope.require_workflow()
    namespace = uuid.uuid4().hex[:12]
    completed = None
    seen_calls: set[str] = set()
    # Do not transparently replay a hosted turn that may already have saved a draft.
    async with project.get_openai_client(agent_name=agent_name, max_retries=0) as client:
        stream = await client.responses.create(
            input=prompt, stream=True, max_output_tokens=4096, metadata=metadata,
        )
        async with stream:
            async for event in stream:
                event = _obj(event)
                kind = event["type"]
                if kind in ("response.failed", "response.incomplete", "error"):
                    raise RuntimeError(f"Hosted specialist {agent_name} did not complete")
                if kind in ("response.output_item.added", "response.output_item.done"):
                    item = _obj(event["item"])
                    done = kind.endswith(".done")
                    # Emit complete arguments once, not an empty added + duplicate done call.
                    if not done:
                        continue
                    key = str(item.get("id") or item.get("call_id"))
                    if key in seen_calls:
                        continue
                    seen_calls.add(key)
                    for update in child_tool_updates(item, agent_name, namespace, done=True):
                        if scope.relay_enabled:
                            scope.events.put_nowait(update)
                if kind == "response.completed":
                    completed = _obj(event["response"])
    if completed is None or completed.get("status") != "completed":
        raise RuntimeError(f"Hosted specialist {agent_name} stream ended without completion")
    output = completed.get("output", [])
    for item in output:
        key = str(item.get("id") or item.get("call_id"))
        if key not in seen_calls:
            for update in child_tool_updates(item, agent_name, namespace, done=True):
                if scope.relay_enabled:
                    scope.events.put_nowait(update)
    if resolution:
        results = [item.get("output") for item in output
                   if item.get("type") in ("function_call_output", "mcp_call") and item.get("output")]
        approvals = []
        for result in results:
            try:
                approvals.append(pending_approval(result, scope.require_workflow()))
            except (ValueError, RuntimeError, TypeError):
                continue
        if not approvals:
            raise RuntimeError("Resolution specialist returned no persisted pending approval evidence")
        scope.approval = approvals[-1]
        return json.dumps(scope.approval)
    text = "".join(c.get("text", "") for item in output if item.get("type") == "message"
                   for c in item.get("content", []) if c.get("type") == "output_text")
    if not text:
        raise RuntimeError(f"Hosted specialist {agent_name} returned no answer")
    return text


def specialist_tools(settings: Settings, project) -> list:
    def wrap(fn_name: str, default_name: str, env_name: str):
        agent_name = settings.env.get(env_name) or default_name
        if settings.business_domain == "insurance" and not agent_name.startswith("northwind-insurance-"):
            raise ValueError("Insurance specialists cannot delegate to retail agents.")

        @tool(name=fn_name, approval_mode="never_require",
              description=f"Delegate to the independently hosted {agent_name} specialist.")
        async def delegate(prompt: str) -> str:
            scope = current_scope.get()
            try:
                return await invoke_hosted_specialist(
                    project, agent_name, prompt, scope,
                    resolution=fn_name in {"resolution_draft", "claim_action_draft"},
                )
            except Exception:
                scope.failures.append(f"Hosted specialist {agent_name} failed")
                raise

        return delegate

    specialists = INSURANCE_SPECIALISTS if settings.business_domain == "insurance" else SPECIALISTS
    return [wrap(fn_name, *target) for fn_name, target in specialists.items()]
