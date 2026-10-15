"""Thin client to the Northwind orchestrator — a Foundry hosted agent.

The heavy multi-agent lifting (manager + triage/order_lookup/policy_lookup/
resolution_draft prompt agents) is done inside the hosted agent itself
(`agents/orchestrator/main.py`). This module only:

  1. Resolves the orchestrator agent name (env var, defaulting to the name).
  2. Invokes hosted agents through their dedicated OpenAI Responses endpoints.
     Prompt agents use the project endpoint with ``agent_reference``.
     Both chain ``previous_response_id`` per session for multi-turn continuity.
  3. Streams the assistant's response back to the caller as small JSON
     records.

The wire format (`assistant_delta`, `tool_call`, `done`, `error`) is
preserved so the existing web front-ends keep working.

Note: ``azure-ai-projects`` >=2.x no longer exposes a threads/messages/runs
API on ``project.agents``; that surface moved to the ``azure-ai-agents``
package. Hosted Foundry agents are instead invoked through the Responses
API on ``project.get_openai_client()``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.identity.aio import (
    ManagedIdentityCredential as AsyncManagedIdentityCredential,
)

from .observability import get_tracer
from .repositories import append_message, save_workflow
from .settings import get_settings

tracer = get_tracer("northwind.orchestrator_client")


@lru_cache
def _async_credential():
    """Async credential — the sync `DefaultAzureCredential` in azure_clients.py
    is fine for Cosmos/Search/Blob (they support both), but when used with
    `AIProjectClient.aio` its sync token-chain (Azure CLI → VSCode → env → …)
    blocks uvicorn's event loop for seconds on first call. The async variant
    runs the same chain via httpx on the loop instead."""
    s = get_settings()
    if s.azure_client_id:
        return AsyncManagedIdentityCredential(client_id=s.azure_client_id)
    return AsyncDefaultAzureCredential()


_cached_agent_name: str | None = None
_previous_response_by_key: dict[tuple[str, str, str], str] = {}


# ── Sub-agent handoff map ─────────────────────────────────────────────────
# The manager exposes each Foundry prompt agent as a function tool (see
# agents/orchestrator/main.py :: _make_prompt_tool). When we see a tool_call
# for one of these names, stamp the outgoing event with `to_agent` so the
# front-end can render the handoff visually.
_MANAGER_TOOL_TO_AGENT: dict[str, str] = {
    "triage":            "northwind-triage",
    "order_lookup":      "northwind-order-lookup",
    "policy_lookup":     "northwind-policy-lookup",
    "resolution_draft":  "northwind-resolution-draft",
}


def _resolve_to_agent(tool_name: str) -> str | None:
    """Map a manager-side tool name to the Foundry agent name it delegates
    to. Env-var overrides (FOUNDRY_AGENT_*_ID) take precedence so a canary
    rollout that renames an agent still gets the right label.

    NOTE ON LEGACY ENV VARS: the env var names are still
    FOUNDRY_AGENT_TECH_LOOKUP_ID and FOUNDRY_AGENT_WORK_ORDER_DRAFT_ID
    because they're wired through Bicep + azd env. Semantically they now
    point at the order-lookup and resolution-draft agents.
    """
    if get_settings().business_domain == "insurance":
        return {
            "triage": "northwind-insurance-triage",
            "policy_claim_lookup": "northwind-insurance-policy-claim-lookup",
            "policy_lookup": "northwind-insurance-policy-wording",
            "claim_action_draft": "northwind-insurance-claim-action-draft",
        }.get(tool_name)
    default = _MANAGER_TOOL_TO_AGENT.get(tool_name)
    if not default:
        return None
    env_key = {
        "triage":           "FOUNDRY_AGENT_TRIAGE_ID",
        "order_lookup":     "FOUNDRY_AGENT_TECH_LOOKUP_ID",
        "policy_lookup":    "FOUNDRY_AGENT_MANUAL_RETRIEVAL_ID",
        "resolution_draft": "FOUNDRY_AGENT_WORK_ORDER_DRAFT_ID",
    }.get(tool_name)
    if env_key:
        import os
        return os.environ.get(env_key) or default
    return default


def _project_client(project_endpoint: str | None = None) -> AIProjectClient:
    # AIProjectClient stores the credential but doesn't call it until you hit
    # an ARM data-plane surface. We override the OpenAI client's ``api_key`` at
    # ``get_openai_client()`` time when a Foundry key is available (see
    # ``_openai_client_kwargs``), so this credential path only actually fires
    # when we're running under managed identity in Container Apps.
    #
    # Preview enables the SDK's named-agent endpoint, API version and protocol
    # headers. Hosted requests never use a project agent_reference.
    #
    # ``project_endpoint`` lets callers route to a non-flagship Foundry
    # project (per-department prompt agents live in their own projects; see
    # ``company.foundry_project_endpoint_for``). Defaults to the flagship.
    s = get_settings()
    return AIProjectClient(
        endpoint=project_endpoint or s.foundry_project_endpoint,
        credential=_async_credential(),
        allow_preview=True,
    )


def _openai_client_kwargs(
    agent_name: str, *,
    hosted: bool | None = None, identity_only: bool = False,
) -> dict[str, Any]:
    """Extra kwargs for ``project.get_openai_client()``.

    * ``agent_name`` selects the hosted endpoint and lets the SDK supply its
      required API version and preview protocol headers. Prompt calls leave
      it unset and use the project endpoint.
    * ``api_key`` + ``default_headers['api-key']`` override the default
      bearer-token provider when a Foundry key is present. Foundry expects
      the key in the ``api-key`` header (Azure convention); the plain
      ``AsyncOpenAI`` client returned by ``get_openai_client()`` would
      otherwise send it as ``Authorization: Bearer …`` and get 403. We set
      both so local dev (docker-compose / venv) doesn't need managed
      identity or a host az login inside the container.
    """
    s = get_settings()
    kwargs: dict[str, Any] = {}
    is_hosted = s.order_agents_hosted if hosted is None else hosted
    if is_hosted:
        kwargs["agent_name"] = agent_name
    if s.foundry_api_key and not identity_only:
        kwargs["api_key"] = s.foundry_api_key
        kwargs["default_headers"] = {"api-key": s.foundry_api_key}
    return kwargs


async def resolve_orchestrator_agent_id() -> str:
    """The Foundry agent id equals the agent name; the env var override lets
    ops point at a different name (e.g., during a canary rollout)."""
    global _cached_agent_name
    if get_settings().business_domain == "insurance":
        from .insurance import require_insurance_runtime
        require_insurance_runtime()
        return get_settings().insurance_orchestrator_agent_id
    if _cached_agent_name:
        return _cached_agent_name
    s = get_settings()
    _cached_agent_name = s.orchestrator_agent_id or "northwind-orchestrator"
    return _cached_agent_name


def _agent_reference(agent_name: str) -> dict[str, str]:
    return {"name": agent_name, "type": "agent_reference"}


def _extract_output_text(response: Any) -> str:
    """Pull the assistant's plain text out of a Responses API result."""
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text:
        return text
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            t = getattr(content, "text", None)
            if isinstance(t, str) and t:
                parts.append(t)
            elif t is not None:
                val = getattr(t, "value", None)
                if val:
                    parts.append(val)
    return "".join(parts)


