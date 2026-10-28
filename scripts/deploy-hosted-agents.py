"""Deploy one Northwind Insurance hosted agent from an allowlisted source ZIP."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentEndpointConfig,
    CodeConfiguration,
    FixedRatioVersionSelectionRule,
    HostedAgentDefinition,
    ProtocolConfiguration,
    ProtocolVersionRecord,
    ResponsesProtocolConfiguration,
    VersionSelector,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential, AzureDeveloperCliCredential, get_bearer_token_provider
import agent_deploy_journal as journal
from hosted_agent_package import PACKAGE_SOURCES as PACKAGE_SOURCES, bundle

# Canonical role id -> Foundry agent name.
ROLES = {
    "triage": "northwind-insurance-triage",
    "policy-claim-lookup": "northwind-insurance-policy-claim-lookup",
    "policy-wording": "northwind-insurance-policy-wording",
    "claim-action-draft": "northwind-insurance-claim-action-draft",
    "orchestrator": "northwind-insurance-orchestrator",
}
# Deprecated role ids: still accepted by --role, as per-role --config sections and as AGENT_MCP_GATEWAY keys.
ROLE_ALIASES = {
    "manager": "orchestrator",
    "orderlookup": "policy-claim-lookup",
    "policylookup": "policy-wording",
    "resolutiondraft": "claim-action-draft",
}
# Canonical setting -> deprecated names; the same table as agents/hosted/config.py::SETTING_ALIASES.
# A deprecated name only fills a canonical setting that is unset or blank and is never sent to the agent.
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
FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
AGENT_CONSUMER = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"
MODELS_USER = "a6b68e3d-a1f0-4c60-8678-237c050cf4ea"
TOOLS_USER = "dce03f8d-857f-4e40-a90d-67770fa6ecd2"
GATEWAY_RESOURCE_GROUP = "rg-northwind-ai"
GATEWAY_NAME = "ai-gateway-northwind"
MCP_GATEWAY_ROLES = ("policy-claim-lookup", "policy-wording", "claim-action-draft")
# The live insurance fleet runs in nw-insurance-claims; northwind-orders is the original native project.
INSURANCE_PROJECTS = ("northwind-orders", "nw-insurance-claims")
# The live knowledge connection is project-derived; the original connection name stays accepted.
INSURANCE_KB_CONNECTIONS = ("nw-insurance-claims-kb-mcp", "northwind-insurance-kb-mcp")
GATEWAY_MODEL_SETTINGS = ("AGENT_MODEL_GATEWAY_ENDPOINT", "AGENT_MODEL_GATEWAY_MODEL",
                          "AGENT_MODEL_GATEWAY_DEPLOYMENT")


def canonical_role(value: str) -> str:
    role = ROLE_ALIASES.get(value, value)
    if role not in ROLES:
        raise ValueError(f"Unknown role {value!r}; expected one of {', '.join(ROLES)}")
    return role


def role_section(configuration: dict, role: str) -> dict:
    """The role's section of the --config file, keyed by its canonical or a deprecated role id."""
    names = [name for name in (role, *(legacy for legacy, target in ROLE_ALIASES.items() if target == role))
             if name in configuration]
    if len(names) > 1:
        raise ValueError(f"Config has more than one section for the {role} role: {', '.join(names)}")
    return configuration[names[0]] if names else {}


def canonical_environment(environment: dict) -> dict:
    """Key the per-role settings canonically; a deprecated name only fills a canonical one that is unset or blank."""
    result = dict(environment)
    for canonical, aliases in SETTING_ALIASES.items():
        for name in (canonical, *aliases):
            if str(result.get(name) or "").strip():
                result[canonical] = result[name]
                break
        for name in aliases:
            result.pop(name, None)
    return result


def setting(environment, name: str) -> str:
    """A trimmed setting value by canonical name, falling back to its deprecated names."""
    for candidate in (name, *SETTING_ALIASES.get(name, ())):
        value = str(environment.get(candidate) or "").strip()
        if value:
            return value
    return ""


