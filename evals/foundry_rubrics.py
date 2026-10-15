"""Rubric prompt texts for all 5 Northwind Order Concierge agents.

Each `build_*_prompt_text()` returns a Foundry-compatible prompt-based
evaluator prompt that:
  - Uses {{query}} and {{response}} template variables
  - Returns {"result": <int 1-5>, "reason": "<str>"}
  - Has an anchored 1-5 rubric tied to the agent's actual job
"""
from __future__ import annotations


_OUTPUT_CONTRACT = (
    "\nReturn ONLY a JSON object:\n"
    "  {\"result\": <int 1-5>, \"reason\": \"<one-sentence justification>\"}\n\n"
    "CUSTOMER / UPSTREAM REQUEST:\n{{query}}\n\n"
    "AGENT RESPONSE:\n{{response}}\n"
)


def build_triage_rubric() -> str:
    return (
        "You are a strict evaluator for the Northwind Order Concierge triage agent.\n"
        "The agent must return ONLY a JSON object with:\n"
        "  order_id (string|null), customer_hint (string|null),\n"
        "  intent (ORDER_STATUS | RETURN | CANCEL | MODIFY | COMPLAINT |\n"
        "          REFUND_REQUEST | OTHER),\n"
        "  sentiment (calm | frustrated | angry),\n"
        "  urgency (P1 | P2 | P3 | P4),\n"
        "  problem_summary (one sentence).\n\n"
        "Urgency calibration:\n"
        "  P1 = safety / fraud / legal risk (account takeover, damaged perishable)\n"
        "  P2 = time-sensitive delivery (wedding, event) or angry escalation\n"
        "  P3 = normal inquiry\n"
        "  P4 = FYI / low-touch\n\n"
        "Score 1-5:\n"
        "  5 - Perfectly valid JSON; every extractable field correct; intent + urgency\n"
        "      correct; no hallucinations.\n"
        "  4 - Valid JSON; correct intent; minor extraction miss OR slightly verbose summary.\n"
        "  3 - Valid JSON but intent adjacent (e.g., RETURN vs REFUND_REQUEST), or an\n"
        "      extractable field null, or urgency off by one band.\n"
        "  2 - Invalid JSON with right ideas, urgency off by 2+ bands, or fabricated order id.\n"
        "  1 - Not JSON at all, refuses task, or prose only.\n"
        + _OUTPUT_CONTRACT
    )


def build_tech_lookup_rubric() -> str:
    # Legacy key name; the agent is northwind-order-lookup.
    return (
        "You are a strict evaluator for the Northwind order-lookup agent.\n"
        "The agent takes an order id, a customer identifier (name / email / phone /\n"
        "customer id), or free-text, calls lookup_order(query), and returns a compact\n"
        "JSON summary with { customer, order, shipment }. If the tool returns no\n"
        "match it must say so plainly rather than invent an order.\n\n"
        "Score 1-5:\n"
        "  5 - Returns a real matched customer + order + shipment record (or clearly\n"
        "      says 'no match') that honors the query.\n"
        "  4 - Returns a match but a field is stripped (e.g., shipment missing) or\n"
        "      slightly generic.\n"
        "  3 - Returns partial data, or the record doesn't match the query intent\n"
        "      (e.g., wrong customer for a name search).\n"
        "  2 - Response ignores the query, fabricates an order id / customer as if a\n"
        "      tool ran, or dumps raw JSON without structure.\n"
        "  1 - Refuses, or returns unrelated content.\n"
        "  Note: acknowledging 'no matches found' for a truly unknown id is a 5.\n"
        + _OUTPUT_CONTRACT
    )


def build_manual_retrieval_rubric() -> str:
    # Legacy key name; the agent is northwind-policy-lookup.
    return (
        "You are a strict evaluator for the Northwind policy-lookup agent.\n"
        "The agent is grounded in a Foundry IQ knowledge base of return policy,\n"
        "shipping SLA, warranty terms, product catalog, and prior resolved-case\n"
        "precedents. It must:\n"
        "  1. Answer from retrieved content only.\n"
        "  2. Preserve inline citations in the form [title, p.N](source_uri).\n"
        "  3. Say \"I do not know\" if no references support the claim.\n"
        "  4. Never invent facts or citations.\n\n"
        "Score 1-5:\n"
        "  5 - Directly answers the question; at least one preserved citation with\n"
        "      title + page + source_uri; no unsupported claims.\n"
        "  4 - Correct answer with citations, but citations partially stripped\n"
        "      (e.g., title only, no page) OR minor tangential prose added.\n"
        "  3 - Answer is on-topic but citations missing, OR answer is padded with\n"
        "      unsourced general knowledge.\n"
        "  2 - Answer contains claims not clearly grounded in retrievable content,\n"
        "      OR invents a citation URI/title.\n"
        "  1 - Refuses without justification, or completely off-topic, or entirely\n"
        "      ungrounded hallucination.\n"
        "  Note: 'I do not know' with no citations is a 5 when the question truly\n"
        "  has no support in Northwind policy / catalog / precedent.\n"
        + _OUTPUT_CONTRACT
    )


