"""Preview the bounded GitHub/Azure deployment trust; only --apply changes it."""
from __future__ import annotations

import argparse
import json
import subprocess
import uuid

import httpx
from azure.identity import AzureCliCredential

SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
REPOSITORY = "ZacharyZurloMSFT/foundry-frontier"
BASE = f"/subscriptions/{SUBSCRIPTION}"
APPS = f"{BASE}/resourceGroups/rg-northwind-apps"
NETWORK = f"{BASE}/resourceGroups/rg-northwind-networking"
STORAGE = f"{BASE}/resourceGroups/rg-northwind-data/providers/Microsoft.Storage/storageAccounts/stzurlonorthwind"
ACR = f"{APPS}/providers/Microsoft.ContainerRegistry/registries/acrzurlonorthwind"
APPS_NAMES = ["ca-api", "ca-web-tech", "ca-web-voice", "ca-mcp-tech-lookup", "ca-mcp-work-order"]
IDENTITIES = {
    "apps": ("id-gh-northwind-apps", "northwind-apps", "AZURE_APPS_CLIENT_ID"),
    "infra-plan": ("id-gh-northwind-plan", "northwind-infra-plan", "AZURE_INFRA_PLAN_CLIENT_ID"),
    "infra-apply": ("id-gh-northwind-apply", "northwind-infra-apply", "AZURE_INFRA_APPLY_CLIENT_ID"),
}
ROLES = {
    "apps": {
        "name": "Northwind scoped app release",
        "actions": [
            "Microsoft.App/containerApps/read", "Microsoft.App/containerApps/write",
            "Microsoft.App/containerApps/revisions/read",
        ],
        "assignable": APPS,
    },
    "infra-plan": {
        "name": "Northwind networking plan",
        "actions": [
            "*/read", "Microsoft.Resources/deployments/read",
            "Microsoft.Resources/deployments/write", "Microsoft.Resources/deployments/whatIf/action",
            "Microsoft.Resources/deployments/validate/action",
        ],
        "assignable": NETWORK,
    },
    "infra-apply": {
        "name": "Northwind private storage networking",
        "actions": [
            "*/read", "Microsoft.Resources/deployments/*",
            "Microsoft.Network/privateEndpoints/write",
            "Microsoft.Network/privateEndpoints/privateDnsZoneGroups/write",
            "Microsoft.Network/privateDnsZones/write",
            "Microsoft.Network/privateDnsZones/virtualNetworkLinks/write",
            "Microsoft.Network/privateDnsZones/join/action",
            "Microsoft.Network/virtualNetworks/join/action",
            "Microsoft.Network/virtualNetworks/subnets/join/action",
        ],
        "assignable": NETWORK,
    },
    "storage-approval": {
        "name": "Northwind blob private endpoint approval",
        "actions": [
            "Microsoft.Storage/storageAccounts/read",
            "Microsoft.Storage/storageAccounts/privateEndpointConnections/read",
            "Microsoft.Storage/storageAccounts/privateEndpointConnectionsApproval/action",
        ],
        "assignable": f"{BASE}/resourceGroups/rg-northwind-data",
    },
}
ACRPUSH = "8311e382-0749-4cb8-b61a-304f252e45ec"
READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"


def stable_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/{REPOSITORY}:{label}"))


def role_id(key: str) -> str:
    return f"{BASE}/providers/Microsoft.Authorization/roleDefinitions/{stable_id('role:' + key)}"


def gh(path: str, method: str = "GET", body: dict | None = None) -> dict:
    args = ["gh", "api", path, "--method", method]
    if body is not None:
        args += ["--input", "-"]
    result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"GitHub configuration failed for {path}; no successful setup is implied.")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def bindings() -> list[tuple[str, str, str]]:
    result = [("apps", f"{APPS}/providers/Microsoft.App/containerApps/{name}", role_id("apps")) for name in APPS_NAMES]
    result += [
        ("apps", ACR, f"{BASE}/providers/Microsoft.Authorization/roleDefinitions/{ACRPUSH}"),
        ("infra-plan", NETWORK, role_id("infra-plan")),
        ("infra-plan", STORAGE, f"{BASE}/providers/Microsoft.Authorization/roleDefinitions/{READER}"),
        ("infra-apply", NETWORK, role_id("infra-apply")),
        ("infra-apply", STORAGE, role_id("storage-approval")),
    ]
    return result


