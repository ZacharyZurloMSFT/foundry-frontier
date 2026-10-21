"""Shared insurance deployment contract; contains no prompts or credentials."""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from urllib.parse import urlsplit

INSURANCE_PROJECT = "nw-insurance-claims"
INSURANCE_PROJECTS = {
    "order-concierge": INSURANCE_PROJECT,
    "employee-help-desk": "nw-insurance-support",
    "claims-warranty": "nw-insurance-documents",
    "contact-center": "nw-insurance-contact-center",
}
INSURANCE_CONNECTION = "nw-insurance-claims-kb-mcp"
INSURANCE_KB = "northwind-insurance-kb"
INSURANCE_INDEX = "northwind-insurance-knowledge"
INSURANCE_AGENT_NAMES = {
    "orchestrator": "northwind-insurance-orchestrator",
    "triage": "northwind-insurance-triage",
    "policy_claim_lookup": "northwind-insurance-policy-claim-lookup",
    "policy_wording": "northwind-insurance-policy-wording",
    "claim_action_draft": "northwind-insurance-claim-action-draft",
}
INSURANCE_ACCOUNT_HOST = "aisvc-zurlo-northwind.services.ai.azure.com"
ROLE_ALIASES = {
    "manager": "orchestrator",
    "orderlookup": "policy_claim_lookup",
    "policylookup": "policy_wording",
    "resolutiondraft": "claim_action_draft",
}
ROLE_DIRECTORIES = {
    "orchestrator": "orchestrator",
    "triage": "triage",
    "policy_claim_lookup": "orderlookup",
    "policy_wording": "policylookup",
    "claim_action_draft": "resolutiondraft",
}
ALIASES = {
    "AGENT_ROLE": ("ORDER_AGENT_ROLE",),
    "MODEL_DEPLOYMENT_NAME": ("ORDERS_MODEL_DEPLOYMENT", "AZURE_AI_MODEL_DEPLOYMENT_NAME"),
    "AZURE_OPENAI_ENDPOINT": ("ORDERS_OPENAI_ENDPOINT",),
    "MODEL_GATEWAY_ENDPOINT": ("ORDERS_MODEL_GATEWAY_ENDPOINT",),
    "MODEL_GATEWAY_MODEL": ("ORDERS_MODEL_GATEWAY_MODEL",),
    "MODEL_GATEWAY_DEPLOYMENT": ("ORDERS_MODEL_GATEWAY_DEPLOYMENT",),
    "MODEL_GATEWAY_SCOPE": ("ORDERS_MODEL_GATEWAY_SCOPE",),
    "INSURANCE_KB_CONNECTION_NAME": (
        "ORDERS_KB_CONNECTION", "ORDERS_KB_CONNECTION_NAME", "FOUNDRY_KB_CONNECTION_NAME",
    ),
    "INSURANCE_PROJECT_NAME": ("ORDER_PROJECT_NAME",),
    "INSURANCE_PROJECT_ENDPOINT": ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT"),
    "AGENTS_HOSTED": ("ORDER_AGENTS_HOSTED",),
    "MCP_INSURANCE_LOOKUP_URL": ("MCP_TECH_LOOKUP_URL",),
    "MCP_INSURANCE_REVIEW_URL": ("MCP_WORK_ORDER_URL",),
    "INSURANCE_GATEWAY_ORCHESTRATOR_MODEL": ("ORDERS_GATEWAY_MANAGER_MODEL",),
    "INSURANCE_GATEWAY_SPECIALIST_MODEL": ("ORDERS_GATEWAY_SPECIALIST_MODEL",),
}


def normalize_role(value: str) -> str:
    role = ROLE_ALIASES.get(value, value)
    if role not in ROLE_DIRECTORIES:
        raise ValueError("Unknown AGENT_ROLE")
    return role


