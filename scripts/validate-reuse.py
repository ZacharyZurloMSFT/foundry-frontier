"""Validate reference-only provisioning inputs without Azure or filesystem writes."""

import json
import os
import re
import sys
from collections.abc import Mapping
from urllib.parse import urlparse


GROUP_KEYS = (
    "AZURE_APPS_RESOURCE_GROUP",
    "AZURE_DATA_RESOURCE_GROUP",
    "AZURE_NETWORKING_RESOURCE_GROUP",
    "AZURE_AI_RESOURCE_GROUP",
    "AZURE_FOUNDRY_RESOURCE_GROUP",
    "AZURE_OBSERVABILITY_RESOURCE_GROUP",
)
OPTIONAL_GROUP_KEYS = (
    "AZURE_LOG_ANALYTICS_RESOURCE_GROUP",
    "AZURE_CONTAINER_APPS_ENVIRONMENT_RESOURCE_GROUP",
    "AZURE_VNET_RESOURCE_GROUP",
)
IDENTITY_KEYS = (
    "AZURE_RESOURCE_GROUP",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_AI_PROJECT_ID",
    "INSURANCE_PROJECT_NAME",
    "HOSTED_AGENTS_ENABLED",
)
FOUNDRY_ACCOUNT_NAME = "aisvc-zurlo-northwind"
INSURANCE_PROJECT_NAME = "nw-insurance-claims"  # the live flagship project and the default for new environments
# The original native project stays valid so azd environments created before the live names landed keep passing.
INSURANCE_PROJECT_NAMES = (INSURANCE_PROJECT_NAME, "northwind-orders")
# Canonical setting -> deprecated alias that azd environments created before the insurance-only cleanup still carry.
# The canonical name wins; the alias is only a fallback and is never written.
DEPRECATED_ALIASES = {
    "INSURANCE_PROJECT_NAME": "ORDER_PROJECT_NAME",
    "HOSTED_AGENTS_ENABLED": "ORDER_AGENTS_HOSTED",
    "AGENT_OPENAI_ENDPOINT": "ORDERS_OPENAI_ENDPOINT",
    "AGENT_KB_CONNECTION_NAME": "ORDERS_KB_CONNECTION_NAME",
    "AGENT_MODEL_GATEWAY_ENDPOINT": "ORDERS_MODEL_GATEWAY_ENDPOINT",
    "AGENT_MODEL_GATEWAY_MODEL": "ORDERS_MODEL_GATEWAY_MODEL",
    "AGENT_GATEWAY_MANAGER_MODEL": "ORDERS_GATEWAY_MANAGER_MODEL",
    "AGENT_GATEWAY_SPECIALIST_MODEL": "ORDERS_GATEWAY_SPECIALIST_MODEL",
    "AGENT_MCP_GATEWAY": "ORDERS_MCP_GATEWAY",
}
# Department slug -> deprecated slug that an existing FOUNDRY_PROJECT_ENDPOINTS value may still use.
DEPARTMENTS = {
    "claims-concierge": "order-concierge",
    "adjuster-support": "employee-help-desk",
    "claims-documents": "claims-warranty",
    "contact-center": None,
}
PRESERVED_KEYS = (
    "FOUNDRY_ENDPOINT",
    "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_AI_PROJECT_ENDPOINT",
    "FOUNDRY_PROJECT_ENDPOINTS",
    "AGENT_OPENAI_ENDPOINT",
    "AZURE_OPENAI_ENDPOINT",
    "AGENT_KB_CONNECTION_NAME",
    "FOUNDRY_KB_CONNECTION_NAME",
    "FOUNDRY_PLANNER_DEPLOYMENT",
    "FOUNDRY_WORKER_DEPLOYMENT",
    "FOUNDRY_PROMPT_AGENT_DEPLOYMENT",
    "FOUNDRY_EMBEDDING_DEPLOYMENT",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME",
    "COSMOS_DATABASE",
    "STORAGE_CU_CONTAINER",
    "STORAGE_SEED_CONTAINER",
)


