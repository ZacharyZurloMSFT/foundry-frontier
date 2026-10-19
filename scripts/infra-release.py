"""Plan or explicitly apply only the approved private-Blob networking boundary."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
GROUP = "rg-northwind-networking"
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "live" / "storage-networking.bicep"
RESOURCE_NAMES = {
    "Microsoft.Network/privateDnsZones": "privatelink.blob.core.windows.net",
    "Microsoft.Network/privateDnsZones/virtualNetworkLinks": "privatelink.blob.core.windows.net/northwind-storage",
    "Microsoft.Network/privateEndpoints": "pe-zz-storage-northwind",
    "Microsoft.Network/privateEndpoints/privateDnsZoneGroups": "pe-zz-storage-northwind/default",
}


def allowed_ids() -> set[str]:
    base = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/"
    result = set()
    for kind, name in RESOURCE_NAMES.items():
        provider, resource, *children = kind.split("/")
        parts = name.split("/")
        path = f"{provider}/{resource}/{parts[0]}"
        for child, value in zip(children, parts[1:]):
            path += f"/{child}/{value}"
        result.add((base + path).lower())
    return result


def command(*args: str) -> dict:
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI is required for an infrastructure plan.")
    result = subprocess.run([executable, *args, "--output", "json", "--only-show-errors"],
                            check=False, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("Azure operation failed. No successful deployment is implied.")
    return json.loads(result.stdout)


def validate_template(template: dict) -> None:
    resources = template.get("resources")
    if not isinstance(resources, list) or len(resources) != 4:
        raise ValueError("Expected a flat, resource-group scoped template.")
    if {(r.get("type"), r.get("name")) for r in resources} != set(RESOURCE_NAMES.items()):
        # Parameterized names are deliberately forbidden at the execution boundary.
        expected = {
            ("Microsoft.Network/privateDnsZones", "privatelink.blob.core.windows.net"),
            ("Microsoft.Network/privateDnsZones/virtualNetworkLinks", "[format('{0}/{1}', 'privatelink.blob.core.windows.net', 'northwind-storage')]"),
            ("Microsoft.Network/privateEndpoints", "[parameters('privateEndpointName')]"),
            ("Microsoft.Network/privateEndpoints/privateDnsZoneGroups", "[format('{0}/{1}', parameters('privateEndpointName'), 'default')]"),
        }
        if {(r.get("type"), r.get("name")) for r in resources} != expected:
            raise ValueError("Template management boundary changed; review the allowlist before applying.")
    required = {
        "location": "canadacentral", "storageResourceGroup": "rg-northwind-data",
        "storageAccountName": "stzurlonorthwind", "vnetName": "vnet-zurlo-northwind",
        "privateEndpointSubnetName": "private-endpoints", "privateEndpointName": "pe-zz-storage-northwind",
    }
    if {key: p.get("defaultValue") for key, p in template.get("parameters", {}).items()} != required:
        raise ValueError("Live target parameters differ from the approved resource boundary.")


def safe_plan(result: dict) -> dict:
    if result.get("status") not in (None, "Succeeded") or not isinstance(result.get("changes"), list):
        raise ValueError("Infrastructure plan did not complete successfully.")
    changes = []
    for change in result["changes"]:
        kind = change.get("changeType")
        if kind in {"Ignore", "NoChange"}:
            continue
        identifier = change.get("resourceId", "")
        if kind not in {"Create", "Modify"} or identifier.lower() not in allowed_ids():
            raise ValueError("Plan contains an unsupported change or resource; no apply is permitted.")
        changes.append({"resource_id": identifier.lower(), "change_type": kind})
    return {"changes": sorted(changes, key=lambda change: change["resource_id"])}


def make_plan(compiled: Path) -> dict:
    validate_template(json.loads(compiled.read_text(encoding="utf-8")))
    return command(
        "deployment", "group", "what-if", "--subscription", SUBSCRIPTION,
        "--resource-group", GROUP, "--name", "northwind-storage-plan",
        "--template-file", str(compiled), "--mode", "Incremental", "--no-pretty-print",
        "--validation-level", "ProviderNoRbac",
    )


def envelope(compiled: Path, result: dict, sha: str) -> dict:
    plan = safe_plan(result)
    return {
        "source_sha": sha, "subscription": SUBSCRIPTION, "resource_group": GROUP,
        "template_sha256": hashlib.sha256(compiled.read_bytes()).hexdigest(),
        "plan_sha256": hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest(),
        **plan,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiled-template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--approved-plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if len(args.source_sha) != 40 or any(c not in "0123456789abcdef" for c in args.source_sha):
        raise ValueError("An exact reviewed Git commit SHA is required.")
    result = make_plan(args.compiled_template)
    summary = envelope(args.compiled_template, result, args.source_sha)
    if args.apply:
        if not args.approved_plan or os.environ.get("NORTHWIND_INFRA_CONFIRM") != args.source_sha:
            raise ValueError("Apply requires explicit confirmation of the reviewed commit and plan.")
        approved = json.loads(args.approved_plan.read_text(encoding="utf-8"))
        if summary != approved:
            raise ValueError("Infrastructure plan or source changed after review. Generate and review a new plan.")
        command(
            "deployment", "group", "create", "--subscription", SUBSCRIPTION,
            "--resource-group", GROUP, "--name", f"northwind-storage-{args.source_sha[:12]}",
            "--template-file", str(args.compiled_template), "--mode", "Incremental",
        )
        state = command(
            "network", "private-endpoint", "show", "--subscription", SUBSCRIPTION,
            "--resource-group", GROUP, "--name", "pe-zz-storage-northwind",
        )
        connections = state.get("privateLinkServiceConnections", [])
        if state.get("provisioningState") != "Succeeded" or not connections or any(
            c.get("privateLinkServiceConnectionState", {}).get("status") != "Approved" for c in connections
        ):
            raise RuntimeError("Private endpoint readiness/approval is not confirmed.")
        summary["applied"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
