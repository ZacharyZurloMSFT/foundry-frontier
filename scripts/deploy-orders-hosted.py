"""Deploy one Orders hosted agent from an allowlisted source ZIP."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
import time
import uuid
import zipfile
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
from orders_agent_package import PACKAGE_SOURCES as PACKAGE_SOURCES, bundle

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.hosted.config import Settings
from apps.api.app.insurance_config import (
    INSURANCE_CONNECTION, INSURANCE_INDEX, INSURANCE_KB, INSURANCE_PROJECT,
    ROLE_ALIASES, ROLE_DIRECTORIES, configuration_hash, normalize_environment,
    normalize_role, valid_project_endpoint, validate_insurance_connection,
)
journal = importlib.import_module("run-orders-native-demo-evals")

ROLES = {
    "triage": "northwind-triage",
    "orderlookup": "northwind-order-lookup",
    "policylookup": "northwind-policy-lookup",
    "resolutiondraft": "northwind-resolution-draft",
    "manager": "northwind-orchestrator",
}
INSURANCE_ROLES = {
    "triage": "northwind-insurance-triage",
    "orderlookup": "northwind-insurance-policy-claim-lookup",
    "policylookup": "northwind-insurance-policy-wording",
    "resolutiondraft": "northwind-insurance-claim-action-draft",
    "manager": "northwind-insurance-orchestrator",
}
FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
AGENT_CONSUMER = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"


def deployment_credential(source: str, tenant: str):
    """Select one cached CLI identity explicitly; never try another principal."""
    if source == "cli":
        return AzureCliCredential(tenant_id=tenant, process_timeout=120)
    if source == "azd":
        return AzureDeveloperCliCredential(tenant_id=tenant, process_timeout=120)
    raise ValueError("credential source must be cli or azd")


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
    parser.add_argument("--role", required=True, choices=(*ROLES, *ROLE_DIRECTORIES))
    parser.add_argument("--agent-name", help="Optional triage demo companion; baseline names remain the default")
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--project-arm-id", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--credential-source", choices=("cli", "azd"), default="cli",
                        help="Explicit cached identity: Azure CLI (default) or Azure Developer CLI; no fallback")
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path, help="Non-secret per-role environment JSON")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--expected-version", help="Required serving version before changing an existing insurance endpoint")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--business-domain", choices=("retail", "insurance"))
    parser.add_argument("--grant-runtime-roles", action="store_true",
                        help="Explicit opt-in to legacy RBAC writes; existing roles are otherwise never changed")
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
        if previous.get("submission_started") and source_hash != previous.get("source_sha256"):
            raise ValueError("Candidate source differs; preserve the original package and version")
        pending_archive.replace(archive_path)
    finally:
        if pending_archive.exists():
            pending_archive.unlink()
    configuration = json.loads(args.config.read_text(encoding="utf-8"))
    role = normalize_role(args.role)
    legacy_role = next((old for old, new in ROLE_ALIASES.items() if new == role), role)
    if role in configuration and legacy_role != role and legacy_role in configuration:
        if normalize_environment(configuration[role]) != normalize_environment(configuration[legacy_role]):
            raise ValueError("Conflicting canonical and deprecated role configuration")
    environment = {**configuration.get("common", {}),
                   **configuration.get(role, configuration.get(legacy_role, {})), "AGENT_ROLE": role}
    if any(not isinstance(value, str) for value in environment.values()):
        raise ValueError("Agent environment values must be strings")
    domain = getattr(args, "business_domain", None) or environment.get("NORTHWIND_BUSINESS_DOMAIN", "retail")
    if domain not in {"retail", "insurance"}:
        raise ValueError("NORTHWIND_BUSINESS_DOMAIN must be retail or insurance")
    environment["NORTHWIND_BUSINESS_DOMAIN"] = domain
    if environment.get("NORTHWIND_INSURANCE_AGENTS_READY", "false").lower() == "true":
        raise ValueError("An agent deployment cannot declare API fleet readiness; verify every specialist first.")
    environment.pop("NORTHWIND_INSURANCE_AGENTS_READY", None)
    if any(name.startswith("FOUNDRY_") for name in environment):
        raise ValueError("FOUNDRY_* variables are platform-reserved")
    environment = normalize_environment(environment)
    if not environment.get("MODEL_DEPLOYMENT_NAME"):
        raise ValueError("MODEL_DEPLOYMENT_NAME is required")
    names = INSURANCE_ROLES if domain == "insurance" else ROLES
    name = args.agent_name or names[legacy_role]
    if name != names[legacy_role] and not (domain == "retail" and role == "triage" and name == "northwind-triage-ft-demo"):
        raise ValueError("Only the separate northwind-triage-ft-demo companion may override an agent name")
    if domain == "insurance":
        if not valid_project_endpoint(args.project_endpoint, INSURANCE_PROJECT):
            raise ValueError("Insurance deployment must target the retained nw-insurance-claims project.")
        if args.apply and not args.project_arm_id.rstrip("/").endswith(f"/projects/{INSURANCE_PROJECT}"):
            raise ValueError("Insurance project ARM ID must match nw-insurance-claims.")
        model = environment.get("TRIAGE_MODEL_DEPLOYMENT") or environment["MODEL_DEPLOYMENT_NAME"]
        if (environment.get("TRIAGE_MODEL_DEPLOYMENT") or "-ft-" in model.lower()) and (
                not model.startswith("northwind-insurance-") or environment.get(
                    "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED", "false").lower() != "true"):
            raise ValueError("A retail triage fine-tune is not an evaluated insurance model.")
        if role == "policy_wording":
            connection = environment.get("INSURANCE_KB_CONNECTION_NAME", "")
            if (environment.get("AZURE_SEARCH_KNOWLEDGE_BASE") != INSURANCE_KB
                    or environment.get("AZURE_SEARCH_INDEX") != INSURANCE_INDEX
                    or connection != INSURANCE_CONNECTION):
                raise ValueError("Insurance policy wording requires its explicit insurance KB, index and connection; no retail fallback.")
    settings = Settings.from_env({**environment, "INSURANCE_PROJECT_ENDPOINT": args.project_endpoint})
    environment = dict(settings.env)
    config_hash = configuration_hash(environment)
    with zipfile.ZipFile(archive_path) as archive:
        manifest_bytes = archive.read("agents/hosted/build.json")
    source_manifest = json.loads(manifest_bytes)
    if args.apply and (not source_manifest["source_clean"] or not source_manifest["source_commit"]):
        raise ValueError("Apply requires a clean, committed source tree; uncommitted bytes cannot claim reviewed Git provenance.")
    credential_source = getattr(args, "credential_source", "cli")
    if credential_source not in {"cli", "azd"}:
        raise ValueError("credential source must be cli or azd")
    if args.apply and not args.project_arm_id.lower().startswith(
            f"/subscriptions/{args.subscription.lower()}/"):
        raise ValueError("Project ARM ID must belong to the explicitly requested subscription.")
    report = {"agent": name, "project_endpoint": args.project_endpoint, "source_sha256": source_hash,
              "model": settings.model, "config_sha256": config_hash,
              "prompt_sha256": settings.prompt_sha256, "role": role,
              "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
              "source_commit": source_manifest["source_commit"],
              "applied": args.apply, "business_domain": domain,
              "credential_source": credential_source,
              "agent_runtime_ready": False, "roles_modified": bool(getattr(args, "grant_runtime_roles", False) and args.apply)}
    if not args.apply:
        journal.atomic_json(args.output, report)
        print(json.dumps(report, indent=2))
        return
    definition = HostedAgentDefinition(
        cpu="0.5", memory="1Gi", environment_variables=environment,
        protocol_versions=[ProtocolVersionRecord(protocol="responses", version="2.0.0")],
        code_configuration=CodeConfiguration(
            runtime="python_3_13", entry_point=["python", "main.py"], dependency_resolution="remote_build",
        ),
    )
    candidate = name == "northwind-triage-ft-demo"
    report["config_sha256"] = config_hash
    if previous.get("submission_started"):
        if any(previous.get(key) != report[key] for key in ("agent", "source_sha256", "config_sha256", "model")):
            raise ValueError("Existing candidate submission differs; do not create another version")
        report = previous
    credential = deployment_credential(credential_source, args.tenant)
    token_provider = get_bearer_token_provider(credential, "https://management.azure.com/.default")
    try:
        with AIProjectClient(endpoint=args.project_endpoint, credential=credential, allow_preview=True,
                             retry_total=0) as project:
            existing = None
            versions = []
            before_endpoint = None
            try:
                before_endpoint = project.agents.get(name).as_dict().get("agent_endpoint")
            except ResourceNotFoundError:
                pass
            if domain == "insurance" and before_endpoint is not None:
                selector = (before_endpoint.get("version_selector") or {}).get("version_selection_rules") or []
                if (len(selector) != 1
                        or selector[0].get("agent_version") != getattr(args, "expected_version", None)
                        or selector[0].get("traffic_percentage") != 100):
                    raise ValueError("The serving version does not match --expected-version; review the endpoint before changing it.")
            report["previous_endpoint"] = before_endpoint
            if domain == "insurance" and role == "policy_wording":
                connection = project.connections.get(INSURANCE_CONNECTION, include_credentials=False).as_dict()
                validate_insurance_connection(connection, environment["AZURE_SEARCH_ENDPOINT"])
            try:
                versions = list(project.agents.list_versions(name))
                for version in versions:
                    metadata = version.metadata or {}
                    if (metadata.get("source_sha256") == source_hash
                            and metadata.get("config_sha256") == config_hash
                            and version.as_dict().get("status") in {"active", "creating"}):
                        existing = version
                        break
            except ResourceNotFoundError:
                pass
            if candidate and (len(versions) > 1 or (versions and existing is None)):
                raise ValueError("Candidate already has a differing or failed version; no automatic replacement")
            if existing is None and report.get("submission_started") and previous.get("submission_started"):
                raise RuntimeError("Uncertain prior candidate POST; reconcile manually, never resubmit")
            if existing is None:
                report.update(submission_started=True, state="submission_started", submitted_at=journal.now())
                journal.atomic_json(args.output, report)
                with archive_path.open("rb") as source:
                    existing = project.agents.create_version_from_code(
                        agent_name=name, definition=definition, code=source,
                        description=f"Hosted Northwind {domain} {args.role}",
                        metadata={"source_sha256": source_hash, "config_sha256": config_hash,
                                  "project": urlsplit(args.project_endpoint).path.rstrip("/").rsplit("/", 1)[-1],
                                  "business_domain": domain, "role": role,
                                  "prompt_sha256": settings.prompt_sha256,
                                  "source_manifest_sha256": report["source_manifest_sha256"],
                                  "source_commit": source_manifest["source_commit"]},
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
                    raise RuntimeError(f"Hosted version {name}:{version.version} is {state}: {version.as_dict().get('error')}")
                time.sleep(5)
            else:
                raise TimeoutError(f"Hosted version {name}:{existing.version} did not become active")
            identity = version.as_dict().get("instance_identity") or {}
            actual_definition = version.as_dict().get("definition") or {}
            if ((actual_definition.get("code_configuration") or {}).get("content_hash") != source_hash
                    or actual_definition.get("environment_variables") != environment):
                raise RuntimeError("Deployed content/configuration differs from the exact submitted artifact; endpoint unchanged.")
            principal = identity.get("principal_id")
            if not principal:
                raise RuntimeError("Active hosted agent has no runtime principal ID")
            account_scope = args.project_arm_id.rsplit("/projects/", 1)[0]
            if getattr(args, "grant_runtime_roles", False):
                with httpx.Client(timeout=120) as http:
                    grant(http, token_provider, args.subscription, account_scope, principal, OPENAI_USER)
                    grant(http, token_provider, args.subscription, args.project_arm_id, principal, FOUNDRY_USER)
                    if role == "orchestrator":
                        grant(http, token_provider, args.subscription, args.project_arm_id, principal, AGENT_CONSUMER)
            current_endpoint = project.agents.get(name).as_dict().get("agent_endpoint")
            if current_endpoint != before_endpoint:
                raise RuntimeError("Agent endpoint changed during deployment; refusing to overwrite it.")
            project.agents.update_details(
                agent_name=name,
                agent_endpoint=AgentEndpointConfig(
                    version_selector=VersionSelector(version_selection_rules=[
                        FixedRatioVersionSelectionRule(agent_version=version.version, traffic_percentage=100),
                    ]),
                    protocol_configuration=ProtocolConfiguration(responses=ResponsesProtocolConfiguration()),
                ),
            )
            report.update({"version": version.version, "state": state, "principal_id": principal})
            report["native_version"] = version.as_dict()
            journal.atomic_json(args.output, report)
            print(json.dumps(report, indent=2))
    finally:
        credential.close()


if __name__ == "__main__":
    main()