def deployment_credential(source: str, tenant: str):
    """Select one cached CLI identity explicitly; never try another principal."""
    if source == "cli":
        return AzureCliCredential(tenant_id=tenant, process_timeout=120)
    if source == "azd":
        return AzureDeveloperCliCredential(tenant_id=tenant, process_timeout=120)
    raise ValueError("credential source must be cli or azd")


def env_flag(environment, name: str) -> bool:
    # A typo must not silently disable a gateway control.
    value = str(environment.get(name) or "").strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("", "false", "0", "no", "off"):
        return False
    raise ValueError(f"{name} must be true, 1 or yes to enable (false, 0, no, off or empty to disable)")


def gateway_scope(subscription: str) -> str:
    return (f"/subscriptions/{subscription}/resourceGroups/{GATEWAY_RESOURCE_GROUP}"
            f"/providers/Microsoft.ApiManagement/service/{GATEWAY_NAME}")


def validate_gateway_environment(environment, role: str) -> None:
    """Fail before any network call when the config cannot satisfy its own gateway requirements."""
    role = ROLE_ALIASES.get(role, role)
    if env_flag(environment, "AI_GATEWAY_REQUIRED"):
        missing = [name for name in GATEWAY_MODEL_SETTINGS if not setting(environment, name)]
        if missing:
            raise ValueError("AI_GATEWAY_REQUIRED is set: " + ", ".join(missing)
                             + " must be configured; direct model inference is not permitted")
    if env_flag(environment, "AI_GATEWAY_MCP_REQUIRED") and role in MCP_GATEWAY_ROLES:
        raw = environment.get("AGENT_MCP_GATEWAY") or environment.get("ORDERS_MCP_GATEWAY")
        try:
            routes = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            routes = None
        if not isinstance(routes, dict):
            raise ValueError("AI_GATEWAY_MCP_REQUIRED is set: AGENT_MCP_GATEWAY must be a JSON object keyed by agent role")
        entries = {ROLE_ALIASES.get(key, key): value for key, value in routes.items()}
        if len(entries) != len(routes):
            raise ValueError("AGENT_MCP_GATEWAY names an agent role more than once")
        entry = entries.get(role)
        if not isinstance(entry, dict) or not str(entry.get("url") or "").strip():
            raise ValueError(f"AI_GATEWAY_MCP_REQUIRED is set but AGENT_MCP_GATEWAY has no {role} entry with a url; "
                             "direct MCP traffic is not permitted")
        if role == "claim-action-draft" and entry.get("workflow_binding") != "signed":
            raise ValueError("Gateway claim-action-draft requires workflow_binding: signed")


def runtime_grants(environment, role: str, project_arm_id: str, account_scope: str) -> list[tuple[str, str]]:
    """(scope, role id) pairs granted to the hosted agent's runtime principal.

    Default: Cognitive Services OpenAI User on the account, Foundry User on the project and, for
    the orchestrator, Agent Consumer. With AI_GATEWAY_REQUIRED, AI Gateway Models User on the gateway
    replaces the OpenAI User grant. Provider-executed MCP authenticates with the runtime key
    of the tool server connection. Only the application-bound draft wrapper
    uses Entra and needs AI Gateway Tools User (gateway-wide; keep unsafe servers blocked).
    """
    models_via_gateway = env_flag(environment, "AI_GATEWAY_REQUIRED")
    gateway = None
    draft_gateway = (ROLE_ALIASES.get(role, role) == "claim-action-draft"
                     and bool(setting(environment, "AGENT_MCP_GATEWAY")))
    if models_via_gateway or draft_gateway:
        subscription = re.match(r"/subscriptions/([^/]+)/", account_scope, re.IGNORECASE)
        if subscription is None:
            raise ValueError("Cannot derive the gateway subscription from the Foundry account scope.")
        gateway = gateway_scope(subscription.group(1))
    grants = [(gateway, MODELS_USER) if models_via_gateway else (account_scope, OPENAI_USER),
              (project_arm_id, FOUNDRY_USER)]
    if draft_gateway:
        routes = json.loads(setting(environment, "AGENT_MCP_GATEWAY"))
        if any(ROLE_ALIASES.get(key, key) == "claim-action-draft" for key in routes):
            grants.append((gateway, TOOLS_USER))
    if ROLE_ALIASES.get(role, role) == "orchestrator":
        grants.append((project_arm_id, AGENT_CONSUMER))
    return grants


