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
REPOSITORY_ID = 1421326484
OWNER_ID = 21691488
SUBJECT_PREFIX = f"repo:ZacharyZurloMSFT@{OWNER_ID}/foundry-frontier@{REPOSITORY_ID}"
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
    "apim-plan": ("id-gh-northwind-apim-plan", "northwind-apim-plan", "AZURE_APIM_PLAN_CLIENT_ID"),
    "apim-apply": ("id-gh-northwind-apim-apply", "northwind-apim-apply", "AZURE_APIM_APPLY_CLIENT_ID"),
}
APIM_KEYS = ("apim-plan", "apim-apply")
ONLY = {"apim": APIM_KEYS}
ONLY_FLAGS = {"apim": "NORTHWIND_APIM_ENABLED"}
APIM_READS = [
    "Microsoft.ApiManagement/service/read", "Microsoft.ApiManagement/service/*/read",
    "Microsoft.App/containerApps/read", "Microsoft.Insights/components/read",
    "Microsoft.Resources/subscriptions/resourceGroups/read",
]
# Deliberately no service-level policies/write, listSecrets, delete or wildcard writes. Adding a resource type
# is a deliberate boundary change: update this list, RESOURCE_TYPES in apim-release.py and their tests together.
APIM_WRITES = [
    f"Microsoft.ApiManagement/service/{child}/write" for child in (
        "apis", "apis/operations", "apis/operations/policies", "apis/policies", "apis/diagnostics",
        "products", "products/apis", "subscriptions", "loggers",
    )
]
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
    "apim-plan": {
        "name": "Northwind apim plan",
        "actions": [
            *APIM_READS, "Microsoft.Resources/deployments/read",
            "Microsoft.Resources/deployments/write", "Microsoft.Resources/deployments/whatIf/action",
            "Microsoft.Resources/deployments/validate/action",
        ],
        "assignable": APPS,
    },
    "apim-apply": {
        "name": "Northwind apim configuration",
        "actions": [*APIM_READS, "Microsoft.Resources/deployments/*", *APIM_WRITES],
        "assignable": APPS,
    },
}
ACRPUSH = "8311e382-0749-4cb8-b61a-304f252e45ec"
READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"


def stable_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/{REPOSITORY}:{label}"))


def role_id(key: str) -> str:
    return f"{BASE}/providers/Microsoft.Authorization/roleDefinitions/{stable_id('role:' + key)}"


def validate_repository(repo: dict, *, allow_public: bool = False) -> None:
    """The trust is bound to the approved main-branch repository; a public one needs an explicit opt-in.

    The opt-in relaxes only the visibility check. The immutable repository and owner IDs, the immutable OIDC
    subject and the main-only environments are still enforced, so a renamed, recreated or forked repository
    cannot obtain these identities.
    """
    if repo.get("default_branch") != "main":
        raise ValueError("Expected the approved main-branch repository.")
    if not repo.get("private") and not allow_public:
        raise ValueError(
            "The repository is public. Review the public-repository notes in docs/api-management.md "
            "and re-run with --allow-public-repository to accept OIDC trust on a public repository."
        )


def validate_oidc_policy(repo: dict, policy: dict) -> None:
    if (repo.get("id") != REPOSITORY_ID or repo.get("owner", {}).get("id") != OWNER_ID
            or policy.get("use_default") is not True or policy.get("use_immutable_subject") is not True
            or policy.get("sub_claim_prefix") != SUBJECT_PREFIX):
        raise ValueError("Repository identity or OIDC subject policy changed; review trust before activation.")


def gh(path: str, method: str = "GET", body: dict | None = None) -> dict:
    args = ["gh", "api", path, "--method", method]
    if body is not None:
        args += ["--input", "-"]
    result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"GitHub configuration failed for {path}; no successful setup is implied.")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def identity_keys(only: str | None = None) -> list[str]:
    return list(IDENTITIES) if only is None else list(ONLY[only])


def role_keys(only: str | None = None) -> list[str]:
    return list(ROLES) if only is None else list(ONLY[only])


def static_variables(only: str | None = None) -> dict[str, str]:
    if only is not None:
        return {ONLY_FLAGS[only]: "true"}
    return {
        "AZURE_TENANT_ID": TENANT, "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_APPS_RESOURCE_GROUP": "rg-northwind-apps", "ACR_NAME": "acrzurlonorthwind",
        "NORTHWIND_DEPLOY_ENABLED": "true", "NORTHWIND_INFRA_ENABLED": "true", "NORTHWIND_APIM_ENABLED": "true",
    }


def restrict_environment(environment: str) -> None:
    gh(f"repos/{REPOSITORY}/environments/{environment}", "PUT", {
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    })
    policies = gh(f"repos/{REPOSITORY}/environments/{environment}/deployment-branch-policies").get("branch_policies", [])
    if any(p.get("name") != "main" or p.get("type") != "branch" for p in policies):
        raise ValueError("Environment has unexpected allowed branches; review before activation.")
    if not policies:
        gh(f"repos/{REPOSITORY}/environments/{environment}/deployment-branch-policies", "POST", {"name": "main", "type": "branch"})


