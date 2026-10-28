"""Load the canonical insurance prompts; deployment configuration never contains prompts."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

import yaml

AGENTS_DIR = Path(__file__).resolve().parents[1]
BUSINESS_DOMAIN = "insurance"
# Canonical role ids, mapped to their prompt under agents/.
PROMPTS = {
    "orchestrator": "insurance/orchestrator.yaml",
    "triage": "insurance/triage.yaml",
    "policy-claim-lookup": "insurance/policy_claim_lookup.yaml",
    "policy-wording": "insurance/policy_wording.yaml",
    "claim-action-draft": "insurance/claim_action_draft.yaml",
}
# Deprecated role ids from before the insurance-only cleanup: still accepted, never emitted.
ROLE_ALIASES = {
    "manager": "orchestrator",
    "orderlookup": "policy-claim-lookup",
    "policylookup": "policy-wording",
    "resolutiondraft": "claim-action-draft",
}
# Manager delegate tool name -> (hosted agent name, setting that overrides the name).
SPECIALISTS = {
    "triage": ("northwind-insurance-triage", "INSURANCE_TRIAGE_AGENT_NAME"),
    "policy_claim_lookup": ("northwind-insurance-policy-claim-lookup", "INSURANCE_LOOKUP_AGENT_NAME"),
    "policy_lookup": ("northwind-insurance-policy-wording", "INSURANCE_POLICY_AGENT_NAME"),
    "claim_action_draft": ("northwind-insurance-claim-action-draft", "INSURANCE_DRAFT_AGENT_NAME"),
}
# The live fleet reads its knowledge through the project-derived connection nw-insurance-claims-kb-mcp;
# northwind-insurance-kb-mcp is the original name and stays accepted.
INSURANCE_KB_CONNECTIONS = ("nw-insurance-claims-kb-mcp", "northwind-insurance-kb-mcp")
INSURANCE_KNOWLEDGE = {
    "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-insurance-kb",
    "AZURE_SEARCH_INDEX": "northwind-insurance-knowledge",
    "AGENT_KB_CONNECTION": INSURANCE_KB_CONNECTIONS[0],
}
# Canonical setting -> deprecated names that are still read while the canonical one is unset or blank.
SETTING_ALIASES = {
    "AGENT_ROLE": ("ORDER_AGENT_ROLE",),
    "AGENT_MODEL_DEPLOYMENT": ("ORDERS_MODEL_DEPLOYMENT",),
    "AGENT_OPENAI_ENDPOINT": ("ORDERS_OPENAI_ENDPOINT",),
    "AGENT_MODEL_GATEWAY_ENDPOINT": ("ORDERS_MODEL_GATEWAY_ENDPOINT",),
    "AGENT_MODEL_GATEWAY_MODEL": ("ORDERS_MODEL_GATEWAY_MODEL",),
    "AGENT_MODEL_GATEWAY_DEPLOYMENT": ("ORDERS_MODEL_GATEWAY_DEPLOYMENT",),
    "AGENT_MODEL_GATEWAY_SCOPE": ("ORDERS_MODEL_GATEWAY_SCOPE",),
    "AGENT_MCP_GATEWAY": ("ORDERS_MCP_GATEWAY",),
    "AGENT_KB_CONNECTION": ("ORDERS_KB_CONNECTION",),
    "AGENT_KB_CONNECTION_NAME": ("ORDERS_KB_CONNECTION_NAME",),
    "MCP_POLICY_LOOKUP_URL": ("MCP_TECH_LOOKUP_URL",),
    "MCP_CLAIM_REVIEW_URL": ("MCP_WORK_ORDER_URL",),
}
# Prompt placeholders that may use a deprecated spelling, with the canonical settings that resolve them.
# FOUNDRY_* is platform-reserved, so the knowledge connection placeholder never names a setting itself.
PLACEHOLDER_SETTINGS = {
    "FOUNDRY_KB_CONNECTION_NAME": ("AGENT_KB_CONNECTION", "AGENT_KB_CONNECTION_NAME"),
    "MCP_TECH_LOOKUP_URL": ("MCP_POLICY_LOOKUP_URL",),
    "MCP_WORK_ORDER_URL": ("MCP_CLAIM_REVIEW_URL",),
}
ENV_KEYS = (
    "FOUNDRY_PROJECT_ENDPOINT", "MODEL_DEPLOYMENT_NAME", "AZURE_AI_MODEL_DEPLOYMENT_NAME",
    "TRIAGE_MODEL_DEPLOYMENT", "AZURE_CLIENT_ID", "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX",
    "AZURE_SEARCH_KNOWLEDGE_BASE", "MCP_GATEWAY_CONNECTION", "AI_GATEWAY_REQUIRED", "AI_GATEWAY_MCP_REQUIRED",
    "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED",
    *(entry[1] for entry in SPECIALISTS.values()),
    *SETTING_ALIASES,
    *(name for aliases in SETTING_ALIASES.values() for name in aliases),
)
# Roles whose MCP server may sit behind the API Management gateway (MCP_GATEWAY_CONNECTION), and a Foundry
# connection name's shape.
MCP_GATEWAY_ROLES = ("policy-claim-lookup", "claim-action-draft")
CONNECTION_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
# AI_GATEWAY_MCP_REQUIRED constrains provider-executed and application-bound MCP alike.
AI_GATEWAY_MCP_ROLES = ("policy-claim-lookup", "policy-wording", "claim-action-draft")
MCP_GATEWAY_FIELDS = {"url", "tool_prefix", "connection", "forwards_headers", "workflow_binding"}
GATEWAY_MCP_PATH = re.compile(r"(?:/[^/]+)*/default/toolservers/(?P<server>[A-Za-z0-9][A-Za-z0-9_-]*)/mcp")
GATEWAY_TOOL_PREFIX = re.compile(r"[a-z0-9_]*")
GATEWAY_KNOWLEDGE_SERVER = "northwind-insurance-knowledge"


def https_url(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{name} must be an absolute HTTPS URL without credentials or fragment")
    return value


def env_flag(env: Mapping[str, str], name: str) -> bool:
    # Undefined azd values arrive empty and mean "off"; a typo must not silently disable a control.
    value = env.get(name, "").strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("", "false", "0", "no", "off"):
        return False
    raise ValueError(f"{name} must be true, 1 or yes to enable (false, 0, no, off or empty to disable)")


def expect_type(value, kind: type, message: str):
    if isinstance(value, kind):
        return value
    raise ValueError(message)


def canonical_role(value: str) -> str:
    return ROLE_ALIASES.get(value, value)


def read_environment(source: Mapping[str, str]) -> dict[str, str]:
    """The allowlisted settings, keyed canonically: a deprecated name only fills a canonical one that is
    unset or blank, so an undefined azd value never hides the setting it replaces."""
    env = {key: source[key] for key in ENV_KEYS if key in source}
    for canonical, aliases in SETTING_ALIASES.items():
        for name in (canonical, *aliases):
            if env.get(name, "").strip():
                env[canonical] = env[name]
                break
        for name in aliases:
            env.pop(name, None)
    return env


@dataclass(frozen=True)
class McpGateway:
    """One role's AI gateway tool server. It never carries a credential."""

    url: str
    tool_prefix: str
    connection: str
    forwards_headers: bool = False
    workflow_binding: str | None = None

    @property
    def server(self) -> str:
        return GATEWAY_MCP_PATH.fullmatch(urlsplit(self.url).path).group("server")


