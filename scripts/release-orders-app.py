"""Update one existing Container App, preserving unrelated configuration."""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

import httpx
from azure.identity import AzureCliCredential, get_bearer_token_provider

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.app.company import _try_parse_endpoints_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--app", choices=["ca-api", "ca-web-tech"], required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--project-endpoint")
    parser.add_argument("--rollback", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        raise ValueError("Release image must be pinned by digest")
    credential = AzureCliCredential(tenant_id=args.tenant, process_timeout=120)
    token = get_bearer_token_provider(credential, "https://management.azure.com/.default")
    resource = (f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}"
                f"/providers/Microsoft.App/containerApps/{args.app}")
    url = f"https://management.azure.com{resource}"
    version = {"api-version": "2024-03-01"}
    try:
        with httpx.Client(timeout=120) as http:
            def read():
                response = http.get(url, params=version, headers={"Authorization": f"Bearer {token()}"})
                response.raise_for_status()
                return response.json()

            before = read()
            if before["properties"]["latestReadyRevisionName"] != args.expected_revision:
                raise ValueError("The active app revision changed; review before applying a release")
            template = copy.deepcopy(before["properties"]["template"])
            if len(template["containers"]) != 1:
                raise ValueError("Expected exactly one container; refusing to replace a multi-container app")
            container = template["containers"][0]
            previous_image = container["image"]
            changes = {}
            env = {entry["name"]: entry for entry in container.get("env", [])}
            if args.app == "ca-api":
                if not args.project_endpoint or not args.project_endpoint.endswith("/projects/northwind-orders"):
                    raise ValueError("An explicit northwind-orders endpoint is required")
                endpoint_map = _try_parse_endpoints_json(env.get("FOUNDRY_PROJECT_ENDPOINTS", {}).get("value", ""))
                if endpoint_map is None:
                    raise ValueError("Cannot safely preserve the current department endpoint map")
                endpoint_map["order-concierge"] = args.project_endpoint
                changes = {
                    "FOUNDRY_PROJECT_ENDPOINT": args.project_endpoint,
                    "FOUNDRY_PROJECT_ENDPOINTS": json.dumps(endpoint_map, separators=(",", ":")),
                    "ORDER_PROJECT_NAME": "northwind-orders",
                    "ORDER_AGENTS_HOSTED": "true",
                    "COPILOT_HARNESS_ENABLED": "false",
                }
            rollback = {
                "app": args.app, "resource_id": resource,
                "previous_image": previous_image, "previous_revision": args.expected_revision,
                "previous_environment": {name: env.get(name) for name in changes},
                "release_image": args.image,
            }
            if args.rollback.exists():
                raise ValueError("Rollback record already exists; use a new release record")
            if not args.apply:
                print(json.dumps({"planned": args.app, "image": args.image,
                                  "changed_environment_names": list(changes)}, indent=2))
                return
            args.rollback.parent.mkdir(parents=True, exist_ok=True)
            args.rollback.write_text(json.dumps(rollback, indent=2), encoding="utf-8")
            for name, value in changes.items():
                env[name] = {"name": name, "value": value}
            container["env"] = list(env.values())
            container["image"] = args.image
            template["revisionSuffix"] = args.revision
            response = http.patch(
                url, params=version, headers={"Authorization": f"Bearer {token()}"},
                json={"properties": {"template": template}},
            )
            response.raise_for_status()
            expected = f"{args.app}--{args.revision}"
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                current = read()["properties"]
                if (current.get("latestReadyRevisionName") == expected
                        and current.get("provisioningState") == "Succeeded"):
                    print(json.dumps({"app": args.app, "revision": expected,
                                      "state": "ready", "image": args.image,
                                      "url": current["configuration"]["ingress"]["fqdn"]}, indent=2))
                    return
                if current.get("provisioningState") == "Failed":
                    raise RuntimeError("Release provisioning failed; rollback record preserved")
                time.sleep(5)
            raise TimeoutError("New app revision did not become ready; rollback record preserved")
    finally:
        credential.close()


if __name__ == "__main__":
    main()
