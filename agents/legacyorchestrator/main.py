# Copyright (c) Microsoft. All rights reserved.
"""Northwind Order Concierge orchestrator — Foundry hosted agent.

This is the "manager" of the Northwind Order Concierge multi-agent flow.
It's hosted in Foundry Agent Service (visible in the Foundry Agents blade,
versioned by azd) and coordinates the four Order Concierge prompt agents:

  - northwind-triage
  - northwind-order-lookup
  - northwind-policy-lookup
  - northwind-resolution-draft

Each of those is a first-class Foundry prompt agent created by
`scripts/create-prompt-agents.py` at post-provision time. Their ids are
resolved at runtime from the FOUNDRY_AGENT_*_ID env vars (populated from Key
Vault by the FastAPI backend that reads and forwards them, or set directly
via `azd env set`).

NOTE ON LEGACY NAMES: The env vars are still called
FOUNDRY_AGENT_TECH_LOOKUP_ID and FOUNDRY_AGENT_WORK_ORDER_DRAFT_ID (and the
Container Apps are still ca-mcp-tech-lookup / ca-mcp-work-order) because
those identifiers are wired through Bicep + azd env and renaming them
would risk breaking `azd up`. Semantically they now serve the order-lookup
and resolution-draft agents.

Prompt-agent tool calls (lookup_order, propose_resolution,
knowledge_base_retrieve) are all served by MCP endpoints and executed
inside Foundry — this process no longer client-side-dispatches
`requires_action` for any prompt agent. The `@tool` functions defined
below are only for the manager agent's own direct use (e.g., the fast
knowledge_retrieve RAG shortcut).
"""
from __future__ import annotations

import json
import os
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework.openai import OpenAIChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.projects.aio import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.identity.aio import get_bearer_token_provider
from dotenv import load_dotenv
from pydantic import Field

load_dotenv(override=False)


# ── Env helpers ────────────────────────────────────────────────────────────
def _env(name: str, *, required: bool = True, default: str = "") -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


PROJECT_ENDPOINT = _env("FOUNDRY_PROJECT_ENDPOINT")
MANAGER_MODEL = _env("AZURE_AI_MODEL_DEPLOYMENT_NAME")
MODEL_GATEWAY_ENDPOINT = _env("ORDERS_MODEL_GATEWAY_ENDPOINT", required=False)
MODEL_GATEWAY_MODEL = _env("ORDERS_MODEL_GATEWAY_MODEL", required=False)
if MODEL_GATEWAY_ENDPOINT:
    parsed_gateway = urlsplit(MODEL_GATEWAY_ENDPOINT)
    if (parsed_gateway.scheme != "https" or not parsed_gateway.hostname
            or parsed_gateway.username or parsed_gateway.password or parsed_gateway.fragment
            or not parsed_gateway.path.rstrip("/").endswith("/openai/v1")):
        raise RuntimeError("ORDERS_MODEL_GATEWAY_ENDPOINT must be a credential-free HTTPS OpenAI v1 endpoint")
    if not MODEL_GATEWAY_MODEL or _env("ORDERS_MODEL_GATEWAY_DEPLOYMENT") != MANAGER_MODEL:
        raise RuntimeError("Gateway alias must bind the selected manager model deployment")
elif MODEL_GATEWAY_MODEL:
    raise RuntimeError("ORDERS_MODEL_GATEWAY_ENDPOINT is required for the gateway alias")

# In Foundry, an agent's id equals its display name. Bicep still exports the
# legacy FOUNDRY_AGENT_*_ID outputs as empty strings for backwards compat, so
# when they're not populated we fall back to the well-known agent names that
# scripts/create-prompt-agents.py creates at post-provision. Without this
# fallback the four wrapper tools never get registered on the manager and
# the LLM correctly reports it has no `order_lookup` / `policy_lookup` /
# etc. tools available.
TRIAGE_ID = _env("FOUNDRY_AGENT_TRIAGE_ID", required=False) or "northwind-triage"
# Legacy env var name; agent is northwind-order-lookup.
ORDER_LOOKUP_ID = _env("FOUNDRY_AGENT_TECH_LOOKUP_ID", required=False) or "northwind-order-lookup"
# Legacy env var name; agent is northwind-policy-lookup.
POLICY_LOOKUP_ID = _env("FOUNDRY_AGENT_MANUAL_RETRIEVAL_ID", required=False) or "northwind-policy-lookup"
# Legacy env var name; agent is northwind-resolution-draft.
RESOLUTION_DRAFT_ID = _env("FOUNDRY_AGENT_WORK_ORDER_DRAFT_ID", required=False) or "northwind-resolution-draft"

