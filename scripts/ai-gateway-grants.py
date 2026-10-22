"""Plan, apply and revoke AI Gateway data-plane roles, and discover hosted agent identities.

Scope is always the gateway resource. `plan` lists what would be created; `apply --yes` creates only the
missing assignments (deterministic names, so a rerun changes nothing) and writes a receipt listing exactly
what it created; `revoke --receipt FILE --yes` deletes only the assignments recorded in that receipt, so
pre-existing assignments are never touched. `discover-agents` prints a principals file for the hosted agents
of a Foundry project.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _load_common():
    module = sys.modules.get("ai_gateway_common")
    if module is None:
        spec = importlib.util.spec_from_file_location(
            "ai_gateway_common", Path(__file__).resolve().with_name("ai_gateway_common.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["ai_gateway_common"] = module
        spec.loader.exec_module(module)
    return module


common = _load_common()

PLAN_KIND = "ai-gateway-grants-plan"
RECEIPT_KIND = "ai-gateway-grants-receipt"
AGENTS_SCOPE = "https://ai.azure.com/.default"
AGENTS_API_VERSION = "v1"
AGENT_PAGE_LIMIT = 20
INACTIVE_STATES = ("failed", "deleting", "deleted")

EPILOG = """\
required access:
  plan             Reader on the gateway (lists role assignments).
  discover-agents  A data-plane read on the Foundry project (Foundry User / Azure AI User on the project).
  apply, revoke    Microsoft.Authorization/roleAssignments/write and delete on the gateway, for example
                   User Access Administrator or Role Based Access Control Administrator.
Sign in with `az login` first. The Azure CLI acquires every token; this script never sees one.

principals file (--principals): a JSON list
  [{"id": "<object id>", "type": "ServicePrincipal" | "User", "label": "free text", "roles": ["models", "tools"]}]
  models = AI Gateway Models User (a6b68e3d-a1f0-4c60-8678-237c050cf4ea)
  tools  = AI Gateway Tools User  (dce03f8d-857f-4e40-a90d-67770fa6ecd2)
--preset app-uami is the app's user-assigned identity (4345f6c9-3c1a-4f4f-9d58-e75d7fa6ee17) with the models role
only: the API app calls models, not tool servers, through the gateway (least privilege).

typical flow:
  python scripts/ai-gateway-grants.py discover-agents --project-endpoint <endpoint> --out principals.json
  python scripts/ai-gateway-grants.py plan --principals principals.json
  python scripts/ai-gateway-grants.py apply --principals principals.json --yes --receipt grants-receipt.json
  python scripts/ai-gateway-grants.py revoke --receipt grants-receipt.json --yes