def parse_mcp_gateway(raw: str) -> dict[str, McpGateway]:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        document = json.loads(raw)
    except ValueError:
        raise ValueError("AGENT_MCP_GATEWAY must be a JSON object keyed by agent role") from None
    document = expect_type(document, dict, "AGENT_MCP_GATEWAY must be a JSON object keyed by agent role")
    entries = {}
    for key, entry in document.items():
        role = canonical_role(key)
        name = f"AGENT_MCP_GATEWAY[{key}]"
        if role not in AI_GATEWAY_MCP_ROLES:
            raise ValueError(f"AGENT_MCP_GATEWAY supports only {', '.join(AI_GATEWAY_MCP_ROLES)}; got {key!r}")
        if role in entries:
            raise ValueError(f"AGENT_MCP_GATEWAY names the {role} role more than once")
        entry = expect_type(entry, dict, f"{name} must be a JSON object")
        forwards = entry.get("forwards_headers", False)
        binding = entry.get("workflow_binding")
        if role == "claim-action-draft" and binding != "signed":
            raise ValueError(f"{name} requires workflow_binding: signed; AI Gateway does not forward workflow headers")
        if binding is not None and (role != "claim-action-draft" or binding != "signed"):
            raise ValueError(f"{name}.workflow_binding is only supported as signed for claim-action-draft")
        unknown = sorted(set(entry) - MCP_GATEWAY_FIELDS)
        if unknown:
            raise ValueError(f"{name} has unsupported fields: {', '.join(unknown)}")
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"{name}.url is required")
        https_url(url, f"{name}.url")
        parsed = urlsplit(url)
        if parsed.query or not GATEWAY_MCP_PATH.fullmatch(parsed.path):
            raise ValueError(f"{name}.url must be a gateway tool server URL "
                             "(/default/toolservers/<server>/mcp) without a query")
        prefix = entry.get("tool_prefix")
        if not isinstance(prefix, str) or not GATEWAY_TOOL_PREFIX.fullmatch(prefix):
            raise ValueError(f"{name}.tool_prefix is required and must match [a-z0-9_]*")
        connection = entry.get("connection")
        if not isinstance(connection, str) or not re.fullmatch(CONNECTION_NAME, connection):
            raise ValueError(f"{name}.connection must name a project connection")
        entries[role] = McpGateway(url, prefix, connection, expect_type(
            forwards, bool, f"{name}.forwards_headers must be a boolean"), binding)
    return entries