async def invoke_prompt_agent_once(agent_name: str, prompt: str, correlation: str,
                                  *, mcp_tool: dict | None = None,
                                  workflow_id: str | None = None) -> Any:
    """Stateless, identity-authenticated specialist call for application tools."""
    if get_settings().business_domain == "insurance":
        from .insurance import require_insurance_runtime
        require_insurance_runtime()
        if not agent_name.startswith("northwind-insurance-"):
            raise ValueError("Retail agents cannot serve insurance inference.")
    async with _project_client() as project:
        client_kwargs = _openai_client_kwargs(agent_name, identity_only=True)
        async with project.get_openai_client(**client_kwargs) as client:
            response = await client.responses.create(
                input=[
                    {"role": "developer", "content": correlation},
                    {"role": "user", "content": prompt},
                ],
                max_output_tokens=2048,
                **({} if "agent_name" in client_kwargs else
                   {"extra_body": {"agent_reference": _agent_reference(agent_name)}}),
                **({"metadata": {"workflow_id": workflow_id}} if workflow_id else {}),
                **({"tools": [mcp_tool]} if mcp_tool is not None
                   and not get_settings().order_agents_hosted else {}),
            )
    if response.status != "completed":
        raise RuntimeError(f"Specialist response did not complete: {response.status}")
    return response