SEARCH_ENDPOINT = _env("AZURE_SEARCH_ENDPOINT", required=False)
SEARCH_INDEX = _env("AZURE_SEARCH_INDEX", required=False, default="northwind-knowledge")
SEARCH_KB = _env("AZURE_SEARCH_KNOWLEDGE_BASE", required=False, default="northwind-kb")
SEARCH_KS = _env("AZURE_SEARCH_KNOWLEDGE_SOURCE", required=False, default="northwind-knowledge-ks")
SEARCH_API_VERSION = "2026-05-01-preview"

_credential = DefaultAzureCredential()
_search_credential = AsyncDefaultAzureCredential()


# ── Local function tools ───────────────────────────────────────────────────
# knowledge_retrieve wraps the Foundry IQ knowledge base (Azure AI Search
# agentic retrieval). The KB does its own query planning + answer synthesis
# against the northwind-knowledge index using gpt-4o and returns a
# synthesized answer with references. This is exposed both:
#   - directly to the manager as a fast RAG shortcut for factual questions
#   - as the backing tool for the northwind-policy-lookup prompt agent
#     (invoked via _invoke_prompt_agent with the tool wired here).

# Static order/customer fixture — same shape and data as the mcp-tech-lookup
# fixture, kept here so this service has no external dependency for the
# demo. Swap for a Cosmos read when we're ready.
_ORDERS: list[dict[str, Any]] = [
    {
        "order_id": "NW-40021",
        "customer": {"id": "C-8801", "name": "Priya Patel", "loyalty_tier": "gold", "email": "priya@example.com"},
        "status": "in_transit",
        "placed_on": "2026-09-02",
        "eta": "2026-09-10",
        "total": 189.00,
        "items": [{"sku": "SKU-2201", "name": "Northwind Cascade Espresso Machine", "qty": 1, "price": 189.00}],
        "shipment": {"carrier": "NW-Freight", "tracking": "NWF9922188", "last_scan": "Chicago DC", "delivered": False},
    },
    {
        "order_id": "NW-40022",
        "customer": {"id": "C-9102", "name": "Marcus Chen", "loyalty_tier": "silver", "email": "marcus@example.com"},
        "status": "delivered",
        "placed_on": "2026-08-15",
        "eta": "2026-08-20",
        "total": 74.50,
        "items": [{"sku": "SKU-1108", "name": "Northwind Cast-Iron Skillet", "qty": 1, "price": 74.50}],
        "shipment": {"carrier": "NW-Freight", "tracking": "NWF9901133", "last_scan": "Seattle, WA", "delivered": True},
    },
]


# Plain implementations — used by the manager's own @tool wrappers below.
# Prompt agents now call their tools via MCP endpoints (ca-mcp-tech-lookup,
# ca-mcp-work-order, Search KB MCP) so no shared dispatcher is needed here.

def _lookup_order(query: str) -> str:
    q = (query or "").strip().lower()
    hits = [
        o for o in _ORDERS
        if q and (
            q in o["order_id"].lower()
            or q in o["customer"]["name"].lower()
            or q in o["customer"]["email"].lower()
            or q in o["customer"]["id"].lower()
        )
    ]
    return json.dumps(hits[:5])


def _propose_resolution(workflow_id: str, draft: dict) -> str:
    hitl_id = f"hitl-{workflow_id}"
    return json.dumps({"status": "pending_approval", "hitl_id": hitl_id, "draft": draft})


