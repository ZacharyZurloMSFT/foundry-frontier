"""Create the Foundry project connections that let prompt agents call
each department's knowledge base via MCP.

The Foundry portal renders the KB under an agent's `Knowledge` panel only
when the agent's `MCPTool` references a project connection whose target is
the KB's MCP endpoint:

    {search}/knowledgebases/{kb}/mcp?api-version=2026-05-01-preview

Multi-project fan-out: every department gets its own Foundry project (see
`infra/modules/foundry.bicep` additionalProjects loop). Prompt agents in
each project need their OWN project connection to the department's KB.
We create the same-named connection (`northwind-kb-mcp`) inside every
department's project — same name simplifies the agent yaml
(`create-prompt-agents.py` uses a single default) while each connection
points at its own department's KB.

Authentication: Azure AI Search's MCP endpoint accepts the admin key via
the `api-key` header (it does NOT accept `Authorization: Bearer <key>` —
that returns 403). So we configure the connection with `CustomKeys` auth
so Foundry sends `api-key: <admin-key>` on every MCP call.

Env vars consumed (populated by azd + fetch-keys.ps1):
  AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP
  FOUNDRY_PROJECT_ENDPOINT          (Order Concierge flagship — for account name)
  FOUNDRY_PROJECT_ENDPOINTS         (JSON map slug -> endpoint; falls back to
                                     single-project when missing for older envs)
  AZURE_SEARCH_ENDPOINT
  AZURE_SEARCH_KEY                  (Search admin key)
  AZURE_SEARCH_KNOWLEDGE_BASE       (flagship KB name; defaults northwind-kb)
  FOUNDRY_KB_CONNECTION_NAME        (defaults to northwind-kb-mcp)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from data.northwind import load_company  # noqa: E402

_ARM_API = "2025-10-01-preview"
_KB_API = "2026-05-01-preview"


def _env(name: str, *, default: str = "", required: bool = True) -> str:
    v = os.environ.get(name, default).strip()
    if required and not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def _project_resource_id(sub: str, rg: str, ai_account: str, project: str) -> str:
    return (
        f"/subscriptions/{sub}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.CognitiveServices/accounts/{ai_account}"
        f"/projects/{project}"
    )


def _account_from_endpoint(endpoint: str) -> str:
    """Extract the AI Services account name from any project endpoint of
    the form `https://<account>.cognitiveservices.azure.com/api/projects/<project>`."""
    u = urllib.parse.urlparse(endpoint)
    account = u.hostname.split(".", 1)[0] if u.hostname else ""
    if not account:
        raise RuntimeError(f"Cannot parse account from endpoint: {endpoint}")
    return account


def _project_from_endpoint(endpoint: str) -> str:
    u = urllib.parse.urlparse(endpoint)
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        raise RuntimeError(f"Cannot parse project from endpoint: {endpoint}")
    return parts[-1]


def _resolve_endpoints() -> dict[str, str]:
    raw = os.environ.get("FOUNDRY_PROJECT_ENDPOINTS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            pass
    # Synthesis fallback — same shape as scripts/create-prompt-agents.py's
    # `_resolve_endpoints`. Lets a local postprovision.ps1 run succeed even
    # when only FOUNDRY_ENDPOINT is set.
    single = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    account_endpoint = os.environ.get("FOUNDRY_ENDPOINT", "").strip()
    if account_endpoint or single:
        base = account_endpoint.rstrip("/") if account_endpoint else ""
        if not base and single:
            base = single.rstrip("/").rsplit("/api/projects/", 1)[0]
        if base:
            company = load_company()
            synthesized = {
                d.slug: f"{base}/api/projects/{d.foundry_project}"
                for d in company.departments
            }
            if single:
                synthesized["order-concierge"] = single
            print(f"Synthesized {len(synthesized)} project endpoints from "
                  f"FOUNDRY_ENDPOINT + manifest.")
            return synthesized
    if not single:
        raise RuntimeError("Neither FOUNDRY_PROJECT_ENDPOINTS nor FOUNDRY_PROJECT_ENDPOINT set.")
    return {"order-concierge": single}


def _connect_one(
    client: httpx.Client,
    arm_token: str,
    project_id: str,
    conn_name: str,
    search_endpoint: str,
    search_key: str,
    kb_name: str,
) -> None:
    mcp_endpoint = f"{search_endpoint}/knowledgebases/{kb_name}/mcp?api-version={_KB_API}"
    url = f"https://management.azure.com{project_id}/connections/{conn_name}?api-version={_ARM_API}"
    body = {
        "name": conn_name,
        "type": "Microsoft.CognitiveServices/accounts/projects/connections",
        "properties": {
            "authType": "CustomKeys",
            "category": "RemoteTool",
            "target": mcp_endpoint,
            "isSharedToAll": True,
            "credentials": {
                "keys": {"api-key": search_key}
            },
            "metadata": {"ApiType": "Azure"},
        },
    }
    print(f"  -> PUT {url}")
    print(f"     target: {mcp_endpoint}")
    r = client.put(
        url,
        headers={"Authorization": f"Bearer {arm_token}", "Content-Type": "application/json"},
        json=body,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"PUT {url} -> {r.status_code}: {r.text}")


def main() -> None:
    sub = _env("AZURE_SUBSCRIPTION_ID")
    rg = _env("AZURE_RESOURCE_GROUP")
    search_endpoint = _env("AZURE_SEARCH_ENDPOINT").rstrip("/")
    search_key = _env("AZURE_SEARCH_KEY")
    flagship_kb = os.environ.get("AZURE_SEARCH_KNOWLEDGE_BASE", "northwind-kb").strip() or "northwind-kb"
    # Connection names in Foundry are account-scoped, not project-scoped —
    # a single `northwind-kb-mcp` in the Order Concierge project blocks the
    # same name in every other project. So we derive one connection name
    # per KB: `<kb>-mcp`. The flagship convention (`northwind-kb-mcp`)
    # falls out naturally from `northwind-kb` -> `northwind-kb-mcp`.
    flagship_conn = os.environ.get(
        "FOUNDRY_KB_CONNECTION_NAME",
        f"{os.environ.get('ORDER_PROJECT_NAME', 'northwind-orders')}-kb-mcp",
    ).strip()
    if not flagship_conn:
        raise ValueError("FOUNDRY_KB_CONNECTION_NAME must not be empty")

    endpoints = _resolve_endpoints()
    company = load_company()

    # The AI Services account name is the same for every project (they all
    # live inside one account). Pull it from any project endpoint.
    ai_account = _account_from_endpoint(next(iter(endpoints.values())))

    cred = DefaultAzureCredential(
        managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID") or None,
    )
    arm_token = cred.get_token("https://management.azure.com/.default").token

    print(f"AI account: {ai_account}")
    print(f"Search endpoint: {search_endpoint}")
    print("Connections are account-scoped; each project gets a unique name.")

    with httpx.Client(timeout=60.0) as client:
        for dept in company.departments:
            endpoint = endpoints.get(dept.slug)
            if not endpoint:
                print(f"\n[{dept.slug}] SKIPPED — no endpoint in FOUNDRY_PROJECT_ENDPOINTS")
                continue

            project = _project_from_endpoint(endpoint)
            project_id = _project_resource_id(sub, rg, ai_account, project)

            # Order Concierge keeps flagship KB name for backward compat
            # with any legacy AZURE_SEARCH_KNOWLEDGE_BASE reader.
            if dept.slug == "order-concierge":
                kb_name = flagship_kb
                conn_name = flagship_conn
            else:
                kb_name = f"{dept.search_index}-kb"
                conn_name = f"{kb_name}-mcp"

            print(f"\n[{dept.slug}] project={project}  kb={kb_name}  conn={conn_name}")
            _connect_one(
                client, arm_token, project_id, conn_name,
                search_endpoint, search_key, kb_name,
            )

    print(f"\nAll {len(endpoints)} project connections ready.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
