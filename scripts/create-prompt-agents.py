"""Create/update the Northwind Insurance Foundry prompt agents after `azd provision`.

The demo is organized around one company (Northwind Insurance) with four
departments, each backed by its own Foundry project inside the shared
AI Services account. This script iterates every department listed in
`data/northwind/company.yaml` and creates that department's prompt
agents, read from its `prompts/*.yaml`, in the department's own project:

  - `claims-concierge` → nothing: its agents are the hosted agents that
    `scripts/deploy-hosted-agents.py` deploys.
  - `adjuster-support` → `northwind-insurance-adjuster-support`.
  - `claims-documents` → `northwind-claims-triage`.
  - `contact-center` → `northwind-intent-classifier`.

Project endpoints are resolved from the `FOUNDRY_PROJECT_ENDPOINTS` env
var (a JSON string emitted by main.bicep). Keys written with a deprecated
department slug (`order-concierge`, `employee-help-desk`, `claims-warranty`)
are mapped to the canonical slug. When that var is missing
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

API Management gateway (`apim-northwind`): a yaml `type: mcp` tool whose `server_url` points at
the gateway (a `*.azure-api.net` host, or the host of `NORTHWIND_GATEWAY_URL`) authenticates through a
project connection (RemoteTool/CustomKeys holding the `Ocp-Apim-Subscription-Key`) named by
`MCP_GATEWAY_CONNECTION_POLICY_LOOKUP` or `MCP_GATEWAY_CONNECTION_CLAIM_REVIEW`, chosen by the
URL's `/mcp/<kind>` ending, unless the yaml sets its own `project_connection_id`. The deprecated
`MCP_GATEWAY_CONNECTION_TECH_LOOKUP` / `MCP_GATEWAY_CONNECTION_WORK_ORDER` names, and the
`MCP_TECH_LOOKUP_URL` / `MCP_WORK_ORDER_URL` spellings of `MCP_POLICY_LOOKUP_URL` /
`MCP_CLAIM_REVIEW_URL`, are read only when the canonical variable is unset. Foundry uses
a referenced connection's *target* as the MCP endpoint, so each connection's target must be
exactly the tool's `server_url`; one connection can never serve two endpoints. Gateway URLs are
never rewritten; direct Container App URLs behave exactly as before. Those connections are never
attached to a URL on the AI Gateway host.

AI Gateway routing (opt-in; unset means the behaviour above, unchanged).
Model calls: NOT USABLE with the AI Gateway tier (verified 2026-10-22, docs/ai-gateway.md
finding 9). Foundry rejects a model id that contains '/', and the gateway needs a
`<provider>/<registration>` alias in the request body, so agents created through
`AI_GATEWAY_MODEL_CONNECTION` fail at inference; `main` warns when it is set.
When it names a Foundry `ApiManagement` model connection created by
`scripts/connect-model-gateway.py`, every agent model (the department default and any
yaml `model:` override) is sent as `<connection>/<provider>/<registration>`.
`AI_GATEWAY_MODEL_PROVIDER` (default `aisvc-zurlo-northwind`) and `AI_GATEWAY_MODEL_ALIASES`
(JSON map of deployment -> full alias, wins over derivation) tune the alias.
`AI_GATEWAY_REQUIRED` is validated but does not apply to prompt-agent models: leaving the
connection unset creates direct agents, so this script keeps working when the
flag is set for the hosted agents and the API.

The knowledge-base MCP tools follow the same opt-in. `AI_GATEWAY_MCP_KB_ROUTES`
is a JSON object keyed by Search knowledge-base name:

  {"northwind-insurance-kb": {"toolserver": "northwind-insurance-knowledge", "connection": "northwind-insurance-kb-gw"}}

A KB with a route gets `server_url` = that gateway tool server's MCP URL,
`project_connection_id` = the project connection holding the gateway runtime key
(create it with `scripts/connect-mcp-gateway.py`) and `allowed_tools` prefixed
with the tool server namespace (`tool_prefix`, default `knowledge_`). A KB
without a route keeps its direct Search endpoint. `AI_GATEWAY_MCP_REQUIRED=true`
turns a KB without a route, or a yaml `mcp` tool that is not on the AI Gateway,
into an error for that agent: it is not created, the run exits non-zero, and
agents that comply are still created. Run `--dry-run` first to see every such
agent before anything is sent.

`--dry-run` prints every planned agent (name, final model string, tools) and
makes no Foundry call.

Fails loudly (non-zero exit) on any per-agent error so a failed run is not
mistaken for a Foundry that was silently left empty.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import sys
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import yaml

_ROOT = Path(__file__).resolve().parents[1]

# Make `data.northwind` and the sibling gateway helpers importable regardless of cwd.
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from azure.ai.projects.aio import AIProjectClient  # noqa: E402
from azure.identity.aio import DefaultAzureCredential  # noqa: E402

from data.northwind import Department, load_company  # noqa: E402

ai_gateway_common = importlib.import_module("ai_gateway_common")

# Departments whose agents are hosted agents (scripts/deploy-hosted-agents.py), not prompt agents.
_HOSTED_DEPARTMENTS = {"claims-concierge"}

# Deprecated department slugs still found in already-deployed FOUNDRY_PROJECT_ENDPOINTS values.
LEGACY_DEPARTMENT_SLUGS = {
    "order-concierge": "claims-concierge",
    "employee-help-desk": "adjuster-support",
    "claims-warranty": "claims-documents",
}

# Canonical variable -> deprecated alias. The canonical name wins; the alias is read only when the
# canonical variable is unset or blank.
ENV_ALIASES = {
    "MCP_POLICY_LOOKUP_URL": "MCP_TECH_LOOKUP_URL",
    "MCP_CLAIM_REVIEW_URL": "MCP_WORK_ORDER_URL",
    "MCP_GATEWAY_CONNECTION_POLICY_LOOKUP": "MCP_GATEWAY_CONNECTION_TECH_LOOKUP",
    "MCP_GATEWAY_CONNECTION_CLAIM_REVIEW": "MCP_GATEWAY_CONNECTION_WORK_ORDER",
}

# Isolated insurance knowledge base published by scripts/publish-insurance-knowledge.py.
DEFAULT_KNOWLEDGE_INDEX = "northwind-insurance-knowledge"
DEFAULT_KNOWLEDGE_BASE = "northwind-insurance-kb"
DEFAULT_KB_CONNECTION = "northwind-insurance-kb-mcp"


def _alias_env(canonical: str) -> str | None:
    """The value of a canonical variable, else of its deprecated alias; None when neither is set."""
    for name in (canonical, ENV_ALIASES.get(canonical)):
        value = os.environ.get(name, "").strip() if name else ""
        if value:
            return value
    return None


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


DEFAULT_GATEWAY_PROVIDER = ai_gateway_common.MODEL_PROVIDER
_FLAG_ON = ("true", "1", "yes")
_FLAG_OFF = ("", "false", "0", "no", "off")
# Foundry connection names (ARM accounts/connections): 3-33 characters.
_CONNECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,32}")
_PROVIDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_FULL_ALIAS = re.compile(r"[^/\s]+/[^/\s]+")


class GatewayModels(NamedTuple):
    """Foundry `ApiManagement` model connection that fronts the AI Gateway."""

    connection: str
    provider: str
    aliases: Mapping[str, str]


DEFAULT_KB_TOOL_PREFIX = "knowledge_"
_KB_ROUTE_FIELDS = {"toolserver", "connection", "tool_prefix"}
_TOOL_PREFIX = re.compile(r"[a-z0-9_]*")


class KbRoute(NamedTuple):
    """Gateway tool server that fronts one Search knowledge-base MCP endpoint."""

    url: str
    connection: str
    tool_prefix: str


class GatewayMcp(NamedTuple):
    """Gateway routing for the knowledge-base MCP tools of prompt agents."""

    required: bool
    routes: Mapping[str, KbRoute]


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    # A typo must not silently disable a fail-closed control.
    value = str(env.get(name) or "").strip().lower()
    if value in _FLAG_ON:
        return True
    if value in _FLAG_OFF:
        return False
    raise ValueError(f"{name} must be true, 1 or yes to enable (false, 0, no, off or empty to disable)")


def gateway_model_string(model: str, connection: str, provider: str = DEFAULT_GATEWAY_PROVIDER,
                         aliases: Mapping[str, str] | None = None) -> str:
    """`<connection>/<alias>`: how a prompt agent names a model served through the gateway connection.

    The alias is the override from `aliases` (deployment -> full alias) or, by
    default, `<provider>/<registration>` (ai_gateway_common.model_alias). A model
    that already starts with `<connection>/` is returned unchanged.
    """
    model = model.strip()
    if "$" in model:
        raise ValueError(f"model {model!r} contains an unresolved placeholder")
    if model.startswith(f"{connection}/"):
        return model
    if "/" in model:
        raise ValueError(
            f"model {model!r} is already qualified with a different connection; "
            f"it cannot be routed through gateway connection {connection!r}"
        )
    alias = (aliases or {}).get(model) or ai_gateway_common.model_alias(model, provider)
    return f"{connection}/{alias}"


def _parse_aliases(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    message = ("AI_GATEWAY_MODEL_ALIASES must be a JSON object mapping deployment names "
               "to <provider>/<registration> aliases")
    try:
        document = json.loads(raw)
    except ValueError:
        raise ValueError(message) from None
    if not isinstance(document, dict) or not all(isinstance(alias, str) for alias in document.values()):
        raise ValueError(message)
    aliases: dict[str, str] = {}
    for deployment, alias in document.items():
        if not (deployment.strip() and _FULL_ALIAS.fullmatch(alias.strip())):
            raise ValueError(f"{message}; got {deployment!r}: {alias!r}")
        aliases[deployment.strip()] = alias.strip()
    return aliases


def gateway_models_from_env(env: Mapping[str, str] | None = None) -> GatewayModels | None:
    """The gateway model routing requested by the environment, or None for direct models.

    Raises ValueError for a malformed setting. AI_GATEWAY_REQUIRED is validated (a typo must not silently
    disable a fail-closed control) but does not make a model connection mandatory: prompt agents cannot be
    routed through the AI Gateway tier (Foundry rejects its `provider/registration` alias as a model id, see
    docs/ai-gateway.md finding 9), so requiring one would only break this script whenever the flag is set for the
    hosted agents and the API.
    """
    source = os.environ if env is None else env
    _env_flag(source, "AI_GATEWAY_REQUIRED")
    connection = str(source.get("AI_GATEWAY_MODEL_CONNECTION") or "").strip()
    if not connection:
        return None
    if not _CONNECTION_NAME.fullmatch(connection):
        raise ValueError("AI_GATEWAY_MODEL_CONNECTION must be a Foundry connection name "
                         "(3-33 letters, digits, '-' or '_', starting with a letter or digit)")
    provider = str(source.get("AI_GATEWAY_MODEL_PROVIDER") or "").strip() or DEFAULT_GATEWAY_PROVIDER
    if not _PROVIDER_NAME.fullmatch(provider):
        raise ValueError("AI_GATEWAY_MODEL_PROVIDER must be a gateway model provider name without '/' or spaces")
    return GatewayModels(connection, provider, _parse_aliases(source.get("AI_GATEWAY_MODEL_ALIASES", "")))


def resolve_agent_model(model: str, gateway: GatewayModels | None) -> str:
    """The `model` for PromptAgentDefinition: unchanged unless a gateway connection is configured."""
    if gateway is None:
        return model
    return gateway_model_string(model, gateway.connection, gateway.provider, gateway.aliases)


def _parse_kb_routes(raw: str) -> dict[str, KbRoute]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    message = ("AI_GATEWAY_MCP_KB_ROUTES must be a JSON object keyed by knowledge-base name; each value is "
               '{"toolserver": "<tool server name>", "connection": "<project connection>", '
               '"tool_prefix": "knowledge_"}')
    try:
        items = list(json.loads(raw).items())
    except (ValueError, AttributeError):
        raise ValueError(message) from None
    routes: dict[str, KbRoute] = {}
    for kb, entry in items:
        name = f"AI_GATEWAY_MCP_KB_ROUTES[{kb}]"
        if not kb.strip() or not isinstance(entry, dict):
            raise ValueError(message)
        unknown = set(entry) - _KB_ROUTE_FIELDS
        if unknown:
            raise ValueError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")
        toolserver = entry.get("toolserver")
        connection = entry.get("connection")
        prefix = entry.get("tool_prefix", DEFAULT_KB_TOOL_PREFIX)
        if not all(isinstance(value, str) for value in (toolserver, connection, prefix)):
            raise ValueError(f"{name} needs string toolserver and connection fields")
        try:
            url = ai_gateway_common.toolserver_url(toolserver.strip())
        except ValueError:
            raise ValueError(f"{name}.toolserver must be a gateway tool server name "
                             "(lowercase letters, digits and '-')") from None
        if not _CONNECTION_NAME.fullmatch(connection.strip()):
            raise ValueError(f"{name}.connection must be a project connection name "
                             "(3-33 letters, digits, '-' or '_', starting with a letter or digit)")
        if not _TOOL_PREFIX.fullmatch(prefix.strip()):
            raise ValueError(f"{name}.tool_prefix must match [a-z0-9_]*")
        routes[kb.strip()] = KbRoute(url, connection.strip(), prefix.strip())
    return routes


def gateway_mcp_from_env(env: Mapping[str, str] | None = None) -> GatewayMcp | None:
    """The gateway MCP routing requested by the environment, or None for direct MCP servers."""
    source = os.environ if env is None else env
    required = _env_flag(source, "AI_GATEWAY_MCP_REQUIRED")
    routes = _parse_kb_routes(source.get("AI_GATEWAY_MCP_KB_ROUTES", ""))
    if not routes and not required:
        return None
    return GatewayMcp(required, routes)


def _on_gateway(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == urlsplit(ai_gateway_common.GATEWAY_URL).hostname


def _kb_mcp_entry(agent: str, kb: str, direct_connection: str, mcp: GatewayMcp | None) -> dict[str, Any]:
    """The knowledge-base MCPTool: via its gateway tool server when routed, else straight to Search."""
    route = mcp.routes.get(kb) if mcp else None
    if route:
        return {
            "type": "mcp",
            "server_label": kb,
            "server_url": route.url,
            "require_approval": "never",
            "allowed_tools": [route.tool_prefix + "knowledge_base_retrieve"],
            "project_connection_id": route.connection,
        }
    if mcp and mcp.required:
        raise ValueError(
            f"AI_GATEWAY_MCP_REQUIRED is set but AI_GATEWAY_MCP_KB_ROUTES has no route for knowledge base "
            f"{kb!r} (agent {agent!r}): it would call Search directly. Register a gateway tool server for it "
            "and add the route (see docs/ai-gateway.md)."
        )
    search_endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
    if not search_endpoint:
        raise RuntimeError("AZURE_SEARCH_ENDPOINT not set - cannot build MCP server_url.")
    return {
        "type": "mcp",
        "server_label": kb,
        "server_url": f"{search_endpoint}/knowledgebases/{kb}/mcp?api-version=2026-05-01-preview",
        "require_approval": "never",
        "allowed_tools": ["knowledge_base_retrieve"],
        "project_connection_id": direct_connection,
    }


def _expand_env(v: Any) -> Any:
    """Expand ${VAR} references in string yaml values against os.environ.
    A canonical variable and its deprecated alias expand to the same value, the canonical
    one winning. Non-string values pass through unchanged."""
    if not isinstance(v, str):
        return v
    out = v
    for canonical, alias in ENV_ALIASES.items():
        value = _alias_env(canonical)
        if value is not None:
            out = out.replace(f"${{{canonical}}}", value).replace(f"${{{alias}}}", value)
    for name, val in os.environ.items():
        out = out.replace(f"${{{name}}}", val)
    return out


def _is_gateway_url(url: str) -> bool:
    """True when an MCP server URL targets an API gateway: any *.azure-api.net host, or the host of
    NORTHWIND_GATEWAY_URL (a custom gateway domain)."""
    configured = os.environ.get("NORTHWIND_GATEWAY_URL", "").strip()
    try:
        host = urlsplit(url).hostname or ""
        custom = urlsplit(configured if "//" in configured else f"//{configured}").hostname if configured else None
    except ValueError:
        return False
    return host.endswith(".azure-api.net") or (custom is not None and host == custom)


# The keys are the gateway path segments of the live insurance APIs (`/insurance/mcp/tech-lookup`,
# `/insurance/mcp/work-order`), which keep their names.
GATEWAY_MCP_CONNECTION_VARIABLES = {
    "tech-lookup": "MCP_GATEWAY_CONNECTION_POLICY_LOOKUP",
    "work-order": "MCP_GATEWAY_CONNECTION_CLAIM_REVIEW",
}


def _gateway_connection_for(url: str) -> tuple[str | None, str | None]:
    """The (connection name, variable name) for a gateway MCP URL ending in /mcp/<kind>.

    Foundry sends an MCP tool's calls to the *target* of the connection it references, so every MCP endpoint
    needs its own connection whose target is exactly its URL. A single shared connection would misroute."""
    try:
        segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    except ValueError:
        return None, None
    kind = segments[-1] if len(segments) >= 2 and segments[-2] == "mcp" else None
    variable = GATEWAY_MCP_CONNECTION_VARIABLES.get(kind or "")
    return (_alias_env(variable) if variable else None), variable


def _knowledge_tools_for_agent(cfg: dict[str, Any], search_index: str | None = None,
                               mcp: GatewayMcp | None = None) -> list[dict[str, Any]]:
    """Translate the yaml `knowledge:` block into MCPTool entries that
    Foundry renders under the agent's Knowledge panel. Each entry becomes
    an MCPTool pointing at a project RemoteTool connection (created by
    scripts/connect-knowledge-base.py) whose target is the KB's MCP
    endpoint, or at the KB's AI Gateway tool server when `mcp` routes it.
    `allowed_tools=[knowledge_base_retrieve]` and
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
        # isn't set, fall back to the isolated insurance knowledge base and
        # its project connection (scripts/publish-insurance-knowledge.py).
        if not kb or (isinstance(kb, str) and kb.startswith("${")):
            kb = os.environ.get("AZURE_SEARCH_KNOWLEDGE_BASE", DEFAULT_KNOWLEDGE_BASE)
        if not conn or (isinstance(conn, str) and conn.startswith("${")):
            conn = os.environ.get("FOUNDRY_KB_CONNECTION_NAME", DEFAULT_KB_CONNECTION)
        if not kb or not conn:
            print(f"  ! knowledge on '{cfg['name']}' missing knowledge_base or connection - skipping.")
            continue
        out.append(_kb_mcp_entry(cfg["name"], kb, conn, mcp))
    # A prompt agent may declare a target Search index with a
    # `type: knowledge` tool block that we translate to the same MCP shape
    # against that index. Simplified: if the yaml has a
    # `tools: [{type: knowledge, index: nw-claims}]` entry, we emit one MCP
    # tool per matching index against the shared Search KB service.
    #
    # Connection naming: Foundry connection names are account-scoped, so
    # every project needs its own uniquely-named connection. Convention:
    # `<kb-name>-mcp` (e.g., `northwind-insurance-kb-mcp`, `nw-claims-kb-mcp`). This
    # matches what scripts/connect-knowledge-base.py creates.
    for t in cfg.get("tools") or []:
        if isinstance(t, dict) and t.get("type") == "knowledge":
            idx = _expand_env(t.get("index")) or search_index
            if not idx:
                print(f"  ! knowledge tool on '{cfg['name']}' has no index - skipping.")
                continue
            # The isolated insurance index keeps its published kb/conn names
            # (AZURE_SEARCH_KNOWLEDGE_BASE / FOUNDRY_KB_CONNECTION_NAME).
            if idx == os.environ.get("AZURE_SEARCH_INDEX", DEFAULT_KNOWLEDGE_INDEX):
                kb = os.environ.get("AZURE_SEARCH_KNOWLEDGE_BASE", DEFAULT_KNOWLEDGE_BASE)
                conn = os.environ.get("FOUNDRY_KB_CONNECTION_NAME", f"{kb}-mcp")
            else:
                kb = f"{idx}-kb"
                conn = f"{kb}-mcp"
            out.append(_kb_mcp_entry(cfg["name"], kb, conn, mcp))
    return out


