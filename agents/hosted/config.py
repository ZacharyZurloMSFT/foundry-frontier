"""Load the canonical prompts; deployment configuration never contains prompts."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

import yaml
from ..insurance.config import (
    ALIASES, INSURANCE_CONNECTION, INSURANCE_INDEX, INSURANCE_KB,
    INSURANCE_PROJECT, configuration_hash, normalize_environment, valid_project_endpoint,
)

AGENTS_DIR = Path(__file__).resolve().parents[1]
PROMPTS = {
    "orchestrator": "orchestrator/instructions.yaml",
    "triage": "triage.yaml",
    "policy_claim_lookup": "tech_lookup.yaml",
    "policy_wording": "manual_retrieval.yaml",
    "claim_action_draft": "work_order_draft.yaml",
}
SPECIALISTS = {
    "triage": ("northwind-triage", "ORDERS_TRIAGE_AGENT_NAME"),
    "order_lookup": ("northwind-order-lookup", "ORDERS_ORDER_LOOKUP_AGENT_NAME"),
    "policy_lookup": ("northwind-policy-lookup", "ORDERS_POLICY_LOOKUP_AGENT_NAME"),
    "resolution_draft": ("northwind-resolution-draft", "ORDERS_RESOLUTION_DRAFT_AGENT_NAME"),
}
INSURANCE_SPECIALISTS = {
    "triage": ("northwind-insurance-triage", "INSURANCE_TRIAGE_AGENT_NAME"),
    "policy_claim_lookup": ("northwind-insurance-policy-claim-lookup", "INSURANCE_LOOKUP_AGENT_NAME"),
    "policy_lookup": ("northwind-insurance-policy-wording", "INSURANCE_POLICY_AGENT_NAME"),
    "claim_action_draft": ("northwind-insurance-claim-action-draft", "INSURANCE_DRAFT_AGENT_NAME"),
}
INSURANCE_PROMPTS = {
    **{role: "insurance/" + path for role, path in PROMPTS.items() if role != "orchestrator"},
    "orchestrator": "insurance/orchestrator.yaml",
}
INSURANCE_KNOWLEDGE = {
    "AZURE_SEARCH_KNOWLEDGE_BASE": INSURANCE_KB,
    "AZURE_SEARCH_INDEX": INSURANCE_INDEX,
    "INSURANCE_KB_CONNECTION_NAME": INSURANCE_CONNECTION,
}
ENV_KEYS = (
    "AGENT_ROLE", "INSURANCE_PROJECT_ENDPOINT", "MODEL_DEPLOYMENT_NAME",
    "TRIAGE_MODEL_DEPLOYMENT", "AZURE_CLIENT_ID",
    "MCP_INSURANCE_LOOKUP_URL", "MCP_INSURANCE_REVIEW_URL", "AZURE_SEARCH_ENDPOINT",
    "AZURE_SEARCH_INDEX",
    "AZURE_SEARCH_KNOWLEDGE_BASE", "INSURANCE_KB_CONNECTION_NAME",
    "AZURE_OPENAI_ENDPOINT",
    "MODEL_GATEWAY_ENDPOINT", "MODEL_GATEWAY_MODEL",
    "MODEL_GATEWAY_DEPLOYMENT", "MODEL_GATEWAY_SCOPE",
    *(entry[1] for entry in SPECIALISTS.values()),
    *(entry[1] for entry in INSURANCE_SPECIALISTS.values()),
    "NORTHWIND_BUSINESS_DOMAIN", "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED",
)


def https_url(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{name} must be an absolute HTTPS URL without credentials or fragment")
    return value


def source_metadata() -> dict[str, str]:
    path = AGENTS_DIR / "hosted" / "build.json"
    if not path.exists():
        return {}
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != 1 or not manifest.get("files_sha256"):
        raise ValueError("Hosted build manifest is invalid")
    root = AGENTS_DIR.parent
    for name, expected in manifest["files_sha256"].items():
        file = root.joinpath(*name.split("/")).resolve()
        if not file.is_relative_to(root.resolve()) or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError("Hosted source differs from its packaged build manifest")
    return {
        "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "source_commit": manifest.get("source_commit") or "unversioned",
        "source_clean": str(manifest.get("source_clean") is True).lower(),
    }


@dataclass(frozen=True)
class Settings:
    role: str
    project_endpoint: str
    model: str
    model_route: str
    prompt: dict
    prompt_sha256: str
    env: Mapping[str, str]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        normalized = normalize_environment(source)
        env = {key: normalized[key] for key in ENV_KEYS if key in normalized}
        domain = env.get("NORTHWIND_BUSINESS_DOMAIN", "retail")
        if domain not in {"retail", "insurance"}:
            raise ValueError("NORTHWIND_BUSINESS_DOMAIN must be retail or insurance")
        role = env.get("AGENT_ROLE", "orchestrator")
        if role not in PROMPTS:
            raise ValueError(f"Unknown AGENT_ROLE: {role}")
        endpoint = https_url(env.get("INSURANCE_PROJECT_ENDPOINT", ""), "INSURANCE_PROJECT_ENDPOINT")
        if domain == "insurance" and not valid_project_endpoint(endpoint, INSURANCE_PROJECT):
            raise ValueError("Insurance runtime requires the retained nw-insurance-claims project")
        baseline = env.get("MODEL_DEPLOYMENT_NAME", "")
        override = env.get("TRIAGE_MODEL_DEPLOYMENT", "").strip() if role == "triage" else ""
        model = override or baseline.strip()
        if not model:
            raise ValueError("MODEL_DEPLOYMENT_NAME is required "
                             "(or TRIAGE_MODEL_DEPLOYMENT for triage)")
        if domain == "insurance" and (override or "-ft-" in model.lower()):
            if (not model.startswith("northwind-insurance-")
                    or env.get("NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED", "").lower() != "true"):
                raise ValueError("Retail fine-tuned models are not insurance-ready; require a separately evaluated insurance model.")
        gateway = env.get("MODEL_GATEWAY_ENDPOINT", "").strip()
        gateway_model = env.get("MODEL_GATEWAY_MODEL", "").strip()
        gateway_deployment = env.get("MODEL_GATEWAY_DEPLOYMENT", "").strip()
        if gateway:
            https_url(gateway, "MODEL_GATEWAY_ENDPOINT")
            if not gateway.rstrip("/").endswith("/openai/v1"):
                raise ValueError("MODEL_GATEWAY_ENDPOINT must end in /openai/v1/")
            if not gateway_model:
                raise ValueError("MODEL_GATEWAY_MODEL is required for gateway inference")
            if gateway_deployment != model:
                raise ValueError("MODEL_GATEWAY_DEPLOYMENT must match the selected model; "
                                 "a triage override cannot silently reuse a baseline gateway alias")
        elif gateway_model or env.get("MODEL_GATEWAY_SCOPE") or (gateway_deployment and gateway_deployment != model):
            raise ValueError("MODEL_GATEWAY_ENDPOINT is required for gateway configuration")
        raw = (AGENTS_DIR / (INSURANCE_PROMPTS if domain == "insurance" else PROMPTS)[role]).read_bytes()
        prompt = yaml.safe_load(raw)
        if not isinstance(prompt, dict) or not prompt.get("name") or not prompt.get("instructions"):
            raise ValueError("Canonical prompt must declare name and instructions")
        return cls(role, endpoint, model, "triage_override" if override else "baseline",
                   prompt, hashlib.sha256(raw).hexdigest(), env)

    def expand(self, value: str) -> str:
        def replace(match: re.Match) -> str:
            name = match.group(1)
            # Legacy prompt placeholders remain canonical; deployment env is not reserved.
            aliases = {alias: primary for primary, names in ALIASES.items() for alias in names}
            result = self.env.get(aliases.get(name, name), "")
            if not result:
                raise ValueError(f"Missing deployment configuration: {aliases.get(name, name)}")
            return result
        return re.sub(r"\$\{([A-Z0-9_]+)\}", replace, value)

    def mcp_spec(self) -> dict:
        tools = self.prompt.get("tools") or []
        expected = ({"policy_claim_lookup": ["lookup_policy", "lookup_claim"], "claim_action_draft": ["draft_claim_action"]}
                    if self.business_domain == "insurance" else
                    {"policy_claim_lookup": ["lookup_order"], "claim_action_draft": ["propose_resolution"]})[self.role]
        if len(tools) != 1 or tools[0].get("allowed_tools") != expected:
            raise ValueError(f"Canonical {self.role} must allow only {expected}")
        spec = dict(tools[0])
        if spec.get("type") != "mcp" or spec.get("require_approval") != "never":
            raise ValueError("Unsupported MCP configuration; never auto-approve a requested approval")
        spec["server_url"] = https_url(self.expand(spec["server_url"]), "MCP endpoint")
        return spec

    def knowledge_spec(self) -> dict:
        entries = [k for k in self.prompt.get("knowledge", []) if k.get("kind") == "foundry_iq"]
        if len(entries) != 1:
            raise ValueError("Policy lookup requires one canonical Foundry IQ knowledge source")
        entry = entries[0]
        kb = self.expand(entry["knowledge_base"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", kb):
            raise ValueError("Invalid knowledge base name")
        search = https_url(self.env.get("AZURE_SEARCH_ENDPOINT", ""), "AZURE_SEARCH_ENDPOINT")
        connection = self.expand(entry["connection"])
        if self.business_domain == "insurance":
            if (kb != INSURANCE_KNOWLEDGE["AZURE_SEARCH_KNOWLEDGE_BASE"]
                    or self.env.get("AZURE_SEARCH_INDEX") != INSURANCE_KNOWLEDGE["AZURE_SEARCH_INDEX"]
                    or connection.rsplit("/", 1)[-1] != INSURANCE_CONNECTION):
                raise ValueError("Insurance policy wording requires explicit northwind-insurance-kb, "
                                 "northwind-insurance-knowledge and nw-insurance-claims-kb-mcp settings; "
                                 "retail knowledge cannot be substituted.")
        return {
            "name": kb,
            "url": f"{search.rstrip('/')}/knowledgebases/{kb}/mcp?api-version=2026-05-01-preview",
            "project_connection_id": connection,
            "allowed_tools": ["knowledge_base_retrieve"],
            "approval_mode": "never_require",
        }

    @property
    def business_domain(self) -> str:
        return self.env.get("NORTHWIND_BUSINESS_DOMAIN", "retail")

    @property
    def uses_model_gateway(self) -> bool:
        return bool(self.env.get("MODEL_GATEWAY_ENDPOINT", "").strip())

    @property
    def inference_model(self) -> str:
        return self.env["MODEL_GATEWAY_MODEL"].strip() if self.uses_model_gateway else self.model

    @property
    def gateway_scope(self) -> str:
        return self.env.get("MODEL_GATEWAY_SCOPE") or "https://azure-api.net/aigateway/.default"

    @property
    def metadata(self) -> dict[str, str]:
        metadata = {
            "business_domain": self.business_domain,
            "agent_role": self.role,
            "model_deployment": self.model,
            "model_route": self.model_route,
            "model_endpoint_kind": "account_chat_completions" if self.role == "triage" else "project_responses",
            "prompt_sha256": self.prompt_sha256,
            "config_sha256": configuration_hash(self.env),
            **source_metadata(),
        }
        if self.uses_model_gateway:
            metadata.update(
                model_endpoint_kind="gateway_chat_completions" if self.role == "triage" else "gateway_responses",
                model_gateway_endpoint=self.env["MODEL_GATEWAY_ENDPOINT"].rstrip("/"),
                model_gateway_model=self.inference_model,
                model_gateway_auth="entra_id",
            )
        return metadata