@dataclass(frozen=True)
class Settings:
    role: str
    project_endpoint: str
    model: str
    model_route: str
    prompt: dict
    prompt_sha256: str
    env: Mapping[str, str]
    mcp_gateway: Mapping[str, McpGateway] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = read_environment(os.environ if env is None else env)
        requested = env.get("AGENT_ROLE", "orchestrator")
        role = canonical_role(requested)
        if role not in PROMPTS:
            raise ValueError(f"Unknown AGENT_ROLE: {requested}")
        endpoint = https_url(env.get("FOUNDRY_PROJECT_ENDPOINT", ""), "FOUNDRY_PROJECT_ENDPOINT")
        baseline = (env.get("MODEL_DEPLOYMENT_NAME") or env.get("AGENT_MODEL_DEPLOYMENT")
                    or env.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", ""))
        override = env.get("TRIAGE_MODEL_DEPLOYMENT", "").strip() if role == "triage" else ""
        model = override or baseline.strip()
        if not model:
            raise ValueError("MODEL_DEPLOYMENT_NAME or AGENT_MODEL_DEPLOYMENT is required "
                             "(or TRIAGE_MODEL_DEPLOYMENT for triage)")
        if override or "-ft-" in model.lower():
            if (not model.startswith("northwind-insurance-")
                    or env.get("NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED", "").lower() != "true"):
                raise ValueError("A triage override or fine-tuned model is not insurance-ready; "
                                 "require a separately evaluated northwind-insurance-* model.")
        gateway = env.get("AGENT_MODEL_GATEWAY_ENDPOINT", "").strip()
        gateway_model = env.get("AGENT_MODEL_GATEWAY_MODEL", "").strip()
        gateway_deployment = env.get("AGENT_MODEL_GATEWAY_DEPLOYMENT", "").strip()
        if env_flag(env, "AI_GATEWAY_REQUIRED") and not (gateway and gateway_model and gateway_deployment):
            raise ValueError("AI_GATEWAY_REQUIRED is set: AGENT_MODEL_GATEWAY_ENDPOINT, AGENT_MODEL_GATEWAY_MODEL "
                             "and AGENT_MODEL_GATEWAY_DEPLOYMENT are all required; "
                             "direct model inference is not permitted")
        if gateway:
            https_url(gateway, "AGENT_MODEL_GATEWAY_ENDPOINT")
            if not gateway.rstrip("/").endswith("/openai/v1"):
                raise ValueError("AGENT_MODEL_GATEWAY_ENDPOINT must end in /openai/v1/")
            if not gateway_model:
                raise ValueError("AGENT_MODEL_GATEWAY_MODEL is required for gateway inference")
            if gateway_deployment != model:
                raise ValueError("AGENT_MODEL_GATEWAY_DEPLOYMENT must match the selected model; "
                                 "a triage override cannot silently reuse a baseline gateway alias")
        elif gateway_model or gateway_deployment or env.get("AGENT_MODEL_GATEWAY_SCOPE"):
            raise ValueError("AGENT_MODEL_GATEWAY_ENDPOINT is required for gateway configuration")
        mcp_gateway = parse_mcp_gateway(env.get("AGENT_MCP_GATEWAY", ""))
        if (env_flag(env, "AI_GATEWAY_MCP_REQUIRED") and role in AI_GATEWAY_MCP_ROLES
                and role not in mcp_gateway):
            raise ValueError(f"AI_GATEWAY_MCP_REQUIRED is set but AGENT_MCP_GATEWAY has no {role} entry; "
                             "direct MCP traffic is not permitted")
        mcp_connection = env.get("MCP_GATEWAY_CONNECTION", "").strip()
        if mcp_connection and role in MCP_GATEWAY_ROLES:
            if not re.fullmatch(CONNECTION_NAME, mcp_connection):
                raise ValueError("MCP_GATEWAY_CONNECTION must be a Foundry project connection name")
            if role in mcp_gateway:
                raise ValueError(f"MCP_GATEWAY_CONNECTION (API Management) and AGENT_MCP_GATEWAY (AI Gateway) "
                                 f"both route the {role} MCP server; configure only one")
        raw = (AGENTS_DIR / PROMPTS[role]).read_bytes()
        prompt = yaml.safe_load(raw)
        if not isinstance(prompt, dict) or not prompt.get("name") or not prompt.get("instructions"):
            raise ValueError("Canonical prompt must declare name and instructions")
        settings = cls(role, endpoint, model, "triage_override" if override else "baseline",
                       prompt, hashlib.sha256(raw).hexdigest(), env, mcp_gateway)
        if role in mcp_gateway:
            # A gateway entry the canonical prompt rejects must fail at startup, before any credential is read.
            if role == "policy-wording":
                settings.knowledge_spec()
            else:
                settings.mcp_spec()
        return settings

    def expand(self, value: str) -> str:
        def replace(match: re.Match) -> str:
            candidates = PLACEHOLDER_SETTINGS.get(match.group(1), (match.group(1),))
            result = next((self.env[name] for name in candidates if self.env.get(name)), "")
            if not result:
                raise ValueError(f"Missing deployment configuration: {candidates[0]}")
            return result
        return re.sub(r"\$\{([A-Z0-9_]+)\}", replace, value)

    def mcp_spec(self) -> dict:
        tools = self.prompt.get("tools") or []
        expected = {"policy-claim-lookup": ["lookup_policy", "lookup_claim"],
                    "claim-action-draft": ["draft_claim_action"]}[self.role]
        if len(tools) != 1 or tools[0].get("allowed_tools") != expected:
            raise ValueError(f"Canonical {self.role} must allow only {expected}")
        spec = dict(tools[0])
        if spec.get("type") != "mcp" or spec.get("require_approval") != "never":
            raise ValueError("Unsupported MCP configuration; never auto-approve a requested approval")
        gateway = self.mcp_entry
        if gateway is None:
            spec["server_url"] = https_url(self.expand(spec["server_url"]), "MCP endpoint")
        else:
            # The canonical prompt names the backend's tools; the gateway namespaces them with its prefix.
            spec["server_url"] = gateway.url
            spec["allowed_tools"] = [gateway.tool_prefix + name for name in expected]
        return spec

    def knowledge_spec(self) -> dict:
        entries = [k for k in self.prompt.get("knowledge", []) if k.get("kind") == "foundry_iq"]
        if len(entries) != 1:
            raise ValueError("Policy wording requires one canonical Foundry IQ knowledge source")
        entry = entries[0]
        gateway = self.mcp_entry
        if gateway is not None:
            if gateway.server != GATEWAY_KNOWLEDGE_SERVER:
                raise ValueError(f"Policy wording requires the {GATEWAY_KNOWLEDGE_SERVER} "
                                 "gateway tool server; other knowledge cannot be substituted.")
            return {
                "name": gateway.server,
                "url": gateway.url,
                "allowed_tools": [gateway.tool_prefix + "knowledge_base_retrieve"],
                "approval_mode": "never_require",
            }
        kb = self.expand(entry["knowledge_base"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", kb):
            raise ValueError("Invalid knowledge base name")
        search = https_url(self.env.get("AZURE_SEARCH_ENDPOINT", ""), "AZURE_SEARCH_ENDPOINT")
        connection = self.expand(entry["connection"])
        if (kb != INSURANCE_KNOWLEDGE["AZURE_SEARCH_KNOWLEDGE_BASE"]
                or self.env.get("AZURE_SEARCH_INDEX") != INSURANCE_KNOWLEDGE["AZURE_SEARCH_INDEX"]
                or connection.rsplit("/", 1)[-1] not in INSURANCE_KB_CONNECTIONS):
            raise ValueError("Insurance policy wording requires explicit northwind-insurance-kb, "
                             "northwind-insurance-knowledge and nw-insurance-claims-kb-mcp "
                             "(or northwind-insurance-kb-mcp) settings; "
                             "other knowledge cannot be substituted.")
        return {
            "name": kb,
            "url": f"{search.rstrip('/')}/knowledgebases/{kb}/mcp?api-version=2026-05-01-preview",
            "project_connection_id": connection,
            "allowed_tools": ["knowledge_base_retrieve"],
            "approval_mode": "never_require",
        }

    @property
    def mcp_gateway_connection(self) -> str:
        """Project connection holding the API gateway key; empty unless this role calls a gated MCP server."""
        name = self.env.get("MCP_GATEWAY_CONNECTION", "").strip()
        return name if self.role in MCP_GATEWAY_ROLES else ""

    @property
    def uses_model_gateway(self) -> bool:
        return bool(self.env.get("AGENT_MODEL_GATEWAY_ENDPOINT", "").strip())

    @property
    def model_gateway_required(self) -> bool:
        return env_flag(self.env, "AI_GATEWAY_REQUIRED")

    @property
    def mcp_entry(self) -> McpGateway | None:
        return self.mcp_gateway.get(self.role)

    @property
    def mcp_route(self) -> str:
        if self.mcp_entry is not None:
            return "ai-gateway"
        return "api-management" if self.mcp_gateway_connection else "direct"

    @property
    def mcp_connection(self) -> str | None:
        """Project connection whose credential is read at startup for provider-executed MCP."""
        gateway = self.mcp_entry
        if gateway is not None:
            # The in-process claim-action client authenticates with the agent identity instead.
            return None if self.role == "claim-action-draft" else gateway.connection
        if self.role == "policy-wording" and self.uses_model_gateway:
            return self.knowledge_spec()["project_connection_id"]
        return None

    @property
    def inference_model(self) -> str:
        return self.env["AGENT_MODEL_GATEWAY_MODEL"].strip() if self.uses_model_gateway else self.model

    @property
    def gateway_scope(self) -> str:
        return self.env.get("AGENT_MODEL_GATEWAY_SCOPE") or "https://azure-api.net/aigateway/.default"

    @property
    def metadata(self) -> dict[str, str]:
        metadata = {
            "business_domain": BUSINESS_DOMAIN,
            "agent_role": self.role,
            "model_deployment": self.model,
            "model_route": self.model_route,
            "model_endpoint_kind": "account_chat_completions" if self.role == "triage" else "project_responses",
            "prompt_sha256": self.prompt_sha256,
        }
        if self.uses_model_gateway:
            metadata.update(
                model_endpoint_kind="gateway_chat_completions" if self.role == "triage" else "gateway_responses",
                model_gateway_endpoint=self.env["AGENT_MODEL_GATEWAY_ENDPOINT"].rstrip("/"),
                model_gateway_model=self.inference_model,
                model_gateway_auth="entra_id",
            )
        if self.model_gateway_required:
            metadata["model_gateway_required"] = "true"
        if self.role in AI_GATEWAY_MCP_ROLES:
            metadata["mcp_route"] = self.mcp_route
        return metadata
