"""Validate reference-only provisioning inputs without Azure or filesystem writes."""

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
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
    "ORDER_PROJECT_NAME",
    "ORDER_AGENTS_HOSTED",
)
FOUNDRY_ACCOUNT_NAME = "aisvc-zurlo-northwind"
AGENT_SERVICES = {
    "orchestrator": "northwind-orchestrator",
    "triage": "northwind-triage",
    "orderlookup": "northwind-order-lookup",
    "policylookup": "northwind-policy-lookup",
    "resolutiondraft": "northwind-resolution-draft",
}
PRESERVED_KEYS = (
    "FOUNDRY_ENDPOINT",
    "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_AI_PROJECT_ENDPOINT",
    "FOUNDRY_PROJECT_ENDPOINTS",
    "ORDERS_OPENAI_ENDPOINT",
    "AZURE_OPENAI_ENDPOINT",
    "ORDERS_KB_CONNECTION_NAME",
    "FOUNDRY_KB_CONNECTION_NAME",
    "FOUNDRY_PLANNER_DEPLOYMENT",
    "FOUNDRY_WORKER_DEPLOYMENT",
    "FOUNDRY_PROMPT_AGENT_DEPLOYMENT",
    "FOUNDRY_EMBEDDING_DEPLOYMENT",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME",
    "FOUNDRY_BAKEOFF_DEPLOYMENTS",
    "AZURE_SEARCH_INDEX",
    "AZURE_SEARCH_KNOWLEDGE_BASE",
    "AZURE_SEARCH_KNOWLEDGE_SOURCE",
    "COSMOS_DATABASE",
    "STORAGE_CU_CONTAINER",
    "STORAGE_SEED_CONTAINER",
)


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


