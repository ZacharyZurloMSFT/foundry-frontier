"""Deploy one Orders hosted agent from an allowlisted source ZIP."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
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
from orders_agent_package import PACKAGE_SOURCES as PACKAGE_SOURCES, bundle

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
    parser.add_argument("--role", required=True, choices=ROLES)
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
        if (args.agent_name == "northwind-triage-ft-demo" and previous.get("submission_started")
                and source_hash != previous.get("source_sha256")):
            raise ValueError("Candidate source differs; preserve the original package and version")
        pending_archive.replace(archive_path)
    finally:
        if pending_archive.exists():
            pending_archive.unlink()
    configuration = json.loads(args.config.read_text(encoding="utf-8"))
    environment = {**configuration.get("common", {}), **configuration.get(args.role, {}),
                   "ORDER_AGENT_ROLE": args.role}
    domain = getattr(args, "business_domain", None) or environment.get("NORTHWIND_BUSINESS_DOMAIN", "retail")
    if domain not in {"retail", "insurance"}:
        raise ValueError("NORTHWIND_BUSINESS_DOMAIN must be retail or insurance")
    environment["NORTHWIND_BUSINESS_DOMAIN"] = domain
    if environment.get("NORTHWIND_INSURANCE_AGENTS_READY", "false").lower() == "true":
        raise ValueError("An agent deployment cannot declare API fleet readiness; verify every specialist first.")
    if any(name.startswith("FOUNDRY_") for name in environment):
        raise ValueError("FOUNDRY_* variables are platform-reserved")
    if not environment.get("ORDERS_MODEL_DEPLOYMENT"):
        raise ValueError("ORDERS_MODEL_DEPLOYMENT is required")
    names = INSURANCE_ROLES if domain == "insurance" else ROLES
    name = args.agent_name or names[args.role]
    if name != names[args.role] and not (domain == "retail" and args.role == "triage" and name == "northwind-triage-ft-demo"):
        raise ValueError("Only the separate northwind-triage-ft-demo companion may override an agent name")
    if domain == "insurance":
        endpoint = urlsplit(args.project_endpoint)
        if (endpoint.scheme != "https" or endpoint.path.rstrip("/") != "/api/projects/northwind-orders"
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("Insurance deployment must explicitly target the existing native northwind-orders project, not legacy web northwind.")
        if args.apply and not args.project_arm_id.rstrip("/").endswith("/projects/northwind-orders"):
            raise ValueError("Insurance project ARM ID must match the existing northwind-orders project.")
        model = environment.get("TRIAGE_MODEL_DEPLOYMENT") or environment["ORDERS_MODEL_DEPLOYMENT"]
        if (environment.get("TRIAGE_MODEL_DEPLOYMENT") or "-ft-" in model.lower()) and (
                not model.startswith("northwind-insurance-") or environment.get(
                    "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED", "false").lower() != "true"):
            raise ValueError("A retail triage fine-tune is not an evaluated insurance model.")
        if args.role == "policylookup":
            connection = environment.get("ORDERS_KB_CONNECTION") or environment.get("ORDERS_KB_CONNECTION_NAME", "")
            if (environment.get("AZURE_SEARCH_KNOWLEDGE_BASE") != "northwind-insurance-kb"
                    or environment.get("AZURE_SEARCH_INDEX") != "northwind-insurance-knowledge"
                    or connection.rsplit("/", 1)[-1] != "northwind-insurance-kb-mcp"):
                raise ValueError("Insurance policy wording requires its explicit insurance KB, index and connection; no retail fallback.")
    credential_source = getattr(args, "credential_source", "cli")
    if credential_source not in {"cli", "azd"}:
        raise ValueError("credential source must be cli or azd")
    if args.apply and not args.project_arm_id.lower().startswith(
            f"/subscriptions/{args.subscription.lower()}/"):
        raise ValueError("Project ARM ID must belong to the explicitly requested subscription.")
    report = {"agent": name, "project_endpoint": args.project_endpoint, "source_sha256": source_hash,
              "model": environment.get("TRIAGE_MODEL_DEPLOYMENT") or environment["ORDERS_MODEL_DEPLOYMENT"],
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
    config_hash = hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
    candidate = name == "northwind-triage-ft-demo"
    report["config_sha256"] = config_hash
    if candidate and previous.get("submission_started"):
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
            if candidate and existing is None and report.get("submission_started"):
                raise RuntimeError("Uncertain prior candidate POST; reconcile manually, never resubmit")
            if existing is None:
                report.update(submission_started=True, state="submission_started", submitted_at=journal.now())
                journal.atomic_json(args.output, report)
                with archive_path.open("rb") as source:
                    existing = project.agents.create_version_from_code(
                        agent_name=name, definition=definition, code=source,
                        description=f"Hosted Northwind {domain} {args.role}",
                        metadata={"source_sha256": source_hash, "config_sha256": config_hash,
                                  "project": "northwind-orders", "business_domain": domain},
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
            principal = identity.get("principal_id")
            if not principal:
                raise RuntimeError("Active hosted agent has no runtime principal ID")
            account_scope = args.project_arm_id.rsplit("/projects/", 1)[0]
            if getattr(args, "grant_runtime_roles", False):
                with httpx.Client(timeout=120) as http:
                    grant(http, token_provider, args.subscription, account_scope, principal, OPENAI_USER)
                    grant(http, token_provider, args.subscription, args.project_arm_id, principal, FOUNDRY_USER)
                    if args.role == "manager":
                        grant(http, token_provider, args.subscription, args.project_arm_id, principal, AGENT_CONSUMER)
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