async def _knowledge_retrieve(query: str) -> str:
    """Query the Foundry IQ knowledge base (agentic retrieval). Returns a
    JSON payload with a synthesized `answer` string and a `references` list
    (title, page, source_uri) drawn from the northwind-knowledge index."""
    if not SEARCH_ENDPOINT:
        return json.dumps({"answer": "Knowledge base is not configured.", "references": []})

    token = (await _search_credential.get_token("https://search.azure.com/.default")).token
    url = (
        f"{SEARCH_ENDPOINT.rstrip('/')}/knowledgebases/{SEARCH_KB}:retrieve"
        f"?api-version={SEARCH_API_VERSION}"
    )
    body = {
        "messages": [{
            "role": "user",
            "content": [{"type": "text", "text": query}],
        }],
        "knowledgeSourceParams": [{
            "knowledgeSourceName": SEARCH_KS,
            "kind": "searchIndex",
        }],
    }
    async with httpx.AsyncClient(timeout=60.0) as http:
        r = await http.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
        if r.status_code >= 400:
            return json.dumps({
                "answer": f"knowledge_retrieve failed: {r.status_code}",
                "error": r.text[:500],
                "references": [],
            })
        data = r.json()

    # Flatten the retrieve response into a stable shape for the caller.
    answer_parts: list[str] = []
    for msg in data.get("response", []) or []:
        for c in msg.get("content", []) or []:
            if c.get("type") == "text" and c.get("text"):
                answer_parts.append(c["text"])
    refs: list[dict[str, Any]] = []
    for ref in data.get("references", []) or []:
        src = ref.get("sourceData") or {}
        refs.append({
            "title": src.get("title"),
            "page": src.get("page"),
            "source_uri": src.get("source_uri"),
            "doc_type": src.get("doc_type"),
            "ref_id": ref.get("id") or ref.get("refId"),
        })
    return json.dumps({"answer": "\n".join(answer_parts).strip(), "references": refs})


@tool(approval_mode="never_require")
def lookup_order(
    query: Annotated[str, Field(description="Order id, customer name/email/id, or free-text search.")],
) -> str:
    """Return Northwind orders + customer + shipment info matching a query."""
    return _lookup_order(query)


@tool(approval_mode="always_require")
def propose_resolution(
    workflow_id: Annotated[str, Field(description="Correlating workflow id.")],
    draft: Annotated[dict, Field(description="Structured resolution fields.")],
) -> str:
    """Enqueue a resolution draft for human approval. Returns { status, hitl_id, draft }."""
    return _propose_resolution(workflow_id, draft)


@tool(approval_mode="never_require")
async def knowledge_retrieve(
    query: Annotated[str, Field(description="Natural-language question for the Northwind policy knowledge base.")],
) -> str:
    """Query the Foundry IQ knowledge base (agentic retrieval). Returns a
    synthesized answer plus references (title, page, source_uri)."""
    return await _knowledge_retrieve(query)


# ── Prompt-agent invocation helper ─────────────────────────────────────────
async def _invoke_prompt_agent(agent_name: str, user_message: str) -> str:
    """Run a single-turn conversation against a Foundry prompt agent and
    return the assistant's concatenated text. Tool calls (MCP) are handled
    entirely inside Foundry — this caller only pulls out the final text."""
    credential = AsyncDefaultAzureCredential()
    try:
        async with AIProjectClient(
            endpoint=PROJECT_ENDPOINT,
            credential=credential,
            allow_preview=True,
        ) as project:
            openai_client = project.get_openai_client(agent_name=agent_name)
            response = await openai_client.responses.create(
                input=user_message,
                max_output_tokens=2048,
                extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
            )
    finally:
        await credential.close()

    if agent_name == RESOLUTION_DRAFT_ID:
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) != "mcp_call":
                continue
            output = getattr(item, "output", None)
            if getattr(item, "status", None) != "completed" or not isinstance(output, str):
                continue
            first_line = output.splitlines()[0]
            try:
                result = json.loads(first_line)
            except json.JSONDecodeError:
                continue
            if result.get("status") == "pending_approval":
                return (
                    "Resolution draft is awaiting human approval. "
                    f"workflow_id={result.get('workflow_id')} "
                    f"hitl_id={result.get('hitl_id')}"
                )
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
    return "".join(parts).strip()