def validate_environment(env: Mapping[str, str], agent_service: str | None = None) -> list[str]:
    mode = env.get("NORTHWIND_REUSE_EXISTING_RESOURCES", "false")
    if mode not in ("true", "false"):
        return ["NORTHWIND_REUSE_EXISTING_RESOURCES must be exactly true or false."]
    ingestion = env.get("NORTHWIND_ENABLE_CU_INGEST", "false")
    if ingestion not in ("true", "false"):
        return ["NORTHWIND_ENABLE_CU_INGEST must be exactly true or false."]
    domain = env.get("NORTHWIND_BUSINESS_DOMAIN", "retail")
    ready = env.get("NORTHWIND_INSURANCE_AGENTS_READY", "false")
    configuration_errors = []
    if domain not in ("retail", "insurance"):
        configuration_errors.append("NORTHWIND_BUSINESS_DOMAIN must be exactly retail or insurance.")
    elif domain == "insurance" and env.get("ORDER_PROJECT_NAME", "northwind-orders") != "northwind-orders":
        configuration_errors.append("Insurance deployment must retain ORDER_PROJECT_NAME=northwind-orders; do not target a legacy project.")
    if ready not in ("true", "false"):
        configuration_errors.append("NORTHWIND_INSURANCE_AGENTS_READY must be exactly true or false.")
    elif ready == "true" and (
        domain != "insurance" or env.get("ORDER_AGENTS_HOSTED", "true") != "true"
    ):
        configuration_errors.append("Insurance readiness requires the insurance domain and ORDER_AGENTS_HOSTED=true.")
    if ready == "true":
        manager_id = env.get("FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID", "")
        if manager_id != manager_id.strip() or not manager_id.startswith("northwind-insurance-"):
            configuration_errors.append(
                "Insurance readiness requires an explicit FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID "
                "with the northwind-insurance- prefix; do not reuse the retail manager ID."
            )
        endpoint = env.get("FOUNDRY_PROJECT_ENDPOINT", "")
        if not is_project_endpoint(endpoint, env.get("ORDER_PROJECT_NAME", "northwind-orders")):
            configuration_errors.append("Insurance readiness requires the deployed Orders project endpoint.")
    if agent_service is not None and domain == "insurance":
        configuration_errors.append(
            "Existing azd agent services retain retail agent.yaml names; insurance deployments "
            "must not overwrite existing retail agents. Use scripts/deploy-orders-hosted.py "
            "--business-domain insurance for the separate native insurance rollout."
        )
    gateway = env.get("NORTHWIND_GATEWAY_URL", "")
    if gateway and not re.fullmatch(r"https://[a-z0-9-]+\.azure-api\.net", gateway):
        configuration_errors.append(
            "NORTHWIND_GATEWAY_URL must be a bare https://<name>.azure-api.net origin with no path, query, or trailing slash."
        )
    if env.get("NORTHWIND_GATEWAY_STACK", "northwind") not in ("northwind", "insurance"):
        configuration_errors.append("NORTHWIND_GATEWAY_STACK must be exactly northwind or insurance.")
    if configuration_errors or mode == "false":
        return configuration_errors

    required = GROUP_KEYS + PRESERVED_KEYS + IDENTITY_KEYS
    missing = [key for key in required if not env.get(key, "").strip()]
    if missing:
        return ["Reuse mode requires existing azd values for: " + ", ".join(missing)]

    errors = []
    if ingestion == "true":
        errors.append("NORTHWIND_ENABLE_CU_INGEST is greenfield-only; reuse never revives ca-cu-ingest.")
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
        f"/projects/{env['ORDER_PROJECT_NAME']}"
    )
    if env["AZURE_AI_PROJECT_ID"].casefold() != expected_project_id.casefold():
        errors.append(
            "AZURE_AI_PROJECT_ID must match AZURE_SUBSCRIPTION_ID, "
            "AZURE_FOUNDRY_RESOURCE_GROUP, the existing Foundry account and ORDER_PROJECT_NAME."
        )
    if "AZURE_COSMOS_PRIVATE_ENDPOINT_NAME" in env:
        name = env["AZURE_COSMOS_PRIVATE_ENDPOINT_NAME"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
            errors.append("AZURE_COSMOS_PRIVATE_ENDPOINT_NAME must be a nonblank resource name.")
    if env["ORDER_AGENTS_HOSTED"] not in ("true", "false"):
        errors.append("ORDER_AGENTS_HOSTED must be exactly true or false.")

    project_endpoint = env["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    if env["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/") != project_endpoint:
        errors.append("The two Foundry project endpoint outputs must agree.")
    if not is_project_endpoint(project_endpoint, env["ORDER_PROJECT_NAME"]):
        errors.append("Foundry project endpoints must resolve ORDER_PROJECT_NAME in the existing Foundry account.")
    try:
        endpoints = json.loads(env["FOUNDRY_PROJECT_ENDPOINTS"])
        departments = (
            "order-concierge",
            "employee-help-desk",
            "claims-warranty",
            "contact-center",
        )
        if not isinstance(endpoints, dict) or any(
            not isinstance(endpoints.get(key), str) or not endpoints[key].strip()
            for key in departments
        ):
            errors.append("FOUNDRY_PROJECT_ENDPOINTS needs all four department endpoints.")
        elif endpoints["order-concierge"].rstrip("/") != project_endpoint:
            errors.append("The Order Concierge endpoint must resolve the Orders project.")
        elif any(not is_project_endpoint(endpoints[key]) for key in departments):
            errors.append("All department endpoints must resolve projects in the existing Foundry account.")
    except json.JSONDecodeError:
        errors.append("FOUNDRY_PROJECT_ENDPOINTS must be a JSON object.")
    return errors


def validate_agent_manifest(agent_service: str, manifest: object) -> list[str]:
    expected = AGENT_SERVICES[agent_service]
    if not isinstance(manifest, dict) or manifest.get("name") != expected:
        return [
            "Existing azd agent manifest must retain the literal retail name " + expected
            + "; unresolved/domain-changing name placeholders are not supported. "
            + "Use deploy-orders-hosted.py --business-domain insurance for separate insurance agents."
        ]
    variables = manifest.get("environment_variables", [])
    if not isinstance(variables, list) or any(not isinstance(item, dict) for item in variables):
        return ["Existing azd agent manifest environment_variables must be a list of named values."]
    for item in variables:
        if item.get("name") == "NORTHWIND_BUSINESS_DOMAIN" and item.get("value") != "retail":
            return ["Existing retail azd agent manifests must declare literal retail domain, not insurance or a placeholder."]
        if item.get("name") == "NORTHWIND_INSURANCE_AGENTS_READY" and item.get("value") not in ("false", False):
            return ["Existing retail azd agent manifests must not enable or infer insurance readiness."]
    return []


def main(agent_service: str | None = None) -> int:
    errors = validate_environment(os.environ, agent_service)
    if not errors and agent_service is not None:
        import yaml

        manifest_path = Path(__file__).resolve().parents[1] / "agents" / agent_service / "agent.yaml"
        errors.extend(validate_agent_manifest(agent_service, yaml.safe_load(manifest_path.read_text(encoding="utf-8"))))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    if os.environ.get("NORTHWIND_REUSE_EXISTING_RESOURCES") == "true":
        print("Reuse inputs validated; provisioning is reference-only and bootstrap is skipped.")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-service", choices=AGENT_SERVICES)
    sys.exit(main(parser.parse_args().agent_service))