def _try_parse_json(raw: Any) -> Any | None:
    """Best-effort JSON parse. Returns None on any failure so callers can
    fall back to the raw string. Foundry sometimes serializes function-call
    arguments as a JSON-encoded string; we want the parsed dict for the UI."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        import json as _json
        try:
            return _json.loads(raw)
        except (ValueError, TypeError):
            return None
    return None


async def _emit_output_item_added(
    event: Any, agent_name: str, *, done: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Translate a Responses API `output_item.added` / `.done` event into
    the trace-rail wire schema.

    Two item types matter here:

    - ``function_call`` — the manager just decided to invoke a tool.
      Emits ``tool_call`` with ``call_id``, ``tool`` (function name),
      and — when the tool maps to one of the four Northwind prompt
      agents — a ``to_agent`` label so the UI can render the handoff.
      Arguments may already be populated on ``.done`` (short calls) or
      arrive later via ``function_call_arguments.done``; forward
      whatever we have.

    - ``function_call_output`` — the tool returned. Emits ``tool_result``
      with ``call_id`` and the tool's output text (which for the four
      sub-agents is the sibling agent's final reply).

    Any other item kind (message, refusal, reasoning summary) is ignored
    here — text-bearing items are already handled by the
    ``output_text.delta`` branch of the parent loop.
    """
    item = getattr(event, "item", None)
    if item is None:
        return
    itype = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
    if not itype:
        return

    def _attr(name: str) -> Any:
        if isinstance(item, dict):
            return item.get(name)
        return getattr(item, name, None)

    if itype == "function_call":
        tool = _attr("name")
        call_id = _attr("call_id") or _attr("id")
        args_raw = _attr("arguments")
        args_parsed = _try_parse_json(args_raw)
        payload: dict[str, Any] = {
            "kind": "tool_call",
            "call_id": call_id,
            "tool": tool,
            "agent": agent_name,
        }
        to_agent = _resolve_to_agent(tool) if isinstance(tool, str) else None
        if to_agent:
            payload["to_agent"] = to_agent
        if args_parsed is not None:
            payload["arguments"] = args_parsed
        elif args_raw:
            payload["arguments"] = args_raw
        # Suppress duplicate emissions on `.done` when the same call was
        # already announced on `.added` — the .done pass only exists to
        # catch SDK versions that don't populate .added.
        if done and not args_parsed and not args_raw:
            return
        yield payload
    elif itype == "function_call_output":
        call_id = _attr("call_id") or _attr("id")
        result = _attr("output")
        # Foundry sometimes wraps the output in {type:"text", text:"..."}
        if isinstance(result, list):
            parts: list[str] = []
            for r in result:
                if isinstance(r, dict) and r.get("type") == "text":
                    parts.append(str(r.get("text") or ""))
            result = "\n".join(p for p in parts if p)
        elif isinstance(result, dict) and result.get("type") == "text":
            result = str(result.get("text") or "")
        yield {
            "kind": "tool_result",
            "call_id": call_id,
            "result": result if isinstance(result, str) else (
                None if result is None else str(result)
            ),
        }