def preview() -> dict:
    return {
        "repository": REPOSITORY, "subscription": SUBSCRIPTION, "tenant": TENANT,
        "identities": [
            {"name": name, "environment": environment,
             "federated_subject": f"repo:{REPOSITORY}:environment:{environment}"}
            for name, environment, _ in IDENTITIES.values()
        ],
        "role_definitions": ROLES,
        "role_bindings": [{"identity": key, "scope": scope, "role_id": role} for key, scope, role in bindings()],
        "environment_branch_allowlist": ["main"],
        "app_deployments": "after successful current-main CI, selected services only",
        "infrastructure": "plan by default; apply requires reviewed same-source plan and full SHA confirmation",
        "credentials": "OIDC only; no client secret",
        "actions_enabled_by_this_script": False,
    }


def configure() -> dict:
    repo = gh(f"repos/{REPOSITORY}")
    if not repo.get("private") or repo.get("default_branch") != "main":
        raise ValueError("Expected the private approved main-branch repository.")
    permission = gh(f"repos/{REPOSITORY}/actions/permissions")
    if permission.get("enabled") is not False:
        raise ValueError("Keep repository Actions disabled until setup is complete.")
    credential = AzureCliCredential(tenant_id=TENANT, process_timeout=120)
    clients = {}
    try:
        with httpx.Client(timeout=60) as client:
            def arm(method: str, path: str, api: str, body: dict | None = None, *, absent_ok: bool = False) -> dict | None:
                response = client.request(
                    method, f"https://management.azure.com{path}", params={"api-version": api},
                    headers={"Authorization": "Bearer " + credential.get_token("https://management.azure.com/.default").token},
                    json=body,
                )
                if absent_ok and response.status_code == 404:
                    return None
                if response.status_code not in (200, 201):
                    raise RuntimeError(f"Azure setup failed (HTTP {response.status_code}) for {path}; reconcile partial setup.")
                return response.json()

            for key, role in ROLES.items():
                body = {"properties": {
                    "roleName": role["name"], "description": f"Scoped GitHub Actions access for {REPOSITORY}",
                    "type": "CustomRole", "assignableScopes": [role["assignable"]],
                    "permissions": [{"actions": role["actions"], "notActions": [], "dataActions": [], "notDataActions": []}],
                }}
                arm("PUT", role_id(key), "2022-04-01", body)
            principals = {}
            for key, (name, environment, variable) in IDENTITIES.items():
                identity_id = f"{APPS}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}"
                existing = arm("GET", identity_id, "2023-01-31", absent_ok=True)
                if existing and existing.get("tags", {}).get("repository") != REPOSITORY:
                    raise ValueError("An existing identity is not owned by this setup; refusing to replace it.")
                identity = existing or arm("PUT", identity_id, "2023-01-31", {
                    "location": "canadacentral", "tags": {"repository": REPOSITORY, "purpose": "github-actions"},
                })
                principals[key] = identity["properties"]["principalId"]
                clients[variable] = identity["properties"]["clientId"]
                arm("PUT", identity_id + "/federatedIdentityCredentials/github-environment", "2023-01-31", {
                    "properties": {
                        "issuer": "https://token.actions.githubusercontent.com",
                        "subject": f"repo:{REPOSITORY}:environment:{environment}",
                        "audiences": ["api://AzureADTokenExchange"],
                    },
                })
                gh(f"repos/{REPOSITORY}/environments/{environment}", "PUT", {
                    "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
                })
                policies = gh(f"repos/{REPOSITORY}/environments/{environment}/deployment-branch-policies").get("branch_policies", [])
                if any(p.get("name") != "main" or p.get("type") != "branch" for p in policies):
                    raise ValueError("Environment has unexpected allowed branches; review before activation.")
                if not policies:
                    gh(f"repos/{REPOSITORY}/environments/{environment}/deployment-branch-policies", "POST", {"name": "main", "type": "branch"})
            for key, scope, role in bindings():
                assignment = stable_id(f"assignment:{key}:{scope}:{role}")
                path = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment}"
                arm("PUT", path, "2022-04-01", {"properties": {
                    "roleDefinitionId": role, "principalId": principals[key], "principalType": "ServicePrincipal",
                }})
    finally:
        credential.close()
    variables = {
        **clients, "AZURE_TENANT_ID": TENANT, "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_APPS_RESOURCE_GROUP": "rg-northwind-apps", "ACR_NAME": "acrzurlonorthwind",
        "NORTHWIND_DEPLOY_ENABLED": "true", "NORTHWIND_INFRA_ENABLED": "true",
    }
    current = {v["name"] for v in gh(f"repos/{REPOSITORY}/actions/variables?per_page=100").get("variables", [])}
    for name, value in variables.items():
        path = f"repos/{REPOSITORY}/actions/variables" + (f"/{name}" if name in current else "")
        gh(path, "PATCH" if name in current else "POST", {"name": name, "value": value})
    return {"configured": True, "repository": REPOSITORY, "variables": list(variables), "actions_still_disabled": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(configure() if args.apply else preview(), indent=2))