def _tools_for_agent(cfg: dict[str, Any], search_index: str | None = None,
                     mcp: GatewayMcp | None = None) -> list[dict[str, Any]]:
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
            # Gateway URLs are left untouched: the gateway rewrites them to the backend's `/mcp/` itself.
            gateway = _is_gateway_url(server_url)
            if not gateway and server_url.rstrip("?").endswith("/mcp"):
                server_url = server_url.rstrip("/") + "/"
            if mcp and mcp.required and not _on_gateway(server_url):
                raise ValueError(
                    f"AI_GATEWAY_MCP_REQUIRED is set but the mcp tool on '{cfg['name']}' calls {server_url} "
                    "directly; register it as a gateway tool server or remove the tool."
                )
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
            elif gateway and not _on_gateway(server_url):
                connection, variable = _gateway_connection_for(server_url)
                if connection:
                    mcp_entry["project_connection_id"] = connection
                    print(f"  ~ {cfg['name']}: MCP server is behind the API gateway; using project connection "
                          f"'{connection}' (its target must be exactly {server_url}).")
                else:
                    names = (f"{variable}, or its deprecated alias {ENV_ALIASES[variable]}" if variable
                             else "unrecognised MCP endpoint")
                    print(f"  ! {cfg['name']}: MCP server is behind the API gateway but no project connection is "
                          f"configured ({names}); its calls will be rejected (401).")
            resolved.append(mcp_entry)
        elif kind == "knowledge":
            # Handled in _knowledge_tools_for_agent (below).
            continue
        else:
            resolved.append(t)
    # Knowledge-base MCP entries are represented as MCPTools on the same
    # tools list; Foundry categorizes them into the Knowledge panel based on
    # server_label + allowed_tools.
    resolved.extend(_knowledge_tools_for_agent(cfg, search_index=search_index, mcp=mcp))
    return resolved


