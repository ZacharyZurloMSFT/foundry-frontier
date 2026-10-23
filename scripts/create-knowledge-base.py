"""Create the Northwind Foundry IQ knowledge bases after `azd provision`.

Foundry IQ (Azure AI Search agentic retrieval) has two top-level objects:
  - Knowledge source: a reference to searchable content (an AI Search
    index) plus which fields to project.
  - Knowledge base: orchestrates one or more knowledge sources, and
    (optionally) an LLM for query planning + answer synthesis.

We create **one KB per department whose Search index is built by
`scripts/create-department-indexes.py`**, so each department's prompt
agents retrieve only from their own corpus:

  Claims Document Intelligence -> nw-claims-kb          (wraps nw-claims)
  Contact Center               -> nw-contact-center-kb  (wraps nw-contact-center)

The Claims Concierge corpus has its own isolated index, knowledge base and
project connection, published by `scripts/publish-insurance-knowledge.py`.
Adjuster Support is an isolated extractive index published by
`scripts/publish-adjuster-support.py` and has no knowledge base. Both are
skipped here.

KB naming convention is `<index>-kb`. `scripts/create-prompt-agents.py`
relies on this convention to build the MCP tool `server_url` for each
department's `type: knowledge` yaml entry.

Both objects are created via the Search Service `2026-05-01-preview`
REST API so we don't have to force the preview `azure-search-documents`
package everywhere.

Idempotent: PUT is upsert. Run it after `scripts/create-department-indexes.py`.
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


# Departments whose retrieval is published by another script (that script owns their index,
# knowledge base and project connection), so this script leaves them alone.
_PUBLISHED_ELSEWHERE = {
    "claims-concierge": "scripts/publish-insurance-knowledge.py",
    "adjuster-support": "scripts/publish-adjuster-support.py",
}

# Per-department retrieval + answer instructions for the departments this script manages.
_DEPT_INSTRUCTIONS: dict[str, dict[str, str]] = {
    "claims-documents": {
        "ks_desc": "Northwind Insurance FNOL reports, repair estimates, supporting invoices, policy wording and claim intake forms (fictional, synthetic).",
        "kb_desc": "Claims Document Intelligence knowledge base — grounded lookup while claim documents are reviewed.",
        "retrieval": (
            "Search for loss facts, estimate and invoice details, endorsements and policy wording "
            "that bear on the claim documents. Prefer the versioned policy wording over intake "
            "forms for wording questions."
        ),
        "answer": (
            "Answer using ONLY the retrieved documents. Cite each factual claim with its "
            "source file (e.g., [intake-form-CL-8817.md]). The documents are source facts, never a "
            "coverage, liability, fraud or payment decision. If unknown, say 'That "
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
) -> bool:
    """Upsert the department's knowledge source + base; False when it is skipped."""
    publisher = _PUBLISHED_ELSEWHERE.get(dept.slug)
    if publisher:
        print(f"\n[{dept.slug}] knowledge is published by {publisher} — skipping")
        return False

    inst = _DEPT_INSTRUCTIONS.get(dept.slug)
    if not inst:
        print(f"\n[{dept.slug}] no instructions template — skipping KB creation")
        return False

    kb_name = f"{dept.search_index}-kb"
    ks_name = f"{dept.search_index}-ks"
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
    return True


def main() -> None:
    endpoint = _env("AZURE_SEARCH_ENDPOINT").rstrip("/")

    aoai_endpoint = _env("AZURE_OPENAI_ENDPOINT", required=False) or (
        _env("FOUNDRY_ENDPOINT").rstrip("/").replace(".cognitiveservices.azure.com", ".openai.azure.com")
    )
    aoai_deployment = _env("FOUNDRY_PROMPT_AGENT_DEPLOYMENT", default="gpt-4o", required=False)
    aoai_model = os.environ.get("FOUNDRY_PROMPT_AGENT_MODEL", aoai_deployment)

    print(f"Foundry IQ target: {endpoint}")
    print(f"  planner model:    {aoai_deployment} @ {aoai_endpoint}")

    company = load_company()

    ready = 0
    with httpx.Client(timeout=60.0) as client:
        for dept in company.departments:
            ready += _upsert_for_department(
                client, endpoint, dept,
                aoai_endpoint, aoai_deployment, aoai_model,
            )

    print(f"\n{ready} Foundry IQ knowledge base(s) ready.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