def check_preconditions(only: str | None = None) -> None:
    if only is None:
        if gh(f"repos/{REPOSITORY}/actions/permissions").get("enabled") is not False:
            raise ValueError("Keep repository Actions disabled until setup is complete.")
        return
    # --only runs against a live repository, so pin the environments to main before any Azure trust exists.
    for key in identity_keys(only):
        restrict_environment(IDENTITIES[key][1])


def bindings(only: str | None = None) -> list[tuple[str, str, str]]:
    result = [("apps", f"{APPS}/providers/Microsoft.App/containerApps/{name}", role_id("apps")) for name in APPS_NAMES]
    result += [
        ("apps", ACR, f"{BASE}/providers/Microsoft.Authorization/roleDefinitions/{ACRPUSH}"),
        ("infra-plan", NETWORK, role_id("infra-plan")),
        ("infra-plan", STORAGE, f"{BASE}/providers/Microsoft.Authorization/roleDefinitions/{READER}"),
        ("infra-apply", NETWORK, role_id("infra-apply")),
        ("infra-apply", STORAGE, role_id("storage-approval")),
        ("apim-plan", APPS, role_id("apim-plan")),
        ("apim-apply", APPS, role_id("apim-apply")),
    ]
    return [binding for binding in result if binding[0] in identity_keys(only)]


def preview(only: str | None = None) -> dict:
    return {
        "repository": REPOSITORY, "subscription": SUBSCRIPTION, "tenant": TENANT,
        "only": only,
        "identities": [
            {"name": name, "environment": environment,
             "federated_subject": f"{SUBJECT_PREFIX}:environment:{environment}"}
            for name, environment, _ in (IDENTITIES[key] for key in identity_keys(only))
        ],
        "role_definitions": {key: ROLES[key] for key in role_keys(only)},
        "role_bindings": [{"identity": key, "scope": scope, "role_id": role} for key, scope, role in bindings(only)],
        "variables": [*(IDENTITIES[key][2] for key in identity_keys(only)), *static_variables(only)],
        "environment_branch_allowlist": ["main"],
        "app_deployments": "after successful current-main CI, selected services only",
        "infrastructure": "plan by default; apply requires reviewed same-source plan and full SHA confirmation",
        "api_management": "plan by default; apply requires reviewed same-source plan and full SHA confirmation",
        "credentials": "OIDC only; no client secret",
        "actions_enabled_by_this_script": False,
    }


def configure(only: str | None = None, *, allow_public_repository: bool = False) -> dict:
    repo = gh(f"repos/{REPOSITORY}")
    validate_repository(repo, allow_public=allow_public_repository)
    validate_oidc_policy(repo, gh(f"repos/{REPOSITORY}/actions/oidc/customization/sub"))
    check_preconditions(only)
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

            for key in role_keys(only):
                role = ROLES[key]
                body = {"properties": {
                    "roleName": role["name"], "description": f"Scoped GitHub Actions access for {REPOSITORY}",
                    "type": "CustomRole", "assignableScopes": [role["assignable"]],
                    "permissions": [{"actions": role["actions"], "notActions": [], "dataActions": [], "notDataActions": []}],
                }}
                arm("PUT", role_id(key), "2022-04-01", body)
            principals = {}
            for key in identity_keys(only):
                name, environment, variable = IDENTITIES[key]
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
                        "subject": f"{SUBJECT_PREFIX}:environment:{environment}",
                        "audiences": ["api://AzureADTokenExchange"],
                    },
                })
                if only is None:
                    restrict_environment(environment)
            for key, scope, role in bindings(only):
                assignment = stable_id(f"assignment:{key}:{scope}:{role}")
                path = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment}"
                arm("PUT", path, "2022-04-01", {"properties": {
                    "roleDefinitionId": role, "principalId": principals[key], "principalType": "ServicePrincipal",
                }})
    finally:
        credential.close()
    variables = {**clients, **static_variables(only)}
    current = {v["name"] for v in gh(f"repos/{REPOSITORY}/actions/variables?per_page=100").get("variables", [])}
    for name, value in variables.items():
        path = f"repos/{REPOSITORY}/actions/variables" + (f"/{name}" if name in current else "")
        gh(path, "PATCH" if name in current else "POST", {"name": name, "value": value})
    return {"configured": True, "repository": REPOSITORY, "variables": list(variables),
            **({"actions_still_disabled": True} if only is None else {"only": only})}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--only", choices=sorted(ONLY),
                        help="Touch just this subsystem's identities, roles, bindings and variables")
    parser.add_argument("--allow-public-repository", action="store_true",
                        help="Accept OIDC trust on a public repository (the default refuses; see docs/api-management.md)")
    args = parser.parse_args()
    print(json.dumps(
        configure(args.only, allow_public_repository=args.allow_public_repository) if args.apply else preview(args.only),
        indent=2,
    ))