def _plan_agent(cfg: dict[str, Any], model: str, search_index: str | None = None,
                gateway: GatewayModels | None = None,
                mcp: GatewayMcp | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Final model string and resolved tools for one prompt-agent yaml (no I/O)."""
    tools = _tools_for_agent(cfg, search_index=search_index, mcp=mcp)
    agent_model = resolve_agent_model(_expand_env(cfg.get("model")) or model, gateway)
    return agent_model, tools


def _tool_route(url: str) -> str:
    """Where an MCP tool's calls go: the AI Gateway, another (API Management) gateway, or straight to the server."""
    if _on_gateway(url):
        return "ai-gateway"
    return "apim" if _is_gateway_url(url) else "direct"


def _tool_summary(tools: list[dict[str, Any]]) -> str:
    if not tools:
        return "none"
    return ", ".join(
        f"{t.get('type')}:{t.get('name') or t.get('server_label') or '-'}"
        + (f"@{_tool_route(t.get('server_url') or '')}" if t.get("type") == "mcp" else "")
        for t in tools
    )


async def _upsert_agent(agents, cfg: dict[str, Any], model: str, search_index: str | None = None,
                        gateway: GatewayModels | None = None, mcp: GatewayMcp | None = None) -> str:
    """Create-or-update a Foundry prompt agent by name. `agents.create_version`
    is upsert-friendly: it creates the agent record on first call and appends
    a new immutable version on subsequent calls.

    A yaml may override the department-default model via a `model:` field,
    so one agent can use a different deployment from the shared default."""
    from azure.ai.projects.models import PromptAgentDefinition

    agent_model, tools = _plan_agent(cfg, model, search_index=search_index, gateway=gateway, mcp=mcp)

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


def _canonical_endpoints(raw: Mapping[str, Any]) -> dict[str, str]:
    """Department-slug -> endpoint with deprecated slugs mapped to canonical ones.
    A canonical key wins over its deprecated alias."""
    canonical: dict[str, str] = {}
    legacy: dict[str, str] = {}
    for key, value in raw.items():
        slug = str(key).strip().lower()
        (legacy if slug in LEGACY_DEPARTMENT_SLUGS else canonical)[LEGACY_DEPARTMENT_SLUGS.get(slug, slug)] = str(value)
    return {**legacy, **canonical}


def _resolve_endpoints() -> dict[str, str]:
    """Return department-slug -> Foundry project endpoint.

    Prefers `FOUNDRY_PROJECT_ENDPOINTS` (JSON emitted by main.bicep). Falls
    back to a single-project map keyed by `claims-concierge` for older envs.
    If neither map is set but `FOUNDRY_ENDPOINT` is available, synthesizes
    per-department endpoints from the manifest's `foundry_project` names —
    this is what lets a local run succeed even when
    the azd Bicep outputs haven't been loaded into the shell.
    """
    raw = os.environ.get("FOUNDRY_PROJECT_ENDPOINTS", "").strip()
    if raw:
        try:
            endpoints = json.loads(raw)
            if not isinstance(endpoints, dict):
                raise ValueError("must be a JSON object")
            return _canonical_endpoints(endpoints)
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
            # The claims-concierge flagship keeps the exact FOUNDRY_PROJECT_ENDPOINT
            # if it was set — preserves any preview-only quirks.
            if single:
                synthesized["claims-concierge"] = single
            print(f"Synthesized {len(synthesized)} project endpoints from "
                  f"FOUNDRY_ENDPOINT + manifest.")
            return synthesized

    if not single:
        raise RuntimeError(
            "Neither FOUNDRY_PROJECT_ENDPOINTS nor FOUNDRY_PROJECT_ENDPOINT is set."
        )
    return {"claims-concierge": single}


def _prompt_yamls_for_department(dept: Department) -> list[Path]:
    """A department's prompt agents live under
    `data/northwind/departments/<slug>/prompts/*.yaml`."""
    return sorted(dept.prompts_dir.glob("*.yaml"))


async def _create_for_department(dept: Department, endpoint: str, model: str,
                                 cred, gateway: GatewayModels | None = None,
                                 dry_run: bool = False,
                                 mcp: GatewayMcp | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    if dept.slug in _HOSTED_DEPARTMENTS:
        print(f"\n[{dept.slug}] Hosted agents are deployed by scripts/deploy-hosted-agents.py; prompt provisioning skipped.")
        return [], []
    yamls = _prompt_yamls_for_department(dept)
    if not yamls:
        print(f"\n[{dept.slug}] no prompt agents found under {dept.prompts_dir}")
        return [], []

    print(f"\n[{dept.slug}] project endpoint = {endpoint}")
    print(f"  {len(yamls)} prompt agents · search_index={dept.search_index}")

    ids: list[str] = []
    failures: list[tuple[str, str]] = []
    if dry_run:
        for path in yamls:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not cfg or "name" not in cfg or "instructions" not in cfg:
                print(f"  x {path.name}: missing 'name' or 'instructions'")
                failures.append((path.name, "invalid yaml"))
                continue
            try:
                agent_model, tools = _plan_agent(
                    cfg, model, search_index=dept.search_index, gateway=gateway, mcp=mcp)
            except Exception as exc:  # noqa: BLE001
                print(f"  x {cfg['name']}: {exc}")
                failures.append((cfg["name"], str(exc)))
                continue
            print(f"  ~ would create or update {cfg['name']} (model={agent_model}; tools={_tool_summary(tools)})")
            ids.append(cfg["name"])
        return ids, failures
    async with AIProjectClient(endpoint=endpoint, credential=cred) as project:
        for path in yamls:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not cfg or "name" not in cfg or "instructions" not in cfg:
                print(f"  ✗ {path.name}: missing 'name' or 'instructions'")
                failures.append((path.name, "invalid yaml"))
                continue
            try:
                agent_id = await _upsert_agent(
                    project.agents, cfg, model, search_index=dept.search_index, gateway=gateway, mcp=mcp,
                )
                ids.append(agent_id)
                print(f"    id = {agent_id}")
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                failures.append((cfg.get("name", path.name), str(exc)))
    return ids, failures


async def main(dry_run: bool = False) -> None:
    try:
        gateway = gateway_models_from_env()
        mcp = gateway_mcp_from_env()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    model = _resolve_model()
    try:
        endpoints = _resolve_endpoints()
    except RuntimeError:
        if not dry_run:
            raise
        endpoints = {}

    company = load_company()

    print(f"Creating Foundry prompt agents across {len(company.departments)} "
          f"department(s) of {company.name}")
    print(f"Model deployment: {model}")
    if gateway:
        print(f"Gateway model connection: {gateway.connection} (provider {gateway.provider}); "
              f"agent models are sent as {gateway.connection}/<alias>")
        print("WARNING: Foundry rejects a model id that contains '/' and the AI Gateway tier needs a "
              "'<provider>/<registration>' alias, so agents created with these models fail at inference "
              "(verified 2026-10-22, docs/ai-gateway.md finding 9). Leave AI_GATEWAY_MODEL_CONNECTION unset.",
              file=sys.stderr)
    if mcp:
        print(f"Gateway MCP: {len(mcp.routes)} knowledge-base route(s)"
              + ("; knowledge bases without a route are an error" if mcp.required else ""))
    if dry_run:
        print("DRY RUN: nothing is sent to Foundry.")

    cred = None if dry_run else _get_credential()

    all_failures: list[tuple[str, str]] = []
    total_ids: list[str] = []

    for dept in company.departments:
        endpoint = endpoints.get(dept.slug) or ("<unresolved>" if dry_run else "")
        if not endpoint:
            print(f"\n[{dept.slug}] SKIPPED — no endpoint in FOUNDRY_PROJECT_ENDPOINTS")
            all_failures.append((dept.slug, "no project endpoint"))
            continue
        ids, failures = await _create_for_department(
            dept, endpoint, model, cred, gateway=gateway, dry_run=dry_run, mcp=mcp,
        )
        total_ids.extend(ids)
        all_failures.extend(failures)

    if all_failures:
        print("\nOne or more agents failed:", all_failures)
        sys.exit(1)
    if dry_run:
        print(f"\nDry run complete: {len(total_ids)} prompt agent(s) planned; nothing was sent to Foundry.")
        return
    print(f"\nAll {len(total_ids)} prompt agents ready (resolve by name).")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or update the Northwind Foundry prompt agents.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print each planned agent (name, final model string, tools); make no Foundry call")
    return parser.parse_args(argv)


if __name__ == "__main__":
    asyncio.run(main(dry_run=_parse_args().dry_run))