def _make_prompt_tool(agent_name: str, fn_name: str, description: str):
    """Wrap a Foundry prompt agent as a tool the manager can call."""
    if not agent_name:
        return None

    @tool(approval_mode="never_require", name=fn_name, description=description)
    async def _fn(
        prompt: Annotated[str, Field(description="Input for the specialist agent.")],
    ) -> str:
        try:
            return await _invoke_prompt_agent(agent_name, prompt)
        except Exception as exc:  # noqa: BLE001 - return the specialist error to the manager.
            return json.dumps({
                "status": "error",
                "specialist": agent_name,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })

    return _fn


# ── Build the manager agent ────────────────────────────────────────────────
MANAGER_INSTRUCTIONS = """\
You are the Northwind Order Concierge manager. Coordinate four specialist
Foundry prompt agents by calling them as tools:

  - triage(prompt) — extracts structured intent (JSON: order_id,
    customer_hint, intent, sentiment, urgency, problem_summary) from
    customer requests. Call first for any new customer contact that isn't
    a simple factual question.
  - policy_lookup(prompt) — grounded answers with citations from the
    Northwind knowledge index (return policy, shipping SLA, product
    catalog, warranty terms, prior resolution precedent). Use for any
    policy or spec question.
  - order_lookup(prompt) — pulls customer + order + shipment info for a
    given order id / customer identifier / free-text query. Use only
    after triage identifies the order or customer in scope.
  - resolution_draft(prompt) — drafts a structured customer resolution
    (refund / reship / RMA / store credit / address change / cancel) and
    pauses for human approval. Only call after triage has an intent AND
    (typically) after order_lookup has confirmed the order exists.

Requests from the Northwind API begin with `[workflow_id: wf-...]`. Preserve
that value exactly. When calling resolution_draft, explicitly include the
exact workflow_id and instruct the specialist to pass it unchanged to
propose_resolution. Never derive a workflow id from an order or customer id.
Treat a resolution_draft response that says the draft is pending or awaiting
approval as success. Tell the user it is awaiting human approval; do not
describe the drafting step or tool call as failed in that case.

For short, factual questions ("what's Northwind's return window on
electronics?"), skip triage and go straight to policy_lookup — this
covers the Q&A use case.

Never fabricate an order id, a customer id, or a policy citation. Finish
as soon as the user's request is complete.
"""


def _build_agent() -> Agent:
    if MODEL_GATEWAY_ENDPOINT:
        client = OpenAIChatClient(
            base_url=MODEL_GATEWAY_ENDPOINT.rstrip("/") + "/",
            model=MODEL_GATEWAY_MODEL,
            api_key=get_bearer_token_provider(_search_credential, "https://azure-api.net/aigateway/.default"),
        )
    else:
        client = FoundryChatClient(
            project_endpoint=PROJECT_ENDPOINT,
            model=MANAGER_MODEL,
            credential=_credential,
        )

    tools: list[Any] = [knowledge_retrieve]
    for agent_id, name, desc in (
        (TRIAGE_ID,            "triage",            "Extract structured intent (JSON) from a customer request."),
        (ORDER_LOOKUP_ID,      "order_lookup",      "Look up a Northwind customer + order + shipment by id or free-text."),
        (POLICY_LOOKUP_ID,     "policy_lookup",     "Grounded knowledge-index answer with citations."),
        (RESOLUTION_DRAFT_ID,  "resolution_draft",  "Draft a structured customer resolution (pauses for HITL)."),
    ):
        wrapped = _make_prompt_tool(agent_id, name, desc)
        if wrapped is not None:
            tools.append(wrapped)

    return Agent(
        client=client,
        name="northwind-orchestrator",
        description="Manager of the Northwind Order Concierge multi-agent flow.",
        instructions=MANAGER_INSTRUCTIONS,
        tools=tools,
        default_options={"store": False},
    )


def main() -> None:
    agent = _build_agent()
    server = ResponsesHostServer(agent)
    server.run()


if __name__ == "__main__":
    main()