def build_work_order_draft_rubric() -> str:
    # Legacy key name; the agent is northwind-resolution-draft.
    return (
        "You are a strict evaluator for the Northwind resolution-drafting agent.\n"
        "The agent produces a structured draft with these fields:\n"
        "  order_id, customer_id,\n"
        "  resolution_type (refund_full | refund_partial | reship | rma |\n"
        "                   store_credit | address_change | cancel | goodwill_credit),\n"
        "  amount_usd (number, 0 if not monetary),\n"
        "  reason (one sentence citing policy or precedent),\n"
        "  policy_ref (string|null),\n"
        "  urgency (P1 | P2 | P3 | P4).\n"
        "It must end with a one-sentence confirmation that the draft is awaiting\n"
        "human approval and must NOT commit changes.\n\n"
        "Score 1-5:\n"
        "  5 - All 7 fields present with plausible values (resolution_type matches\n"
        "      the customer's ask, amount_usd within policy thresholds, reason cites\n"
        "      a real policy_ref); confirmation sentence present; awaits approval.\n"
        "  4 - All 7 fields present but one value is slightly off (e.g., amount a\n"
        "      touch high) OR the confirmation sentence is missing.\n"
        "  3 - Missing 1 required field, OR amount_usd implausible for the case,\n"
        "      OR resolution_type inconsistent with the customer's intent.\n"
        "  2 - Missing 2+ fields, OR fabricates a policy_ref that doesn't exist,\n"
        "      OR the agent claims the resolution was applied without approval.\n"
        "  1 - Not a structured draft, refuses, or unrelated content.\n"
        + _OUTPUT_CONTRACT
    )


def build_orchestrator_rubric() -> str:
    return (
        "You are a strict evaluator for the Northwind hosted orchestrator agent.\n"
        "The orchestrator coordinates 4 sub-agents (triage, order-lookup,\n"
        "policy-lookup, resolution-draft) to fulfill Northwind Order Concierge\n"
        "customer requests. It should:\n"
        "  1. Extract intent (implicitly via the triage sub-agent).\n"
        "  2. Fetch grounded policy content when the customer asks a return /\n"
        "     shipping / warranty question.\n"
        "  3. Look up an order only when the customer identifies one.\n"
        "  4. Draft a resolution only when the customer's ask implies action\n"
        "     (refund / reship / cancel / credit).\n"
        "  5. Not ask the customer for information already stated in their message.\n"
        "  6. Produce a coherent final response that ties the sub-steps together.\n\n"
        "Score 1-5:\n"
        "  5 - Response chains the right sub-steps for the request, reflects\n"
        "      information from the customer message (no re-asking), and ends\n"
        "      with a clear next action.\n"
        "  4 - Right sub-steps but one minor redundancy (e.g., re-asks a detail) or\n"
        "      a slightly generic wrap-up.\n"
        "  3 - Missing one appropriate sub-step (e.g., skips policy grounding for a\n"
        "      policy question) OR asks the customer for information already given.\n"
        "  2 - Skips triage entirely, drafts a resolution without confirming the\n"
        "      order first when needed, or returns raw sub-agent output without\n"
        "      stitching.\n"
        "  1 - Refuses, returns unrelated content, or fabricates tool results.\n"
        + _OUTPUT_CONTRACT
    )


# NOTE ON LEGACY DICT KEYS: the keys `tech_lookup`, `manual_retrieval`, and
# `work_order_draft` are kept for backwards compat with the CI harness and
# existing baseline files. Semantically they now score the order-lookup,
# policy-lookup, and resolution-draft agents.
AGENT_RUBRICS: dict[str, dict] = {
    "triage": {
        "agent_yaml": "triage.yaml",
        "display_name": "Triage Rubric",
        "description": "LLM-as-judge rubric for the Northwind triage agent (1-5 ordinal).",
        "prompt_text": build_triage_rubric(),
    },
    "tech_lookup": {
        "agent_yaml": "tech_lookup.yaml",
        "display_name": "Order Lookup Rubric",
        "description": "LLM-as-judge rubric for the Northwind order-lookup agent (1-5 ordinal).",
        "prompt_text": build_tech_lookup_rubric(),
    },
    "manual_retrieval": {
        "agent_yaml": "manual_retrieval.yaml",
        "display_name": "Policy Lookup Rubric",
        "description": "LLM-as-judge rubric for the Northwind policy-lookup agent (1-5 ordinal, grounded citations).",
        "prompt_text": build_manual_retrieval_rubric(),
    },
    "work_order_draft": {
        "agent_yaml": "work_order_draft.yaml",
        "display_name": "Resolution Draft Rubric",
        "description": "LLM-as-judge rubric for the Northwind resolution-drafting agent (1-5 ordinal, complete draft + HITL).",
        "prompt_text": build_work_order_draft_rubric(),
    },
    "orchestrator": {
        "agent_yaml": "orchestrator/agent.yaml",
        "display_name": "Orchestrator Rubric",
        "description": "LLM-as-judge rubric for the Northwind hosted orchestrator agent (1-5 ordinal, multi-step coordination).",
        "prompt_text": build_orchestrator_rubric(),
    },
}