def grant(http: httpx.Client, token_provider, subscription: str, scope: str,
          principal: str, role: str) -> None:
    role_id = f"/subscriptions/{subscription}/providers/Microsoft.Authorization/roleDefinitions/{role}"
    assignment = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.lower()}|{principal}|{role}"))
    url = f"https://management.azure.com{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment}"
    response = http.put(
        url, params={"api-version": "2022-04-01"},
        headers={"Authorization": f"Bearer {token_provider()}"},
        json={"properties": {"principalId": principal, "principalType": "ServicePrincipal",
                             "roleDefinitionId": role_id}},
    )
    if response.status_code == 409 and response.json().get("error", {}).get("code") == "RoleAssignmentExists":
        print(json.dumps({"permission": role, "state": "already assigned"}), flush=True)
        return
    response.raise_for_status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=(*ROLES, *ROLE_ALIASES),
                        help="Canonical role id; the deprecated ids manager, orderlookup, policylookup and "
                             "resolutiondraft are accepted as aliases")
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--project-arm-id", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--credential-source", choices=("cli", "azd"), default="cli",
                        help="Explicit cached identity: Azure CLI (default) or Azure Developer CLI; no fallback")
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path, help="Non-secret per-role environment JSON")
    parser.add_argument("--workflow-signing-key-env",
                        help="Environment variable holding the draft runtime private PEM; never put it in config JSON")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--stage-only", action="store_true",
                        help="Create and grant the version without changing the current agent endpoint selector")
    parser.add_argument("--grant-runtime-roles", action="store_true",
                        help="Explicit opt-in to RBAC writes: OpenAI User on the account by default; "
                             "AI_GATEWAY_REQUIRED in the config swaps it for AI Gateway Models User on the "
                             "gateway; existing roles are otherwise never changed")
    args = parser.parse_args()
    with journal.receipt_lock(args.output):
        deploy(args)