def normalize_environment(values: Mapping[str, str], *, warn: bool = True) -> dict[str, str]:
    if any(not isinstance(name, str) or not isinstance(value, str) for name, value in values.items()):
        raise ValueError("Environment setting names and values must be strings")
    result = dict(values)
    for primary, aliases in ALIASES.items():
        supplied = {name: result[name] for name in (primary, *aliases) if name in result}
        if not supplied:
            continue
        normalized = {}
        for name, value in supplied.items():
            if primary == "AGENT_ROLE":
                value = normalize_role(value)
            elif primary.endswith("_ENDPOINT"):
                value = value.rstrip("/")
            normalized[name] = value
        if len(set(normalized.values())) != 1:
            raise ValueError(f"Conflicting settings for {primary}: {', '.join(supplied)}")
        result[primary] = next(iter(normalized.values()))
        for alias in aliases:
            if alias in result:
                del result[alias]
                if warn:
                    logging.getLogger(__name__).warning("Deprecated setting %s; use %s", alias, primary)
    return result


def configuration_hash(values: Mapping[str, str]) -> str:
    normalized = normalize_environment(values, warn=False)
    # A version's runtime identity is supplied by Foundry, not by its source configuration.
    normalized.pop("AZURE_CLIENT_ID", None)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


def insurance_routing() -> dict[str, str]:
    base = f"https://{INSURANCE_ACCOUNT_HOST}/api/projects/"
    return {
        "NORTHWIND_BUSINESS_DOMAIN": "insurance",
        "INSURANCE_PROJECT_NAME": INSURANCE_PROJECT,
        "INSURANCE_PROJECT_ENDPOINT": base + INSURANCE_PROJECT,
        "AGENTS_HOSTED": "true",
        "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID": INSURANCE_AGENT_NAMES["orchestrator"],
        "FOUNDRY_PROJECT_ENDPOINTS": json.dumps(
            {slug: base + project for slug, project in INSURANCE_PROJECTS.items()}, separators=(",", ":"),
        ),
    }


def validate_fleet_receipt(receipt: Mapping, source_commit: str) -> None:
    if (receipt.get("state") != "verified" or receipt.get("source_commit") != source_commit
            or receipt.get("routing_sha256") != configuration_hash(insurance_routing())
            or receipt.get("synthetic") is not True
            or receipt.get("draft_execution_status") != "not_executed"
            or not receipt.get("workflow_id") or not receipt.get("hitl_id")):
        raise ValueError("A matching successful insurance fleet verification receipt is required")
    agents = receipt.get("agents")
    if not isinstance(agents, Mapping) or set(agents) != set(INSURANCE_AGENT_NAMES):
        raise ValueError("Verification must cover all five insurance roles")
    for role, name in INSURANCE_AGENT_NAMES.items():
        entry = agents[role]
        if (not isinstance(entry, Mapping) or entry.get("name") != name
                or not isinstance(entry.get("version"), str) or not entry["version"].isdigit()
                or entry.get("runtime_verified") is not True):
            raise ValueError("Insurance serving version/runtime verification is incomplete")
        for key in ("source_sha256", "config_sha256", "prompt_sha256", "source_manifest_sha256"):
            value = entry.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Insurance verification requires exact artifact/config/prompt hashes")


def valid_project_endpoint(value: str, project: str, *, account_host: str | None = None) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            value == value.strip() and parsed.scheme == "https" and bool(parsed.hostname)
            and parsed.port in (None, 443) and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and parsed.path.rstrip("/") == f"/api/projects/{project}"
            and (account_host is None or parsed.hostname == account_host)
        )
    except ValueError:
        return False


def valid_insurance_routes(endpoints: object, primary: str) -> bool:
    if not isinstance(endpoints, Mapping) or set(endpoints) != set(INSURANCE_PROJECTS):
        return False
    if not valid_project_endpoint(primary, INSURANCE_PROJECT):
        return False
    host = urlsplit(primary).hostname
    return all(
        isinstance(endpoints[slug], str)
        and valid_project_endpoint(endpoints[slug], project, account_host=host)
        for slug, project in INSURANCE_PROJECTS.items()
    ) and endpoints["order-concierge"].rstrip("/") == primary.rstrip("/")


def validate_insurance_connection(connection: Mapping, search_endpoint: str) -> None:
    expected = f"{search_endpoint.rstrip('/')}/knowledgebases/{INSURANCE_KB}/mcp?api-version=2026-05-01-preview"
    properties = connection.get("properties", connection)
    if not isinstance(properties, Mapping):
        raise ValueError("Insurance knowledge connection properties are invalid")
    category = properties.get("category", properties.get("type"))
    if category != "RemoteTool" or properties.get("target") != expected:
        raise ValueError("Insurance knowledge connection must target the canonical insurance RemoteTool endpoint")
