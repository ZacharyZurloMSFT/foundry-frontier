"""Create/update the Northwind Foundry prompt agents after `azd provision`.

The demo is organized around one company (Northwind Industries) with four
departments, each backed by its own Foundry project inside the shared
AI Services account. This script iterates every department listed in
`data/northwind/company.yaml` and creates that department's prompt
agents in the department's own project:

  - `order-concierge` → the flagship 4-agent set + dispatcher/parts,
    read from the pre-existing top-level `agents/*.yaml` for backward
    compat.
  - `employee-help-desk` → `northwind-hr-assistant` from the department's
    `prompts/*.yaml`.
  - `claims-warranty` → `northwind-claims-triage`.
  - `contact-center` → `northwind-intent-classifier`.

Project endpoints are resolved from the `FOUNDRY_PROJECT_ENDPOINTS` env
var (a JSON string emitted by main.bicep). When that var is missing
(e.g., an older env deployed before the multi-project change) the
script falls back to the single `FOUNDRY_PROJECT_ENDPOINT` and creates
every agent there — preserves backward compatibility.

Idempotent — re-runs update instructions/tools in place using name as
the key. Agent id in Foundry equals the agent name, so callers resolve
agents by name at runtime (no secret store required).

Model is resolved from `FOUNDRY_PROMPT_AGENT_DEPLOYMENT` (a gpt-4o
deployment, because prompt-agent function tools don't yet work on
gpt-5.x). Falls back to `FOUNDRY_WORKER_DEPLOYMENT` for older
environments that predate the split.

Fails loudly (non-zero exit) on any per-agent error so post-provision
surfaces the failure instead of silently leaving Foundry empty.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_AGENTS_DIR = _ROOT / "agents"

# Make `data.northwind` importable regardless of cwd.
sys.path.insert(0, str(_ROOT))

from azure.ai.projects.aio import AIProjectClient  # noqa: E402
from azure.identity.aio import DefaultAzureCredential  # noqa: E402

from data.northwind import Department, load_company  # noqa: E402


def _get_credential():
    """AIProjectClient in azure-ai-projects>=2.x requires a TokenCredential
    (it calls get_token internally). AzureKeyCredential is not accepted for
    the project client, so we always use DefaultAzureCredential regardless
    of whether a Foundry API key is present. Passing the UAMI client id
    (from AZURE_CLIENT_ID) when set nudges DefaultAzureCredential toward
    the right identity in Container Apps but stays interactive-friendly
    for local runs."""
    client_id = os.environ.get("AZURE_CLIENT_ID") or None
    return DefaultAzureCredential(managed_identity_client_id=client_id)

# Yaml files that are NOT prompt agents (e.g., hosted agent scaffolds live
# under agents/<name>/ folders, not top-level yamls).
_SKIP = {"orchestrator.yaml", "connected-agents.yaml"}


def _resolve_model() -> str:
    model = (
        os.environ.get("FOUNDRY_PROMPT_AGENT_DEPLOYMENT", "").strip()
        or os.environ.get("FOUNDRY_WORKER_DEPLOYMENT", "").strip()
    )
    if not model:
        raise RuntimeError(
            "Neither FOUNDRY_PROMPT_AGENT_DEPLOYMENT nor FOUNDRY_WORKER_DEPLOYMENT "
            "is set — cannot resolve model for prompt agents. Ensure `azd provision` "
            "has completed and the env vars are in .azure/<env>/.env."
        )
    return model


def _expand_env(v: Any) -> Any:
    """Expand ${VAR} references in string yaml values against os.environ.
    Non-string values pass through unchanged."""
    if not isinstance(v, str):
        return v
    out = v
    for name, val in os.environ.items():
        out = out.replace(f"${{{name}}}", val)
    return out


def _knowledge_tools_for_agent(cfg: dict[str, Any], search_index: str | None = None) -> list[dict[str, Any]]:
    """Translate the yaml `knowledge:` block into MCPTool entries that
    Foundry renders under the agent's Knowledge panel. Each entry becomes
    an MCPTool pointing at a project RemoteTool connection (created by
    scripts/connect-knowledge-base.py) whose target is the KB's MCP
    endpoint. `allowed_tools=[knowledge_base_retrieve]` and
    `require_approval=never` match the pattern documented at
    https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/foundry-iq-connect
    """
    ks = cfg.get("knowledge") or []
    out: list[dict[str, Any]] = []
    for k in ks:
        if not isinstance(k, dict):
            continue
        kind = k.get("kind", "foundry_iq")
        if kind != "foundry_iq":
            print(f"  ! unknown knowledge kind '{kind}' on '{cfg['name']}' - skipping.")
            continue
        kb = _expand_env(k.get("knowledge_base"))
        conn = _expand_env(k.get("connection"))
        # If the yaml used ${FOUNDRY_KB_CONNECTION_NAME} but the env var
        # isn't set, fall back to the well-known default that
        # scripts/connect-knowledge-base.py provisions. Same for KB name.
        if not kb or (isinstance(kb, str) and kb.startswith("${")):
            kb = os.environ.get("AZURE_SEARCH_KNOWLEDGE_BASE", "northwind-kb")
        if not conn or (isinstance(conn, str) and conn.startswith("${")):
            conn = os.environ.get("FOUNDRY_KB_CONNECTION_NAME", "northwind-kb-mcp")
        if not kb or not conn:
            print(f"  ! knowledge on '{cfg['name']}' missing knowledge_base or connection - skipping.")
            continue
        search_endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
        if not search_endpoint:
            raise RuntimeError("AZURE_SEARCH_ENDPOINT not set - cannot build MCP server_url.")
        server_url = f"{search_endpoint}/knowledgebases/{kb}/mcp?api-version=2026-05-01-preview"
        out.append({
            "type": "mcp",
            "server_label": kb,
            "server_url": server_url,
            "require_approval": "never",
            "allowed_tools": ["knowledge_base_retrieve"],
            "project_connection_id": conn,
        })
    # Non-order-concierge departments declare a target Search index in their
    # department.yaml (`search_index`) and their prompt agents use a
    # `type: knowledge` tool block that we translate to the same MCP shape
    # against the per-department index. Simplified: if the yaml has a
    # `tools: [{type: knowledge, index: nw-hr}]` entry, we emit one MCP
    # tool per matching index against the shared Search KB service.
    #
    # Connection naming: Foundry connection names are account-scoped, so
    # every project needs its own uniquely-named connection. Convention:
    # `<kb-name>-mcp` (e.g., `northwind-kb-mcp`, `nw-hr-kb-mcp`). This
    # matches what scripts/connect-knowledge-base.py creates.
    for t in cfg.get("tools") or []:
        if isinstance(t, dict) and t.get("type") == "knowledge":
            idx = _expand_env(t.get("index")) or search_index
            if not idx:
                print(f"  ! knowledge tool on '{cfg['name']}' has no index - skipping.")
                continue
            search_endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
            if not search_endpoint:
                raise RuntimeError("AZURE_SEARCH_ENDPOINT not set - cannot build MCP server_url.")
            # Order Concierge flagship keeps the pre-existing kb/conn names
            # for backward compat with everything that references
            # AZURE_SEARCH_KNOWLEDGE_BASE / FOUNDRY_KB_CONNECTION_NAME.
            if idx == os.environ.get("AZURE_SEARCH_INDEX", "northwind-knowledge"):
                kb = os.environ.get("AZURE_SEARCH_KNOWLEDGE_BASE", "northwind-kb")
                conn = os.environ.get("FOUNDRY_KB_CONNECTION_NAME", f"{kb}-mcp")
            else:
                kb = f"{idx}-kb"
                conn = f"{kb}-mcp"
            server_url = f"{search_endpoint}/knowledgebases/{kb}/mcp?api-version=2026-05-01-preview"
            out.append({
                "type": "mcp",
                "server_label": kb,
                "server_url": server_url,
                "require_approval": "never",
                "allowed_tools": ["knowledge_base_retrieve"],
                "project_connection_id": conn,
            })
    return out


def _tools_for_agent(cfg: dict[str, Any], search_index: str | None = None) -> list[dict[str, Any]]:
    tools = cfg.get("tools") or []
    resolved: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        kind = t.get("type")
        if kind == "function":
            # Foundry Prompt Agents use the flat Responses-API function shape:
            #   { type: function, name, description, parameters }
            # YAMLs may use the nested Assistants shape:
            #   { type: function, function: { name, description, parameters } }
            # Accept either and flatten.
            fn = t.get("function") or {}
            name = t.get("name") or fn.get("name")
            if not name:
                print(f"  ! '{cfg['name']}' has a function tool without a name - skipping.")
                continue
            resolved.append({
                "type": "function",
                "name": name,
                "description": t.get("description") or fn.get("description", ""),
                "parameters": t.get("parameters") or fn.get("parameters") or {"type": "object", "properties": {}},
            })
        elif kind == "mcp":
            # Pass MCP tool entries through, expanding ${VAR} refs in
            # server_url (and headers, if any) against the current env.
            # Same shape as the KB MCP entry built in
            # _knowledge_tools_for_agent, but sourced from yaml so any
            # prompt-agent can point at any MCP server.
            server_url = _expand_env(t.get("server_url"))
            if not server_url:
                print(f"  ! '{cfg['name']}' has an mcp tool without a server_url - skipping.")
                continue
            # FastMCP mounts at `/mcp/` and issues a 307 redirect from `/mcp`.
            # Foundry's MCP client does NOT follow redirects — it returns
            # 400 tool_user_error at agent-invocation time. Normalize any
            # `/mcp` (no trailing slash) URL here so old azd envs that were
            # provisioned before the Bicep fix keep working after a
            # re-run of this script.
            if server_url.rstrip("?").endswith("/mcp"):
                server_url = server_url.rstrip("/") + "/"
            mcp_entry: dict[str, Any] = {
                "type": "mcp",
                "server_label": t.get("server_label") or cfg["name"],
                "server_url": server_url,
                "require_approval": t.get("require_approval", "never"),
            }
            if t.get("allowed_tools"):
                mcp_entry["allowed_tools"] = t["allowed_tools"]
            headers = t.get("headers")
            if isinstance(headers, dict):
                mcp_entry["headers"] = {k: _expand_env(v) for k, v in headers.items()}
            if t.get("project_connection_id"):
                mcp_entry["project_connection_id"] = _expand_env(t["project_connection_id"])
            resolved.append(mcp_entry)
        elif kind == "knowledge":
            # Handled in _knowledge_tools_for_agent (below).
            continue
        else:
            resolved.append(t)
    # Knowledge-base MCP entries are represented as MCPTools on the same
    # tools list; Foundry categorizes them into the Knowledge panel based on
    # server_label + allowed_tools.
    resolved.extend(_knowledge_tools_for_agent(cfg, search_index=search_index))
    return resolved


async def _upsert_agent(agents, cfg: dict[str, Any], model: str, search_index: str | None = None) -> str:
    """Create-or-update a Foundry prompt agent by name. `agents.create_version`
    is upsert-friendly: it creates the agent record on first call and appends
    a new immutable version on subsequent calls.

    A yaml may override the department-default model via a `model:` field —
    used by the Ask Northwind trio (northwind-hr-assistant-{gpt-5-4,
    gpt-5-4-mini,kimi}) so the UI dropdown can pick a specific model without
    changing the shared HR instructions or RAG grounding."""
    from azure.ai.projects.models import PromptAgentDefinition

    tools = _tools_for_agent(cfg, search_index=search_index)
    agent_model = _expand_env(cfg.get("model")) or model

    exists = False
    try:
        await agents.get(cfg["name"])
        exists = True
    except Exception:  # noqa: BLE001
        exists = False

    definition_kwargs: dict[str, Any] = {
        "kind": "prompt",
        "model": agent_model,
        "instructions": cfg["instructions"],
    }
    if tools:
        definition_kwargs["tools"] = tools
    definition = PromptAgentDefinition(**definition_kwargs)

    print(f"  {'* new version of' if exists else '+ creating'} {cfg['name']} (model={agent_model})")
    await agents.create_version(
        agent_name=cfg["name"],
        definition=definition,
        description=cfg.get("description", ""),
    )
    # Agent id in Foundry is the agent name (versions are numbered separately).
    return cfg["name"]


def _resolve_endpoints() -> dict[str, str]:
    """Return department-slug -> Foundry project endpoint.

    Prefers `FOUNDRY_PROJECT_ENDPOINTS` (JSON emitted by main.bicep). Falls
    back to a single-project map keyed by `order-concierge` for older envs.
    If neither map is set but `FOUNDRY_ENDPOINT` is available, synthesizes
    per-department endpoints from the manifest's `foundry_project` names —
    this is what makes local `postprovision.ps1` runs succeed even when
    the azd Bicep outputs haven't been loaded into the shell.
    """
    raw = os.environ.get("FOUNDRY_PROJECT_ENDPOINTS", "").strip()
    if raw:
        try:
            endpoints = json.loads(raw)
            if not isinstance(endpoints, dict):
                raise ValueError("must be a JSON object")
            return {str(k): str(v) for k, v in endpoints.items()}
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"WARNING: FOUNDRY_PROJECT_ENDPOINTS not valid JSON — falling back "
                  f"to synthesis. ({exc})")

    single = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    account_endpoint = os.environ.get("FOUNDRY_ENDPOINT", "").strip()

    # Synthesis path: derive per-project endpoints from FOUNDRY_ENDPOINT +
    # the department manifest's foundry_project names. Requires that the
    # projects were already created by Bicep (they are; azd provision runs
    # before this script), so the endpoints resolve at runtime.
    if account_endpoint or single:
        from data.northwind import load_company
        base = account_endpoint.rstrip("/") if account_endpoint else ""
        if not base and single:
            # Derive account endpoint from the flagship project endpoint
            # (https://<account>.cognitiveservices.azure.com/api/projects/<name>).
            base = single.rstrip("/").rsplit("/api/projects/", 1)[0]
        if base:
            company = load_company()
            synthesized = {
                d.slug: f"{base}/api/projects/{d.foundry_project}"
                for d in company.departments
            }
            # Order Concierge flagship keeps the exact FOUNDRY_PROJECT_ENDPOINT
            # if it was set — preserves any preview-only quirks.
            if single:
                synthesized["order-concierge"] = single
            print(f"Synthesized {len(synthesized)} project endpoints from "
                  f"FOUNDRY_ENDPOINT + manifest.")
            return synthesized

    if not single:
        raise RuntimeError(
            "Neither FOUNDRY_PROJECT_ENDPOINTS nor FOUNDRY_PROJECT_ENDPOINT is set."
        )
    return {"order-concierge": single}


def _prompt_yamls_for_department(dept: Department) -> list[Path]:
    """Order Concierge points at the pre-existing top-level `agents/*.yaml`
    (backward compat). Other departments have their prompts colocated
    under `data/northwind/departments/<slug>/prompts/*.yaml`.
    """
    return sorted(
        p for p in dept.prompts_dir.glob("*.yaml")
        if p.name not in _SKIP
    )


async def _create_for_department(dept: Department, endpoint: str, model: str,
                                 cred) -> tuple[list[str], list[tuple[str, str]]]:
    if dept.slug == "order-concierge" and os.environ.get("ORDER_AGENTS_HOSTED", "true").lower() == "true":
        print("\n[order-concierge] Hosted agents are deployed by the five azure.yaml agent services; prompt provisioning skipped.")
        return [], []
    yamls = _prompt_yamls_for_department(dept)
    if not yamls:
        print(f"\n[{dept.slug}] no prompt agents found under {dept.prompts_dir}")
        return [], []

    print(f"\n[{dept.slug}] project endpoint = {endpoint}")
    print(f"  {len(yamls)} prompt agents · search_index={dept.search_index}")

    ids: list[str] = []
    failures: list[tuple[str, str]] = []
    async with AIProjectClient(endpoint=endpoint, credential=cred) as project:
        for path in yamls:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not cfg or "name" not in cfg or "instructions" not in cfg:
                print(f"  ✗ {path.name}: missing 'name' or 'instructions'")
                failures.append((path.name, "invalid yaml"))
                continue
            try:
                agent_id = await _upsert_agent(
                    project.agents, cfg, model, search_index=dept.search_index,
                )
                ids.append(agent_id)
                print(f"    id = {agent_id}")
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                failures.append((cfg.get("name", path.name), str(exc)))
    return ids, failures


async def main() -> None:
    model = _resolve_model()
    endpoints = _resolve_endpoints()

    company = load_company()

    print(f"Creating Foundry prompt agents across {len(company.departments)} "
          f"department(s) of {company.name}")
    print(f"Model deployment: {model}")

    cred = _get_credential()

    all_failures: list[tuple[str, str]] = []
    total_ids: list[str] = []

    for dept in company.departments:
        endpoint = endpoints.get(dept.slug)
        if not endpoint:
            print(f"\n[{dept.slug}] SKIPPED — no endpoint in FOUNDRY_PROJECT_ENDPOINTS")
            all_failures.append((dept.slug, "no project endpoint"))
            continue
        ids, failures = await _create_for_department(dept, endpoint, model, cred)
        total_ids.extend(ids)
        all_failures.extend(failures)

    if all_failures:
        print("\nOne or more agents failed:", all_failures)
        sys.exit(1)
    print(f"\nAll {len(total_ids)} prompt agents ready (resolve by name).")


if __name__ == "__main__":
    asyncio.run(main())