def setting(env: Mapping[str, str], name: str, default: str = "") -> str:
    """Canonical name first, its deprecated alias second, `default` only when neither is set.

    A blank canonical value falls back to a non-blank alias; when everything that is set is blank the blank value is
    returned so that it is still reported instead of silently replaced by the default.
    """
    names = [name, *([DEPRECATED_ALIASES[name]] if name in DEPRECATED_ALIASES else [])]
    present = [env[candidate] for candidate in names if candidate in env]
    for value in present:
        if value.strip():
            return value
    return present[0] if present else default


def is_project_endpoint(value: str, project_name: str | None = None) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    path = parsed.path.rstrip("/")
    prefix = "/api/projects/"
    return (
        value == value.strip()
        and parsed.scheme == "https"
        and parsed.netloc.casefold() == FOUNDRY_ACCOUNT_NAME + ".services.ai.azure.com"
        and not parsed.query
        and not parsed.fragment
        and (
            path == prefix + project_name if project_name is not None
            else path.startswith(prefix) and "/" not in path[len(prefix):]
            and bool(path[len(prefix):])
        )
    )


def validate_reuse_environment(env: Mapping[str, str]) -> list[str]:
    mode = env.get("NORTHWIND_REUSE_EXISTING_RESOURCES", "false")
    if mode not in ("true", "false"):
        return ["NORTHWIND_REUSE_EXISTING_RESOURCES must be exactly true or false."]
    ready = env.get("NORTHWIND_INSURANCE_AGENTS_READY", "false")
    project_name = setting(env, "INSURANCE_PROJECT_NAME", INSURANCE_PROJECT_NAME)
    configuration_errors = []
    if mode == "true" and project_name not in INSURANCE_PROJECT_NAMES:
        configuration_errors.append(
            "Reuse mode must retain INSURANCE_PROJECT_NAME=" + " or ".join(INSURANCE_PROJECT_NAMES)
            + "; do not target a legacy project."
        )
    if ready not in ("true", "false"):
        configuration_errors.append("NORTHWIND_INSURANCE_AGENTS_READY must be exactly true or false.")
    elif ready == "true" and setting(env, "HOSTED_AGENTS_ENABLED", "true") != "true":
        configuration_errors.append("Insurance readiness requires HOSTED_AGENTS_ENABLED=true.")
    if ready == "true":
        manager_id = env.get("FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID", "")
        if manager_id != manager_id.strip() or not manager_id.startswith("northwind-insurance-"):
            configuration_errors.append(
                "Insurance readiness requires an explicit FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID "
                "with the northwind-insurance- prefix."
            )
        endpoint = env.get("FOUNDRY_PROJECT_ENDPOINT", "")
        if not is_project_endpoint(endpoint, project_name):
            configuration_errors.append("Insurance readiness requires the deployed insurance project endpoint.")
    gateway = env.get("NORTHWIND_GATEWAY_URL", "")
    if gateway and not re.fullmatch(r"https://[a-z0-9-]+\.azure-api\.net", gateway):
        configuration_errors.append(
            "NORTHWIND_GATEWAY_URL must be a bare https://<name>.azure-api.net origin with no path, query, or trailing slash."
        )
    if env.get("NORTHWIND_GATEWAY_STACK", "insurance") != "insurance":
        configuration_errors.append(
            "NORTHWIND_GATEWAY_STACK must be exactly insurance; the northwind stack is no longer managed by this repository."
        )
    if configuration_errors or mode == "false":
        return configuration_errors

    required = GROUP_KEYS + PRESERVED_KEYS + IDENTITY_KEYS
    missing = [key for key in required if not setting(env, key).strip()]
    if missing:
        return ["Reuse mode requires existing azd values for: " + ", ".join(missing)]

    errors = []
    for key in GROUP_KEYS + ("AZURE_RESOURCE_GROUP", "AZURE_LEGACY_RESOURCE_GROUP") + OPTIONAL_GROUP_KEYS:
        if key not in env:
            continue
        value = env[key]
        # The deprecated label may be empty, but never selects a live resource.
        if key == "AZURE_LEGACY_RESOURCE_GROUP" and value == "":
            continue
        if not value.strip():
            errors.append(key + " cannot be blank when set.")
        elif not re.fullmatch(r"[\w().-]{1,90}", value) or value.endswith("."):
            errors.append(key + " must be an Azure resource group name without surrounding whitespace.")
    if env["AZURE_RESOURCE_GROUP"].casefold() != env["AZURE_FOUNDRY_RESOURCE_GROUP"].casefold():
        errors.append(
            "AZURE_RESOURCE_GROUP must agree with AZURE_FOUNDRY_RESOURCE_GROUP "
            "as azd's agent target; do not route Foundry to Apps or the legacy group."
        )
    subscription_id = env["AZURE_SUBSCRIPTION_ID"]
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", subscription_id):
        errors.append("AZURE_SUBSCRIPTION_ID must be a subscription GUID.")
    expected_project_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{env['AZURE_FOUNDRY_RESOURCE_GROUP']}"
        f"/providers/Microsoft.CognitiveServices/accounts/{FOUNDRY_ACCOUNT_NAME}"
        f"/projects/{project_name}"
    )
    if env["AZURE_AI_PROJECT_ID"].casefold() != expected_project_id.casefold():
        errors.append(
            "AZURE_AI_PROJECT_ID must match AZURE_SUBSCRIPTION_ID, "
            "AZURE_FOUNDRY_RESOURCE_GROUP, the existing Foundry account and INSURANCE_PROJECT_NAME."
        )
    if "AZURE_COSMOS_PRIVATE_ENDPOINT_NAME" in env:
        name = env["AZURE_COSMOS_PRIVATE_ENDPOINT_NAME"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
            errors.append("AZURE_COSMOS_PRIVATE_ENDPOINT_NAME must be a nonblank resource name.")
    if setting(env, "HOSTED_AGENTS_ENABLED") not in ("true", "false"):
        errors.append("HOSTED_AGENTS_ENABLED must be exactly true or false.")

    project_endpoint = env["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    if env["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/") != project_endpoint:
        errors.append("The two Foundry project endpoint outputs must agree.")
    if not is_project_endpoint(project_endpoint, project_name):
        errors.append("Foundry project endpoints must resolve INSURANCE_PROJECT_NAME in the existing Foundry account.")
    try:
        endpoints = json.loads(env["FOUNDRY_PROJECT_ENDPOINTS"])
        resolved = (
            {slug: department_endpoint(endpoints, slug) for slug in DEPARTMENTS} if isinstance(endpoints, dict) else {}
        )
        if not resolved or any(value is None for value in resolved.values()):
            errors.append("FOUNDRY_PROJECT_ENDPOINTS needs all four department endpoints.")
        elif resolved["claims-concierge"].rstrip("/") != project_endpoint:
            errors.append("The claims-concierge endpoint must resolve the insurance project.")
        elif any(not is_project_endpoint(value) for value in resolved.values()):
            errors.append("All department endpoints must resolve projects in the existing Foundry account.")
    except json.JSONDecodeError:
        errors.append("FOUNDRY_PROJECT_ENDPOINTS must be a JSON object.")
    return errors


def department_endpoint(endpoints: Mapping[str, object], slug: str) -> str | None:
    """The endpoint of a department under its slug, or under the deprecated slug an older value still uses."""
    for key in (slug, DEPARTMENTS[slug]):
        value = endpoints.get(key) if key else None
        if isinstance(value, str) and value.strip():
            return value
    return None


GATEWAY_ALIAS_KEYS = ("AGENT_GATEWAY_MANAGER_MODEL", "AGENT_GATEWAY_SPECIALIST_MODEL")
# A gateway model alias is <provider>/<registration>; the registration is a lowercase [a-z0-9-] name.
GATEWAY_MODEL_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[a-z0-9][a-z0-9-]*")
GATEWAY_TOOLSERVER_PATH = re.compile(r"(?:/[^/]+)*/default/toolservers/[A-Za-z0-9][A-Za-z0-9_-]*/mcp")


def _gateway_flag(env: Mapping[str, str], name: str, errors: list[str]) -> bool:
    value = str(env.get(name) or "").strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value not in ("", "false", "0", "no", "off"):
        # A typo must not silently disable a gateway control.
        errors.append(f"{name} must be true, 1 or yes to enable (false, 0, no, off or empty to disable).")
    return False


def _plain_https_url(value: object):
    if not isinstance(value, str) or value != value.strip():
        return None
    try:
        parsed = urlparse(value)
        credentials = parsed.username or parsed.password
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or credentials or parsed.query or parsed.fragment:
        return None
    return parsed


def _validate_mcp_gateway(raw: str) -> list[str]:
    try:
        routes = json.loads(raw)
    except json.JSONDecodeError:
        routes = None
    if not isinstance(routes, dict) or not routes:
        return ["AI_GATEWAY_MCP_REQUIRED needs AGENT_MCP_GATEWAY to be a JSON object keyed by agent role."]
    errors = []
    for role, entry in routes.items():
        parsed = _plain_https_url(entry.get("url") if isinstance(entry, dict) else None)
        if parsed is None or not GATEWAY_TOOLSERVER_PATH.fullmatch(parsed.path):
            errors.append(
                f"AGENT_MCP_GATEWAY[{role}].url must be an HTTPS gateway tool server URL "
                "(.../default/toolservers/<server>/mcp) without credentials, query or fragment."
            )
    return errors


def validate_gateway(env: Mapping[str, str]) -> list[str]:
    """AI Gateway consistency of the azd environment.

    A no-op unless AI_GATEWAY_REQUIRED (models) or AI_GATEWAY_MCP_REQUIRED (hosted MCP) is enabled,
    so a fresh `azd up` is unaffected. The AGENT_* settings are read canonical name first, then the
    deprecated ORDERS_* alias; role keys of AGENT_MCP_GATEWAY may use either spelling.
    """
    errors: list[str] = []
    models_required = _gateway_flag(env, "AI_GATEWAY_REQUIRED", errors)
    mcp_required = _gateway_flag(env, "AI_GATEWAY_MCP_REQUIRED", errors)
    if models_required:
        parsed = _plain_https_url(setting(env, "AGENT_MODEL_GATEWAY_ENDPOINT"))
        if parsed is None or not parsed.path.endswith("/openai/v1/"):
            errors.append(
                "AI_GATEWAY_REQUIRED needs AGENT_MODEL_GATEWAY_ENDPOINT to be an HTTPS URL ending in /openai/v1/ "
                "without credentials, query or fragment."
            )
        for key in GATEWAY_ALIAS_KEYS:
            if not GATEWAY_MODEL_ALIAS.fullmatch(setting(env, key)):
                errors.append(
                    f"AI_GATEWAY_REQUIRED needs {key} to be a gateway model alias <provider>/<registration>, "
                    "such as aisvc-zurlo-northwind/gpt-4o."
                )
        alias = setting(env, "AGENT_MODEL_GATEWAY_MODEL")
        if alias and not GATEWAY_MODEL_ALIAS.fullmatch(alias):
            errors.append("AGENT_MODEL_GATEWAY_MODEL must be a gateway model alias <provider>/<registration>.")
    if mcp_required:
        errors.extend(_validate_mcp_gateway(setting(env, "AGENT_MCP_GATEWAY")))
    return errors


def validate_environment(env: Mapping[str, str]) -> list[str]:
    return validate_reuse_environment(env) + validate_gateway(env)


def main() -> int:
    errors = validate_environment(os.environ)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    if os.environ.get("NORTHWIND_REUSE_EXISTING_RESOURCES") == "true":
        print("Reuse inputs validated; provisioning is reference-only.")
        env = os.environ
        print(
            f"Agent target: {env['AZURE_FOUNDRY_RESOURCE_GROUP']}; "
            f"existing app deploy target: {env['AZURE_APPS_RESOURCE_GROUP']}."
        )
        print(
            "Read-only selections: environment "
            + env.get("AZURE_CONTAINER_APPS_ENVIRONMENT_RESOURCE_GROUP", env["AZURE_APPS_RESOURCE_GROUP"])
            + "; VNet "
            + env.get("AZURE_VNET_RESOURCE_GROUP", env["AZURE_NETWORKING_RESOURCE_GROUP"])
            + "; Cosmos private endpoint "
            + env.get("AZURE_COSMOS_PRIVATE_ENDPOINT_NAME", "pe-zz-cosmos-northwind")
            + "; Log Analytics "
            + env.get("AZURE_LOG_ANALYTICS_RESOURCE_GROUP", env["AZURE_APPS_RESOURCE_GROUP"])
            + "."
        )
        print("Selections do not migrate app attachments or network dependencies; inspect live resource outputs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
