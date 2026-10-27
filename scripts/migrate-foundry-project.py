"""Stage a Foundry project migration inside one AI Services account without reprovisioning shared resources."""
from __future__ import annotations

import argparse
import json
import time
import uuid
from urllib.parse import quote

import httpx
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential, get_bearer_token_provider

API_VERSION = "2025-06-01"
ROLE_VERSION = "2022-04-01"


class Migration:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.credential = AzureCliCredential(tenant_id=args.tenant, process_timeout=120)
        self.arm_token = get_bearer_token_provider(
            self.credential, "https://management.azure.com/.default"
        )
        self.http = httpx.Client(timeout=120)
        self.group = f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}"
        self.account = f"{self.group}/providers/Microsoft.CognitiveServices/accounts/{args.account}"
        self.source = f"{self.account}/projects/{args.source}"
        self.target = f"{self.account}/projects/{args.target}"

    def close(self):
        self.http.close()
        self.credential.close()

    def request(self, method: str, path: str, body: dict | None = None,
                *, version: str | None = None, missing_ok: bool = False) -> dict | None:
        token = self.arm_token()
        url = f"https://management.azure.com{path}"
        response = self.http.request(
            method, url, params={"api-version": version or self.args.api_version},
            headers={"Authorization": f"Bearer {token}"}, json=body,
        )
        if response.status_code == 404 and missing_ok:
            return None
        response.raise_for_status()
        return response.json() if response.content else {}

    def inspect(self) -> dict:
        account = self.request("GET", self.account)
        projects = self.request("GET", f"{self.account}/projects")
        connections = self.request("GET", f"{self.source}/connections")
        return {
            "account": self.args.account,
            "default_project": account["properties"].get("defaultProject"),
            "projects": [
                {"name": p["name"].split("/")[-1],
                 "is_default": p["properties"].get("isDefault", False)}
                for p in projects["value"]
            ],
            "source_connections": [
                {"name": c["name"], "category": c["properties"]["category"],
                 "auth_type": c["properties"]["authType"]}
                for c in connections["value"]
            ],
            "target": self.args.target,
            "apply": self.args.apply,
        }

    def provision(self) -> dict:
        source = self.request("GET", self.source)
        if not self.args.apply:
            return {"planned": "create project, copy connections and identity role grants",
                    **self.inspect()}
        current = self.request("GET", self.target, missing_ok=True)
        if current is None:
            self.request("PUT", self.target, {
                "location": source["location"],
                "identity": {"type": "SystemAssigned"},
                "tags": {**source.get("tags", {}), "migration-source": self.args.source},
                "properties": {"displayName": self.args.target,
                               "description": f"Northwind Insurance project migrated from {self.args.source}"},
            })
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = self.request("GET", self.target)
            state = current["properties"].get("provisioningState")
            if state == "Succeeded":
                break
            if state in {"Failed", "Canceled", "Deleted"}:
                raise RuntimeError(f"Target project provisioning failed: {state}")
            time.sleep(3)
        else:
            raise TimeoutError("Project provisioning did not finish within five minutes")

        copied = []
        mapping = {}
        appinsights_target = None
        connections = self.request("GET", f"{self.source}/connections")["value"]
        for connection in connections:
            if not connection["id"].lower().startswith((self.source + "/connections/").lower()):
                continue
            name = connection["name"].split("/")[-1]
            category = connection["properties"]["category"]
            suffix = {"RemoteTool": "kb-mcp", "CognitiveSearch": "search",
                      "AppInsights": "appinsights"}.get(category)
            if not suffix:
                raise ValueError(f"Unsupported migration connection category: {category}")
            new_name = f"{self.args.target}-{suffix}"
            if category == "AppInsights" and appinsights_target is not None:
                if connection["properties"]["target"] != appinsights_target:
                    raise ValueError("Source has multiple different App Insights targets; review explicitly")
                mapping[name] = new_name
                copied.append({"source": name, "target": new_name, "category": category,
                               "action": "reuse identical inherited telemetry connection"})
                continue
            if new_name in mapping.values():
                raise ValueError(f"Multiple source connections map to {new_name}; review explicitly")
            # Connection credentials stay in memory and are never written to logs or artifacts.
            endpoint = source["properties"]["endpoints"]["AI Foundry API"]
            with AIProjectClient(endpoint=endpoint, credential=self.credential) as project_client:
                secured = project_client.connections.get(name, include_credentials=True).as_dict()
            credentials = {k: v for k, v in secured.get("credentials", {}).items() if k != "type"}
            if not credentials:
                raise ValueError(f"Source connection has no retrievable credentials: {name}")
            auth_type = connection["properties"]["authType"]
            if auth_type == "CustomKeys":
                credentials = {"keys": credentials}
            elif auth_type != "ApiKey":
                raise ValueError(f"Unsupported credential migration: {auth_type}")
            properties = {**connection["properties"], "credentials": credentials}
            allowed = {"authType", "category", "target", "credentials", "metadata",
                       "isSharedToAll", "sharedUserList", "expiryTime"}
            body = {"properties": {k: v for k, v in properties.items() if k in allowed}}
            self.request("PUT", f"{self.target}/connections/{quote(new_name)}", body)
            if category == "AppInsights":
                appinsights_target = connection["properties"]["target"]
            mapping[name] = new_name
            copied.append({"source": name, "target": new_name, "category": category})

        source_principal = source["identity"]["principalId"]
        target_principal = current["identity"]["principalId"]
        role_response = self.request(
            "GET", f"{self.group}/providers/Microsoft.Authorization/roleAssignments",
            version=ROLE_VERSION,
        )
        if role_response.get("nextLink"):
            raise RuntimeError("Role inventory is paginated; review all grants before continuing")
        grants = []
        for assignment in role_response["value"]:
            props = assignment["properties"]
            if props["principalId"].lower() != source_principal.lower():
                continue
            scope = props["scope"]
            if scope.lower().startswith(self.source.lower()):
                scope = self.target + scope[len(self.source):]
            role = props["roleDefinitionId"]
            assignment_name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{role}|{target_principal}"))
            self.request(
                "PUT", f"{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment_name}",
                {"properties": {"principalId": target_principal, "principalType": "ServicePrincipal",
                                "roleDefinitionId": role}},
                version=ROLE_VERSION,
            )
            grants.append({"scope": scope, "role": role.split("/")[-1]})
        return {"project": self.args.target, "endpoint": current["properties"].get("endpoints"),
                "principal_id": target_principal, "connections": copied, "role_grants": grants,
                "default_project_unchanged": True}

    def set_default(self) -> dict:
        desired = self.args.target if self.args.stage == "default" else self.args.previous_default
        if not desired:
            raise ValueError("--previous-default is required for rollback")
        self.request("GET", f"{self.account}/projects/{desired}")
        account = self.request("GET", self.account)
        previous = account["properties"].get("defaultProject")
        if not self.args.apply:
            return {"planned_default": desired, "current_default": previous}
        if not self.args.default_callers_reviewed:
            raise ValueError("Review default-routed callers and stateful assets, then pass --default-callers-reviewed")
        if self.args.expected_default and previous != self.args.expected_default:
            raise ValueError(f"Default changed concurrently: expected {self.args.expected_default}, found {previous}")
        self.request("PATCH", self.account, {"properties": {"defaultProject": desired}})
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = self.request("GET", self.account)
            project = self.request("GET", f"{self.account}/projects/{desired}")
            if (result["properties"].get("defaultProject") == desired
                    and project["properties"].get("isDefault") is True):
                return {"previous_default": previous, "default_project": desired,
                        "project_is_default": True}
            time.sleep(3)
        raise RuntimeError("Account default or computed project flag did not converge; inspect before retrying")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["inspect", "provision", "default", "rollback"])
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--api-version", default=API_VERSION)
    parser.add_argument("--source", required=True, help="Existing project to copy connections and role grants from")
    parser.add_argument("--target", required=True, help="Project to create or make the account default")
    parser.add_argument("--previous-default")
    parser.add_argument("--expected-default")
    parser.add_argument("--default-callers-reviewed", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Otherwise show the planned change only")
    args = parser.parse_args()
    migration = Migration(args)
    try:
        if args.stage == "inspect":
            result = migration.inspect()
        elif args.stage == "provision":
            result = migration.provision()
        else:
            result = migration.set_default()
        print(json.dumps(result, indent=2))
    finally:
        migration.close()


if __name__ == "__main__":
    main()