async def run_agent_turn(
    session_id: str,
    user_message: str,
    agent_name: str,
    project_endpoint: str | None = None,
    workflow_id: str | None = None,
    *,
    hosted: bool | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one turn against ``agent_name`` (hosted or prompt), streaming events.

    Ephemeral: no Cosmos persistence, but multi-turn continuity is preserved
    per (agent, session) via the ``previous_response_id`` chain.

    ``project_endpoint`` targets a specific Foundry project when the agent
    lives outside the flagship project (per-department prompt agents each
    live in their own project — see ``company.foundry_project_endpoint_for``).
    Defaults to the flagship project.
    ``hosted=False`` keeps department prompt agents on the project protocol,
    even when the flagship fleet uses ``ORDER_AGENTS_HOSTED=true``.
    """
    if get_settings().business_domain == "insurance":
        from .insurance import require_insurance_runtime
        require_insurance_runtime()
        if not agent_name.startswith("northwind-insurance-"):
            raise ValueError("Retail agents cannot serve insurance inference.")
    with tracer.start_as_current_span("orchestrator_client.run_agent_turn") as span:
        span.set_attribute("session_id", session_id)
        span.set_attribute("agent_name", agent_name)
        if project_endpoint:
            span.set_attribute("project_endpoint", project_endpoint)

        key = (project_endpoint or get_settings().foundry_project_endpoint, agent_name, session_id)
        final_text = ""
        try:
            async with _project_client(project_endpoint) as project:
                client_kwargs = _openai_client_kwargs(
                    agent_name, hosted=hosted,
                )
                openai_client = project.get_openai_client(**client_kwargs)
                kwargs: dict[str, Any] = {
                    "input": user_message,
                    **({} if "agent_name" in client_kwargs else
                       {"extra_body": {"agent_reference": _agent_reference(agent_name)}}),
                    # Ask Foundry to stream Server-Sent Events. The
                    # AsyncOpenAI client returns an async iterator of typed
                    # events; we forward `response.output_text.delta` chunks
                    # to the caller so the UI paints tokens as they arrive
                    # instead of blocking on the full completion.
                    "stream": True,
                }
                if workflow_id:
                    kwargs["metadata"] = {"workflow_id": workflow_id}
                prev = _previous_response_by_key.get(key)
                if prev:
                    kwargs["previous_response_id"] = prev

                response_id: str | None = None
                response_status: str | None = None
                stream = await openai_client.responses.create(**kwargs)
                async for event in stream:
                    etype = getattr(event, "type", "") or ""
                    if etype == "response.output_text.delta":
                        delta = getattr(event, "delta", "") or ""
                        if delta:
                            final_text += delta
                            yield {
                                "kind": "assistant_delta",
                                "agent": agent_name,
                                "text": delta,
                            }
                    elif etype == "response.reasoning_text.delta":
                        # Reasoning-model chain-of-thought. Only surfaced when
                        # the manager deployment is a reasoning model (o-series
                        # or gpt-5.x reasoning variants). Front-end renders
                        # these in a dedicated "Thinking" card so they don't
                        # bleed into the visible assistant answer.
                        delta = getattr(event, "delta", "") or ""
                        if delta:
                            yield {
                                "kind": "reasoning_delta",
                                "agent": agent_name,
                                "text": delta,
                            }
                    elif etype == "response.output_item.added":
                        # New output item — a function call, function call
                        # output, or message. We forward function_call and
                        # function_call_output so the UI can render the
                        # manager -> sub-agent handoff timeline.
                        async for evt in _emit_output_item_added(event, agent_name):
                            yield evt
                    elif etype == "response.output_item.done":
                        # `.done` also carries the final item; some Foundry
                        # SDK versions only populate arguments/output here
                        # (not on .added). Emit again if we haven't yet.
                        async for evt in _emit_output_item_added(event, agent_name, done=True):
                            yield evt
                    elif etype == "response.function_call_arguments.done":
                        # Streaming arg completion (Foundry accumulates JSON
                        # in chunks). We only forward the terminal .done event
                        # with the full parsed args — .delta noise isn't
                        # useful for the trace rail.
                        call_id = getattr(event, "item_id", None) or getattr(event, "call_id", None)
                        args_raw = getattr(event, "arguments", None)
                        args_parsed = _try_parse_json(args_raw) if args_raw else None
                        yield {
                            "kind": "tool_args_done",
                            "call_id": call_id,
                            "arguments": args_parsed if args_parsed is not None else args_raw,
                        }
                    elif etype in ("response.created", "response.in_progress",
                                   "response.completed", "response.failed",
                                   "response.incomplete"):
                        resp = getattr(event, "response", None)
                        if resp is not None:
                            rid = getattr(resp, "id", None)
                            if rid:
                                response_id = rid
                            status = getattr(resp, "status", None)
                            if status:
                                response_status = str(status)
                        if etype in ("response.failed", "response.incomplete"):
                            raise RuntimeError(f"Agent response did not complete: {response_status or etype}")
                    elif etype == "response.error":
                        # Foundry emits this for mid-stream errors; surface
                        # to the caller and stop consuming.
                        err = getattr(event, "error", None) or getattr(event, "message", None)
                        raise RuntimeError(str(err) if err else "response.error")
                    # Fallback: if we didn't get typed deltas but the final
                    # response object is attached (e.g., non-delta-emitting
                    # tools like intent classifier), pull text from it on
                    # completion.
                    if etype == "response.completed" and not final_text:
                        resp = getattr(event, "response", None)
                        if resp is not None:
                            final_text = _extract_output_text(resp).strip()
                            if final_text:
                                yield {
                                    "kind": "assistant_delta",
                                    "agent": agent_name,
                                    "text": final_text,
                                }

                if response_status != "completed" or not final_text.strip():
                    raise RuntimeError(
                        "Agent response ended without a completed assistant answer "
                        f"(status: {response_status or 'unknown'})."
                    )
                if response_id:
                    span.set_attribute("response_id", response_id)
                    _previous_response_by_key[key] = response_id
                if response_status:
                    span.set_attribute("response_status", response_status)
        except Exception as exc:  # noqa: BLE001
            span.record_exception(exc)
            yield {"kind": "error", "message": str(exc)}
            return

        yield {"kind": "done"}


async def run_turn(session_id: str, user_message: str) -> AsyncIterator[dict[str, Any]]:
    """Run one orchestrator turn, streaming events to the caller as JSON records.

    Thin wrapper over :func:`run_agent_turn` that also persists conversation
    messages and workflow state to Cosmos — used by the multi-agent Assistant
    tab where cross-session history matters.
    """
    workflow_id = f"wf-insurance-{session_id}" if get_settings().business_domain == "insurance" else f"wf-{session_id}"
    manager_message = (
        f"[workflow_id: {workflow_id}]\n"
        f"{user_message}\n\n"
        "If you call resolution_draft, require it to pass the workflow_id "
        f"exactly as {workflow_id}; do not derive or invent another value."
    )
    with tracer.start_as_current_span("orchestrator_client.run_turn") as span:
        span.set_attribute("session_id", session_id)
        span.set_attribute("workflow_id", workflow_id)

        agent_name = await resolve_orchestrator_agent_id()
        if not agent_name:
            yield {"kind": "error", "message": (
                "Orchestrator agent has not been deployed yet. "
                "Run `azd deploy orchestrator`."
            )}
            return

        project_name = get_settings().order_project_name
        await append_message(session_id, "user", user_message,
                             meta={"foundry_project": project_name})
        await save_workflow(workflow_id, {"sessionId": session_id, "state": "running",
                                          "foundry_project": project_name})

        final_text = ""
        failed = False
        async for event in run_agent_turn(session_id, manager_message, agent_name,
                                         workflow_id=workflow_id):
            if event.get("kind") == "error":
                failed = True
            if event.get("kind") == "assistant_delta" and event.get("text"):
                final_text += event["text"]
            if event.get("kind") == "done":
                # Attach the workflow id so the front-end can correlate approvals.
                event = {**event, "workflow_id": workflow_id}
            yield event

        if final_text and not failed:
            await append_message(session_id, "assistant", final_text,
                                 meta={"foundry_project": project_name})
        await save_workflow(workflow_id, {"sessionId": session_id, "state": "failed" if failed else "idle",
                                          "foundry_project": project_name})


async def ask_single_turn(question: str) -> AsyncIterator[str]:
    """Backwards-compatible single-turn text stream against the policy-lookup
    prompt agent. Kept for the legacy ``/qa`` route; new callers should use
    :func:`run_agent_turn` directly.
    """
    s = get_settings()
    agent_name = ("northwind-insurance-policy-wording" if s.business_domain == "insurance"
                  else s.manual_agent_id or "northwind-policy-lookup")
    # Use a stable session id so repeated /qa calls share a lightweight thread.
    async for event in run_agent_turn("qa-legacy", question, agent_name):
        if event.get("kind") == "assistant_delta" and event.get("text"):
            yield event["text"]