"""


def desired_grants(principals: list[dict]) -> list[dict]:
    rows = []
    for principal in principals:
        for key in principal["roles"]:
            role = common.GATEWAY_ROLES[key]
            rows.append({
                "principalId": principal["id"], "principalType": principal["type"], "label": principal["label"],
                "role": key, "roleId": role, "roleName": common.ROLE_NAMES[role],
                "roleDefinitionId": common.role_definition_id(role),
                "assignmentName": common.assignment_name(common.GATEWAY_ID, principal["id"], role),
            })
    return sorted(rows, key=lambda row: (row["principalId"], row["role"]))


def plan_grants(runner, principals: list[dict]) -> dict:
    existing = common.list_role_assignments(runner, common.GATEWAY_ID)
    grants = []
    for row in desired_grants(principals):
        matches = [item for item in existing
                   if item["principalId"] == row["principalId"] and item["roleId"] == row["roleId"]
                   and common.scope_covers(item["scope"], common.GATEWAY_ID)]
        grants.append({**row, "state": "already-present" if matches else "would-create",
                       "presentAt": sorted({item["scope"] for item in matches})})
    return {
        "schemaVersion": 1, "kind": PLAN_KIND, "scope": common.GATEWAY_ID, "grants": grants,
        "summary": {"wouldCreate": sum(1 for row in grants if row["state"] == "would-create"),
                    "alreadyPresent": sum(1 for row in grants if row["state"] == "already-present")},
    }


def _entry(row: dict) -> dict:
    return {"principalId": row["principalId"], "principalType": row["principalType"], "label": row["label"],
            "role": row["role"], "roleId": row["roleId"], "scope": common.GATEWAY_ID,
            "assignmentName": row["assignmentName"]}


def _present(row: dict, scopes: list[str], **extra) -> dict:
    """An assignment this tool did not create; it is informational and never revocable from the receipt."""
    return {"principalId": row["principalId"], "label": row["label"], "role": row["role"], "roleId": row["roleId"],
            "presentAt": scopes, **extra}


def apply_grants(runner, principals: list[dict], receipt_path: Path, *, now) -> dict:
    plan = plan_grants(runner, principals)
    receipt = {
        "schemaVersion": 1, "kind": RECEIPT_KIND, "scope": common.GATEWAY_ID, "createdAt": common.iso(now()),
        "status": "in-progress", "created": [],
        "alreadyPresent": [_present(row, row["presentAt"]) for row in plan["grants"] if row["state"] == "already-present"],
        "failed": [],
    }
    # The receipt exists before the first write so an interrupted run still records what it created.
    common.write_json_exclusive(receipt_path, receipt)
    for row in plan["grants"]:
        if row["state"] != "would-create":
            continue
        try:
            outcome = common.put_role_assignment(
                runner, common.GATEWAY_ID, row["assignmentName"], row["principalId"], row["principalType"], row["roleId"])
        except common.GatewayError as error:
            receipt["status"] = "failed"
            receipt["failed"].append({**_entry(row), "error": str(error)})
            common.replace_json(receipt_path, receipt)
            raise common.GatewayError(f"{error} The receipt lists what was created so far: {receipt_path}") from None
        if outcome == "created":
            receipt["created"].append(_entry(row))
        else:
            receipt["alreadyPresent"].append(_present(row, [common.GATEWAY_ID], assignmentName=row["assignmentName"],
                                                      note="created concurrently by another writer"))
        common.replace_json(receipt_path, receipt)
    receipt["status"] = "complete"
    common.replace_json(receipt_path, receipt)
    return receipt


def validate_receipt(receipt) -> None:
    valid = (isinstance(receipt, dict) and receipt.get("kind") == RECEIPT_KIND and receipt.get("schemaVersion") == 1
             and receipt.get("scope") == common.GATEWAY_ID and isinstance(receipt.get("created"), list))
    if not valid:
        raise common.GatewayError("The file is not a grants receipt for this gateway.")
    for index, entry in enumerate(receipt["created"]):
        role = entry.get("role") if isinstance(entry, dict) else None
        valid = (role in common.GATEWAY_ROLES and entry.get("roleId") == common.GATEWAY_ROLES[role]
                 and entry.get("scope") == common.GATEWAY_ID and common.is_guid(entry.get("principalId"))
                 and entry.get("assignmentName") == common.assignment_name(
                     common.GATEWAY_ID, entry["principalId"], entry["roleId"]))
        if not valid:
            raise common.GatewayError(
                f"created[{index}] was not produced by this tool (scope, role or name mismatch); nothing was deleted.")


def revoke_grants(runner, receipt: dict) -> dict:
    validate_receipt(receipt)
    pending = []
    for entry in receipt["created"]:
        live = common.get_role_assignment(runner, entry["scope"], entry["assignmentName"])
        if live is None:
            pending.append((entry, False))
        elif (live["principalId"] != entry["principalId"] or live["roleId"] != entry["roleId"]
              or live["scope"].lower() != entry["scope"].lower()):
            raise common.GatewayError(
                "A recorded assignment no longer matches the receipt; nothing was deleted. Review it manually.")
        else:
            pending.append((entry, True))
    results = []
    for entry, present in pending:
        deleted = present and common.delete_role_assignment(runner, entry["scope"], entry["assignmentName"])
        results.append({**entry, "outcome": "deleted" if deleted else "already-absent"})
    return {"schemaVersion": 1, "kind": "ai-gateway-grants-revoke", "scope": common.GATEWAY_ID, "results": results}


def validate_project_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    valid = (parts.scheme == "https" and bool(parts.hostname) and parts.hostname.endswith(".services.ai.azure.com")
             and not parts.username and not parts.password and not parts.query and not parts.fragment
             and re.fullmatch(r"/api/projects/[A-Za-z0-9._-]+/?", parts.path) is not None)
    if not valid:
        raise common.GatewayError("--project-endpoint must look like https://<account>.services.ai.azure.com/api/projects/<project>.")
    return f"https://{parts.hostname}{parts.path.rstrip('/')}"


def discover_agents(runner, endpoint: str, roles: list[str]) -> tuple[list[dict], list[str]]:
    base = validate_project_endpoint(endpoint)
    agents, after = [], None
    for _ in range(AGENT_PAGE_LIMIT):
        params = {"api-version": AGENTS_API_VERSION, "kind": "hosted", "limit": "100"}
        if after:
            params["after"] = after
        body = runner("GET", f"{base}/agents", params=params, scope=AGENTS_SCOPE).body
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise common.GatewayError("Unexpected agents response from the Foundry project.")
        agents += [agent for agent in body["data"] if isinstance(agent, dict)]
        after = body.get("last_id")
        if not body.get("has_more") or not after:
            break
    else:
        raise common.GatewayError("The agents list exceeded the page limit.")
    principals, skipped, seen = [], [], set()
    for agent in sorted(agents, key=lambda item: str(item.get("name"))):
        latest = (agent.get("versions") or {}).get("latest") or {}
        kind = (latest.get("definition") or {}).get("kind")
        if kind not in (None, "hosted"):
            continue
        name = str(agent.get("name"))
        principal = (latest.get("instance_identity") or agent.get("instance_identity") or {}).get("principal_id")
        if latest.get("status") in INACTIVE_STATES or not common.is_guid(principal):
            skipped.append(f"{name}: no usable runtime principal on the latest version")
        elif principal.lower() in seen:
            skipped.append(f"{name}: shares a principal with another agent")
        else:
            seen.add(principal.lower())
            principals.append({"id": principal.lower(), "type": "ServicePrincipal",
                               "label": f"{name}:v{latest.get('version')}", "roles": roles})
    return (common.validate_principals(principals) if principals else []), skipped


def _roles(text: str) -> list[str]:
    roles = [item.strip() for item in text.split(",") if item.strip()]
    if not roles or any(role not in common.GATEWAY_ROLES for role in roles):
        raise argparse.ArgumentTypeError(f"roles must be a comma-separated list drawn from {', '.join(common.GATEWAY_ROLES)}")
    return [role for role in common.GATEWAY_ROLES if role in roles]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-gateway-grants.py", description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def with_principals(command):
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument("--principals", type=Path, help="JSON file listing the principals and roles to grant.")
        group.add_argument("--preset", choices=common.PRESETS, help="Built-in principal list.")

    plan = commands.add_parser("plan", help="Show which assignments would be created (read-only).")
    with_principals(plan)
    apply = commands.add_parser("apply", help="Create only the missing assignments and write a receipt.")
    with_principals(apply)
    apply.add_argument("--yes", action="store_true", help="Required to create role assignments.")
    apply.add_argument("--receipt", type=Path,
                       help="Receipt path (never overwritten). Default: .release-state/ai-gateway-grants-<UTC time>.json")
    revoke = commands.add_parser("revoke", help="Delete only the assignments recorded in a receipt.")
    revoke.add_argument("--receipt", type=Path, required=True)
    revoke.add_argument("--yes", action="store_true", help="Required to delete role assignments.")
    discover = commands.add_parser("discover-agents", help="Print a principals file for the hosted agents of a project.")
    discover.add_argument("--project-endpoint", required=True,
                          help="https://<account>.services.ai.azure.com/api/projects/<project>")
    discover.add_argument("--roles", type=_roles, default=["models"],
                          help="Roles for every discovered agent, comma-separated (default: models).")
    discover.add_argument("--out", type=Path, help="Write the principals JSON here instead of stdout.")
    return parser


def main(argv=None, *, runner=None, now=None) -> int:
    args = build_parser().parse_args(argv)
    now = now or common.utc_now
    try:
        if args.command in ("apply", "revoke") and not args.yes:
            raise common.GatewayError("Refusing to change role assignments without --yes; run `plan` first.")
        runner = runner or common.AzRestRunner()
        if args.command == "discover-agents":
            principals, skipped = discover_agents(runner, args.project_endpoint, args.roles)
            for line in skipped:
                print(f"skipped {line}", file=sys.stderr)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                common.replace_json(args.out, principals)
            else:
                print(common.pretty_json(principals), end="")
            return 0
        if args.command == "revoke":
            result = revoke_grants(runner, common.read_json(args.receipt))
        else:
            principals = common.load_principals(args.principals) if args.principals else common.preset_principals(args.preset)
            if args.command == "plan":
                result = plan_grants(runner, principals)
            else:
                result = apply_grants(runner, principals, args.receipt or common.default_receipt_path("grants", now()), now=now)
        print(common.pretty_json(result), end="")
        return 0
    except (common.GatewayError, OSError, ValueError) as error:
        print(f"AI Gateway grants refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
