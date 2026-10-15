"""Create the Northwind Foundry IQ knowledge bases after `azd provision`.

Foundry IQ (Azure AI Search agentic retrieval) has two top-level objects:
  - Knowledge source: a reference to searchable content (an AI Search
    index) plus which fields to project.
  - Knowledge base: orchestrates one or more knowledge sources, and
    (optionally) an LLM for query planning + answer synthesis.

We create **one KB per department** so each department's prompt agents
retrieve only from their own corpus:

  Order Concierge    -> northwind-kb           (wraps northwind-knowledge)
  Employee Help Desk -> nw-hr-kb              (wraps nw-hr)
  Claims & Warranty -> nw-claims-kb           (wraps nw-claims)
  Contact Center    -> nw-contact-center-kb   (wraps nw-contact-center)

KB naming convention is `<index>-kb`. `scripts/create-prompt-agents.py`
relies on this convention to build the MCP tool `server_url` for each
department's `type: knowledge` yaml entry.

Both objects are created via the Search Service `2026-05-01-preview`
REST API so we don't have to force the preview `azure-search-documents`
package everywhere.

Idempotent: PUT is upsert. Called from `scripts/postprovision.{ps1,sh}`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from data.northwind import Department, load_company  # noqa: E402

_API_VERSION = "2026-05-01-preview"


def _env(name: str, *, default: str = "", required: bool = True) -> str:
    v = os.environ.get(name, default).strip()
    if required and not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def _search_headers() -> dict[str, str]:
    """Prefer admin key when populated by fetch-keys; fall back to Entra ID."""
    key = os.environ.get("AZURE_SEARCH_KEY", "").strip()
    base = {"Content-Type": "application/json"}
    if key:
        base["api-key"] = key
        return base
    client_id = os.environ.get("AZURE_CLIENT_ID") or None
    cred = DefaultAzureCredential(managed_identity_client_id=client_id)
    token = cred.get_token("https://search.azure.com/.default").token
    base["Authorization"] = f"Bearer {token}"
    return base


def _put(client: httpx.Client, url: str, body: dict[str, Any]) -> None:
    r = client.put(url, headers=_search_headers(), json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"PUT {url} -> {r.status_code}: {r.text}")


def _ks_body(name: str, index_name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "searchIndex",
        "description": description,
        "searchIndexParameters": {
            "searchIndexName": index_name,
            "semanticConfigurationName": "default",
            "sourceDataFields": [],
            "searchFields": [],
        },
    }


def _kb_body(
    name: str,
    ks_name: str,
    description: str,
    retrieval_instructions: str,
    answer_instructions: str,
    aoai_endpoint: str,
    aoai_deployment: str,
    aoai_model: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "retrievalInstructions": retrieval_instructions,
        "answerInstructions": answer_instructions,
        "outputMode": "answerSynthesis",
        "knowledgeSources": [{"name": ks_name}],
        "models": [{
            "kind": "azureOpenAI",
            "azureOpenAIParameters": {
                "resourceUri": aoai_endpoint.rstrip("/"),
                "deploymentId": aoai_deployment,
                "modelName": aoai_model,
            },
        }],
        "retrievalReasoningEffort": {"kind": "low"},
    }


# Per-department retrieval + answer instructions. Reused for both the
# flagship (Order Concierge) and the three new departments.
_DEPT_INSTRUCTIONS: dict[str, dict[str, str]] = {
    "order-concierge": {
        "ks_desc": "Northwind manuals, service reports, contracts, and warranty snippets.",
        "kb_desc": "Northwind Order Concierge knowledge base for grounded answers with citations.",
        "retrieval": (
            "Search the Northwind knowledge source for procedural or spec questions. "
            "Prefer manuals over service reports for procedural questions."
        ),
        "answer": (
            "Answer using ONLY the retrieved documents. Every factual claim must cite "
            "its source in the form [title, p.N](source_uri). If the corpus does not "
            "contain the answer, respond 'I do not know'."
        ),
    },
    "employee-help-desk": {
        "ks_desc": "Northwind Industries HR policies, benefits guides, IT runbooks, and internal ops procedures.",
        "kb_desc": "Employee Help Desk knowledge base — grounded answers for internal HR/IT questions.",
        "retrieval": (
            "Search the Employee Help Desk corpus for questions about HR policy, benefits, "
            "expense/travel, IT setup, security training, or manager approvals. "
            "Prefer the most specific policy doc (e.g., pto-policy.md over handbook-*.md)."
        ),
        "answer": (
            "Answer using ONLY the retrieved documents. Cite each factual claim with the "
            "source filename in the form [pto-policy.md]. If the corpus doesn't cover the "
            "question, say: 'I don't have that in the current Employee Help Desk knowledge "
            "base — please contact hr@northwind.example.'"
        ),
    },
    "claims-warranty": {
        "ks_desc": "Northwind warranty PDFs, service reports, damage-photo transcriptions, and claim intake forms.",
        "kb_desc": "Claims & Warranty Ops knowledge base — grounded lookup during CU triage.",
        "retrieval": (
            "Search for warranty terms, prior claims on the same equipment, and service history. "
            "Prefer manufacturer warranty PDFs over customer intake forms for warranty-eligibility questions."
        ),
        "answer": (
            "Answer using ONLY the retrieved documents. Cite each factual claim with its "
            "source file (e.g., [warranty-carrier-30gx.md]). If unknown, say 'This warranty "
            "isn't in the current Claims corpus.'"
        ),
    },
    "contact-center": {
        "ks_desc": "Northwind Contact Center call scripts, IVR flows, escalation playbook, and deflection FAQ.",
        "kb_desc": "Contact Center knowledge base — grounds the Voice Live greeting and intent routing.",
        "retrieval": (
            "Search call scripts, IVR flows, and the deflection FAQ for the caller's stated intent. "
            "The escalation playbook always wins over standard scripts."
        ),
        "answer": (
            "Answer using ONLY the retrieved documents. Keep it under 2 sentences — "
            "the Voice Live agent speaks the answer aloud. Cite the source in the form [call-scripts.md]. "
            "If the corpus doesn't cover it, say 'let me get a human.'"
        ),
    },
}


def _upsert_for_department(
    client: httpx.Client, search_endpoint: str, dept: Department,
    aoai_endpoint: str, aoai_deployment: str, aoai_model: str,
    flagship_kb_name: str, flagship_ks_name: str,
) -> None:
    # Order Concierge keeps the pre-existing kb/ks names for backward
    # compat with everything that references AZURE_SEARCH_KNOWLEDGE_BASE.
    if dept.slug == "order-concierge":
        kb_name = flagship_kb_name
        ks_name = flagship_ks_name
    else:
        kb_name = f"{dept.search_index}-kb"
        ks_name = f"{dept.search_index}-ks"

    inst = _DEPT_INSTRUCTIONS.get(dept.slug)
    if not inst:
        print(f"  [{dept.slug}] no instructions template — skipping KB creation")
        return

    print(f"\n[{dept.slug}] index={dept.search_index}  ks={ks_name}  kb={kb_name}")

    ks_url = f"{search_endpoint}/knowledgesources/{ks_name}?api-version={_API_VERSION}"
    print(f"  -> upserting knowledge source {ks_name}")
    _put(client, ks_url, _ks_body(ks_name, dept.search_index, inst["ks_desc"]))

    kb_url = f"{search_endpoint}/knowledgebases/{kb_name}?api-version={_API_VERSION}"
    print(f"  -> upserting knowledge base   {kb_name}")
    _put(client, kb_url, _kb_body(
        kb_name, ks_name, inst["kb_desc"], inst["retrieval"], inst["answer"],
        aoai_endpoint, aoai_deployment, aoai_model,
    ))


def main() -> None:
    endpoint = _env("AZURE_SEARCH_ENDPOINT").rstrip("/")
    flagship_ks = _env("AZURE_SEARCH_KNOWLEDGE_SOURCE",
                       default="northwind-knowledge-ks", required=False)
    flagship_kb = _env("AZURE_SEARCH_KNOWLEDGE_BASE",
                       default="northwind-kb", required=False)

    aoai_endpoint = _env("AZURE_OPENAI_ENDPOINT", required=False) or (
        _env("FOUNDRY_ENDPOINT").rstrip("/").replace(".cognitiveservices.azure.com", ".openai.azure.com")
    )
    aoai_deployment = _env("FOUNDRY_PROMPT_AGENT_DEPLOYMENT", default="gpt-4o", required=False)
    aoai_model = os.environ.get("FOUNDRY_PROMPT_AGENT_MODEL", aoai_deployment)

    print(f"Foundry IQ target: {endpoint}")
    print(f"  planner model:    {aoai_deployment} @ {aoai_endpoint}")

    company = load_company()

    with httpx.Client(timeout=60.0) as client:
        for dept in company.departments:
            _upsert_for_department(
                client, endpoint, dept,
                aoai_endpoint, aoai_deployment, aoai_model,
                flagship_kb, flagship_ks,
            )

    print(f"\nAll {len(company.departments)} Foundry IQ knowledge bases ready.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