def deploy(args):
    root = Path(__file__).resolve().parents[1]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {}
    if not args.apply and previous.get("submission_started"):
        raise ValueError("Dry run must not overwrite a submitted deployment receipt")
    archive_path = args.output.with_suffix(".zip")
    pending_archive = args.output.with_suffix(".pending.zip")
    try:
        source_hash = bundle(root, args.requirements, pending_archive)
        pending_archive.replace(archive_path)
    finally:
        if pending_archive.exists():
            pending_archive.unlink()
    role = canonical_role(args.role)
    configuration = json.loads(args.config.read_text(encoding="utf-8"))
    environment = canonical_environment({**configuration.get("common", {}), **role_section(configuration, role)})
    environment["AGENT_ROLE"] = role
    if "CLAIM_WORKFLOW_SIGNING_KEY" in environment:
        raise ValueError("Private signing keys must not be stored in deployment config JSON")
    # The domain flag was removed: the runtime is insurance-only, so a stale insurance value is dropped.
    if str(environment.pop("NORTHWIND_BUSINESS_DOMAIN", "") or "").strip().lower() not in ("", "insurance"):
        raise ValueError("NORTHWIND_BUSINESS_DOMAIN was removed; only the insurance fleet can be deployed")
    if environment.get("NORTHWIND_INSURANCE_AGENTS_READY", "false").lower() == "true":
        raise ValueError("An agent deployment cannot declare API fleet readiness; verify every specialist first.")
    if any(name.startswith("FOUNDRY_") for name in environment):
        raise ValueError("FOUNDRY_* variables are platform-reserved")
    if not environment.get("AGENT_MODEL_DEPLOYMENT"):
        raise ValueError("AGENT_MODEL_DEPLOYMENT is required")
    validate_gateway_environment(environment, role)
    name = ROLES[role]
    endpoint = urlsplit(args.project_endpoint)
    project_name = endpoint.path.rstrip("/").rsplit("/", 1)[-1]
    if (endpoint.scheme != "https" or project_name not in INSURANCE_PROJECTS
            or endpoint.path.rstrip("/") != f"/api/projects/{project_name}"
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise ValueError("Insurance deployment must explicitly target an existing native insurance project "
                         f"({', '.join(INSURANCE_PROJECTS)}), not the legacy web project.")
    if args.apply and not args.project_arm_id.rstrip("/").endswith(f"/projects/{project_name}"):
        raise ValueError(f"Insurance project ARM ID must match the existing {project_name} project.")
    model = environment.get("TRIAGE_MODEL_DEPLOYMENT") or environment["AGENT_MODEL_DEPLOYMENT"]
    if (environment.get("TRIAGE_MODEL_DEPLOYMENT") or "-ft-" in model.lower()) and (
            not model.startswith("northwind-insurance-") or environment.get(
                "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED", "false").lower() != "true"):
        raise ValueError("A triage override or fine-tuned model is not an evaluated insurance model.")
    if role == "policy-wording":
        connection = environment.get("AGENT_KB_CONNECTION") or environment.get("AGENT_KB_CONNECTION_NAME", "")
        if (environment.get("AZURE_SEARCH_KNOWLEDGE_BASE") != "northwind-insurance-kb"
                or environment.get("AZURE_SEARCH_INDEX") != "northwind-insurance-knowledge"
                or connection.rsplit("/", 1)[-1] not in INSURANCE_KB_CONNECTIONS):
            raise ValueError("Insurance policy wording requires its explicit insurance KB, index and connection; "
                             "no other knowledge base can be substituted.")
    credential_source = getattr(args, "credential_source", "cli")
    if credential_source not in {"cli", "azd"}:
        raise ValueError("credential source must be cli or azd")
    if args.apply and not args.project_arm_id.lower().startswith(
            f"/subscriptions/{args.subscription.lower()}/"):
        raise ValueError("Project ARM ID must belong to the explicitly requested subscription.")
    report = {"agent": name, "project_endpoint": args.project_endpoint, "source_sha256": source_hash,
              "model": model, "applied": args.apply, "business_domain": "insurance",
              "credential_source": credential_source,
              "agent_runtime_ready": False, "roles_modified": bool(getattr(args, "grant_runtime_roles", False) and args.apply)}
    gateway_routing = {"models_required": env_flag(environment, "AI_GATEWAY_REQUIRED"),
                       "mcp_required": env_flag(environment, "AI_GATEWAY_MCP_REQUIRED") and role in MCP_GATEWAY_ROLES}
    if any(gateway_routing.values()):
        report["ai_gateway"] = gateway_routing
    if not args.apply:
        journal.atomic_json(args.output, report)
        print(json.dumps(report, indent=2))
        return
    routes = json.loads(setting(environment, "AGENT_MCP_GATEWAY") or "{}")
    draft_gateway = role == "claim-action-draft" and any(
        ROLE_ALIASES.get(key, key) == role for key in routes)
    if draft_gateway:
        key_env = getattr(args, "workflow_signing_key_env", None)
        pem = os.environ.get(key_env, "") if key_env else ""
        spec = importlib.util.spec_from_file_location("claim_workflow_binding", root / "apps" / "api" / "app" / "workflow_binding.py")
        binding = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(binding)
        binding.signing_key(pem)
        environment["CLAIM_WORKFLOW_SIGNING_KEY"] = pem
    elif getattr(args, "workflow_signing_key_env", None):
        raise ValueError("Workflow signing key is permitted only for the gateway claim-action-draft runtime")
    definition = HostedAgentDefinition(
        cpu="0.5", memory="1Gi", environment_variables=environment,
        protocol_versions=[ProtocolVersionRecord(protocol="responses", version="2.0.0")],
        code_configuration=CodeConfiguration(
            runtime="python_3_13", entry_point=["python", "main.py"], dependency_resolution="remote_build",
        ),
    )
    config_hash = hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
    report["config_sha256"] = config_hash
    credential = deployment_credential(credential_source, args.tenant)
    token_provider = get_bearer_token_provider(credential, "https://management.azure.com/.default")
    try:
        with AIProjectClient(endpoint=args.project_endpoint, credential=credential, allow_preview=True,
                             retry_total=0) as project:
            existing = None
            try:
                for version in list(project.agents.list_versions(name)):
                    metadata = version.metadata or {}
                    if (metadata.get("source_sha256") == source_hash
                            and metadata.get("config_sha256") == config_hash
                            and version.as_dict().get("status") in {"active", "creating"}):
                        existing = version
                        break
            except ResourceNotFoundError:
                pass
            if existing is None:
                report.update(submission_started=True, state="submission_started", submitted_at=journal.now())
                journal.atomic_json(args.output, report)
                with archive_path.open("rb") as source:
                    existing = project.agents.create_version_from_code(
                        agent_name=name, definition=definition, code=source,
                        description=f"Hosted Northwind insurance {role}",
                        metadata={"source_sha256": source_hash, "config_sha256": config_hash,
                                  "project": project_name, "business_domain": "insurance"},
                    )
            report.update(version=existing.version, state=existing.as_dict().get("status"))
            journal.atomic_json(args.output, report)
            print(json.dumps({"agent": name, "version": existing.version,
                              "state": existing.as_dict().get("status")}), flush=True)
            deadline = time.monotonic() + args.timeout
            last_status = None
            while time.monotonic() < deadline:
                version = project.agents.get_version(name, existing.version)
                state = version.as_dict().get("status")
                if state != last_status:
                    print(json.dumps({"agent": name, "version": version.version, "state": state}), flush=True)
                    last_status = state
                if state == "active":
                    break
                if state in {"failed", "deleted", "deleting"}:
                    raise RuntimeError(f"Hosted version {name}:{version.version} is {state}; "
                                       "inspect Foundry deployment diagnostics before retrying")
                time.sleep(5)
            else:
                raise TimeoutError(f"Hosted version {name}:{existing.version} did not become active")
            identity = version.as_dict().get("instance_identity") or {}
            principal = identity.get("principal_id")
            if not principal:
                raise RuntimeError("Active hosted agent has no runtime principal ID")
            account_scope = args.project_arm_id.rsplit("/projects/", 1)[0]
            if getattr(args, "grant_runtime_roles", False):
                grants = runtime_grants(environment, role, args.project_arm_id, account_scope)
                with httpx.Client(timeout=120) as http:
                    for scope, role_id in grants:
                        grant(http, token_provider, args.subscription, scope, principal, role_id)
                if "ai_gateway" in report:
                    report["ai_gateway"]["runtime_grants"] = [{"scope": scope, "role_id": role_id}
                                                              for scope, role_id in grants]
            if not getattr(args, "stage_only", False):
                project.agents.update_details(
                    agent_name=name,
                    agent_endpoint=AgentEndpointConfig(
                        version_selector=VersionSelector(version_selection_rules=[
                            FixedRatioVersionSelectionRule(agent_version=version.version, traffic_percentage=100),
                        ]),
                        protocol_configuration=ProtocolConfiguration(responses=ResponsesProtocolConfiguration()),
                    ),
                )
            report["endpoint_selected"] = not getattr(args, "stage_only", False)
            report.update({"version": version.version, "state": state, "principal_id": principal})
            native = version.as_dict()
            report["native_version"] = {
                field: native[field] for field in
                ("id", "name", "version", "status", "instance_identity", "created_at", "updated_at")
                if field in native
            }
            journal.atomic_json(args.output, report)
            print(json.dumps(report, indent=2))
    finally:
        credential.close()


if __name__ == "__main__":
    main()
