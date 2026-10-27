"""Audit direct-access exposure of the Foundry account and remove redundant direct-inference grants.

`audit` is read-only. It reports the account's network and key settings, which in-scope identities can still
call the account directly, how exposed the MCP container apps are, and ready-to-use KQL for detecting
inference that bypasses the gateway. `plan` / `apply --yes` remove only the account-scope Cognitive Services
OpenAI User assignments of the listed hosted-agent service principals, after verifying that each already
holds AI Gateway Models User on the gateway, and write a rollback receipt. `rollback --receipt FILE --yes`
recreates exactly those assignments.

RBAC alone cannot force gateway-only inference: Foundry User (Azure AI User) includes
Microsoft.CognitiveServices/* and the hosted agents need it, so it is never removed. MCP container-app
lockdown and network ACL changes are NOT implemented here; the audit only recommends them and they remain a
gated manual decision.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


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

AUDIT_KIND = "ai-gateway-enforce-audit"
PLAN_KIND = "ai-gateway-enforce-plan"
RECEIPT_KIND = "ai-gateway-enforce-receipt"
ACCOUNT_API_VERSION = "2025-06-01"
APPS_API_VERSION = "2024-03-01"
DIAGNOSTICS_API_VERSION = "2021-05-01-preview"
APPS_RESOURCE_GROUP = "rg-northwind-apps"
MCP_APPS = ("ca-insurance-lookup", "ca-insurance-review")
# The claim-review server is the one the gateway cannot front: it needs the workflow-binding header (see MCP_RECOMMENDATIONS).
CLAIM_REVIEW_APPS = ("ca-insurance-review",)
ENFORCED_AUTH_ACTIONS = ("Return401", "Return403", "RedirectToLoginPage")
INFERENCE_ROLES = {
    common.ROLE_OPENAI_USER: "Cognitive Services OpenAI User",
    common.ROLE_FOUNDRY_USER: "Foundry User (Azure AI User)",
    "a97b65f3-24c7-4388-baec-2e87135dc908": "Cognitive Services User",
    "64702f94-c441-49e6-a78b-ef80e0188fee": "Azure AI Developer",
    "a001fd3d-188f-4b5d-821b-7da978bf7442": "Cognitive Services OpenAI Contributor",
}
ACCOUNT_LOWER = common.FOUNDRY_ACCOUNT_ID.lower()


def is_account_scope(scope) -> bool:
    """ARM resource ids are case-insensitive, so the live casing of an assignment scope can differ."""
    return isinstance(scope, str) and scope.lower() == ACCOUNT_LOWER

LIMITS = [
    (
        "Foundry User (Azure AI User) includes Microsoft.CognitiveServices/*, so it still permits direct inference. "
        "The hosted agents need it, so it is never removed: RBAC alone cannot make the gateway the only inference path."
    ),
    (
        "Enforcement here means code that fails closed (AI_GATEWAY_REQUIRED), removing the redundant account-scope "
        "Cognitive Services OpenAI User from hosted-agent service principals that already hold AI Gateway Models User, "
        "and detecting bypass with the KQL in this report."
    ),
    (
        "Network ACL changes and MCP container-app lockdown are not implemented by this tool. They are a gated manual "
        "decision and the audit only recommends them."
    ),
]
MCP_RECOMMENDATIONS = [
    (
        "Require Entra authentication (Container Apps authentication) on each MCP app and accept only the gateway "
        f"identity {common.GATEWAY_IDENTITY_PRINCIPAL_ID}; then change the tool server credential from none to "
        "managedIdentity so the gateway can still call the backend."
    ),
    (
        "Add ingress IP restrictions for the gateway's outbound addresses once they are known; the gateway currently "
        "publishes no static outbound addresses."
    ),
    (
        "The claim-review MCP app (ca-insurance-review) stays direct because the gateway does not forward the "
        "workflow-binding header (x-workflow-id, or the deprecated x-orders-workflow); protect it with its own "
        "caller identity, not the gateway path."
    ),
    "These are gated manual decisions. This tool never changes container apps, ingress or network settings.",
]
KQL_NOTES = [
    (
        "All four queries were run against Log Analytics workspace log-zurlo-northwind on 2026-10-22. The account's "
        "RequestResponse rows land in AzureDiagnostics; the caller's object id is in properties_s (objectId, "
        "callerObjectId) and inference operations are named like create-response, so the filter is a case-insensitive "
        "regex rather than a list of operation names."
    ),
    "The account's diagnostic settings must send RequestResponse (or the allLogs group) to Log Analytics.",
    (
        "If caller comes back empty, inspect the identity fields with: "
        "AzureDiagnostics | where ResourceProvider == 'MICROSOFT.COGNITIVESERVICES' | take 5. An empty caller means the "
        "workspace does not log caller identity; compare CallerIPAddress against the gateway's outbound addresses instead."
    ),
]

EPILOG = """\
required access:
  audit             Reader on the Foundry account and its projects, the gateway and the resource group of the
                    MCP container apps, and Microsoft.Authorization/roleAssignments/read; diagnostic settings
                    are read when permitted.
  plan              Microsoft.Authorization/roleAssignments/read at the gateway and the account.
  apply, rollback   Microsoft.Authorization/roleAssignments/write and delete on the account, for example
                    User Access Administrator or Role Based Access Control Administrator.
Sign in with `az login` first. The Azure CLI acquires every token; this script never sees one.

principals file (--principals): the same JSON list the grants script uses
  [{"id": "<object id>", "type": "ServicePrincipal" | "User", "label": "free text", "roles": ["models"]}]
apply and plan act on ServicePrincipal entries only. Users, the app user-assigned identity and the gateway
identity are always skipped, and a service principal without AI Gateway Models User on the gateway blocks
the whole apply.

typical flow:
  python scripts/ai-gateway-enforce.py audit --principals principals.json --out audit.json
  python scripts/ai-gateway-enforce.py plan --principals principals.json
  python scripts/ai-gateway-enforce.py apply --principals principals.json --yes --receipt enforce-receipt.json
  python scripts/ai-gateway-enforce.py rollback --receipt enforce-receipt.json --yes
"""


def _finding(severity: str, message: str) -> dict:
    return {"severity": severity, "message": message}


def scope_kind(scope: str) -> str:
    lowered = scope.lower()
    if lowered == ACCOUNT_LOWER:
        return "account"
    if lowered.startswith(ACCOUNT_LOWER + "/projects/"):
        return "project"
    return "inherited" if common.scope_covers(scope, common.FOUNDRY_ACCOUNT_ID) else "other"


def classify_principal(principal: dict) -> tuple[bool, str]:
    """Whether enforcement may touch this principal, and why not."""
    if principal["type"] != "ServicePrincipal":
        return False, "only service principals are changed; users keep their access"
    if principal["id"] == common.APP_UAMI_PRINCIPAL_ID:
        return False, "the app user-assigned identity keeps its direct access"
    if principal["id"] == common.GATEWAY_IDENTITY_PRINCIPAL_ID:
        return False, "the gateway identity needs its access to call the account"
    return True, ""


def holds_models_user(gateway_rows: list[dict], principal_id: str) -> bool:
    return any(row["principalId"] == principal_id and row["roleId"] == common.ROLE_MODELS_USER
               and common.scope_covers(row["scope"], common.GATEWAY_ID) for row in gateway_rows)


def audit_account(runner) -> dict:
    body = runner("GET", f"{common.ARM_ENDPOINT}{common.FOUNDRY_ACCOUNT_ID}",
                  params={"api-version": ACCOUNT_API_VERSION}).body
    if not isinstance(body, dict):
        raise common.GatewayError("Unexpected account response.")
    properties = body.get("properties") or {}
    acls = properties.get("networkAcls") or {}
    public, local_auth_disabled = properties.get("publicNetworkAccess"), properties.get("disableLocalAuth")
    findings = []
    if local_auth_disabled is True:
        findings.append(_finding("info", "Key authentication is disabled: only Entra identities can call the account."))
    else:
        findings.append(_finding("high", "Key authentication is enabled: any key holder can call the account without the gateway."))
    if public != "Disabled" and acls.get("defaultAction") != "Deny":
        findings.append(_finding(
            "warning", "The account accepts calls from any network, so the network does not force traffic through "
                       "the gateway. Restricting it is a separate, gated decision."))
    return {
        "id": common.FOUNDRY_ACCOUNT_ID, "kind": body.get("kind"), "publicNetworkAccess": public,
        "disableLocalAuth": local_auth_disabled,
        "networkAcls": {"defaultAction": acls.get("defaultAction"), "ipRules": len(acls.get("ipRules") or []),
                        "virtualNetworkRules": len(acls.get("virtualNetworkRules") or [])},
        "privateEndpointConnections": len(properties.get("privateEndpointConnections") or []),
        "findings": findings,
    }


def audit_projects(runner) -> list[dict]:
    items = common.list_arm(runner, f"{common.ARM_ENDPOINT}{common.FOUNDRY_ACCOUNT_ID}/projects",
                            {"api-version": ACCOUNT_API_VERSION})
    return sorted(({"name": str(item.get("name", "")).rsplit("/", 1)[-1], "id": item.get("id")}
                   for item in items if item.get("id")), key=lambda row: row["name"])


def audit_identities(runner, principals: list[dict], project_ids: list[str]) -> dict:
    assignments = {}
    for scope in (common.FOUNDRY_ACCOUNT_ID, *project_ids):
        for row in common.list_role_assignments(runner, scope):
            assignments[(row["scope"].lower(), row["name"].lower())] = row
    gateway_rows = common.list_role_assignments(runner, common.GATEWAY_ID)
    known = {principal["id"] for principal in principals}
    identities = []
    for principal in principals:
        eligible, _ = classify_principal(principal)
        direct = []
        for row in sorted(assignments.values(), key=lambda item: (item["scope"].lower(), item["name"])):
            if row["principalId"] != principal["id"] or row["roleId"] not in INFERENCE_ROLES:
                continue
            kind = scope_kind(row["scope"])
            direct.append({
                "role": INFERENCE_ROLES[row["roleId"]], "roleId": row["roleId"], "scope": row["scope"],
                "scopeKind": kind, "assignmentName": row["name"],
                "removableByEnforce": eligible and row["roleId"] == common.ROLE_OPENAI_USER and kind == "account",
            })
        notes = []
        if any(item["roleId"] == common.ROLE_FOUNDRY_USER for item in direct):
            notes.append("Foundry User cannot be removed because the agent needs it, and it still permits direct "
                         "inference (Microsoft.CognitiveServices/*): RBAC alone cannot enforce the gateway path.")
        identities.append({
            "principalId": principal["id"], "label": principal["label"], "type": principal["type"],
            "gatewayRoles": {
                "models": holds_models_user(gateway_rows, principal["id"]),
                "tools": any(row["principalId"] == principal["id"] and row["roleId"] == common.ROLE_TOOLS_USER
                             and common.scope_covers(row["scope"], common.GATEWAY_ID) for row in gateway_rows),
            },
            "directInference": direct, "canInferWithoutGateway": bool(direct), "notes": notes,
        })
    others = [{
        "principalId": row["principalId"], "principalType": row["principalType"], "role": INFERENCE_ROLES[row["roleId"]],
        "scope": row["scope"], "scopeKind": scope_kind(row["scope"]),
        "isGatewayIdentity": row["principalId"] == common.GATEWAY_IDENTITY_PRINCIPAL_ID,
    } for row in sorted(assignments.values(), key=lambda item: (item["principalId"], item["scope"].lower(), item["roleId"]))
        if row["principalId"] not in known and row["roleId"] in INFERENCE_ROLES
        and scope_kind(row["scope"]) in ("account", "project")]
    return {"identities": identities, "otherPrincipals": others}


def mcp_exposure(ingress: dict, auth: dict) -> str:
    if not ingress:
        return "no-ingress"
    if not ingress.get("external"):
        return "internal-only"
    if auth.get("enforced"):
        return "public-authenticated"
    if ingress.get("ipSecurityRestrictions"):
        return "public-ip-restricted"
    return "public-unauthenticated"


def _auth_config(runner, app_id: str) -> dict:
    try:
        body = runner("GET", f"{common.ARM_ENDPOINT}{app_id}/authConfigs/current",
                      params={"api-version": APPS_API_VERSION}).body
    except common.RunnerError as error:
        if error.status == 404:
            return {"configured": False, "enforced": False}
        raise
    properties = (body or {}).get("properties") or {}
    action = (properties.get("globalValidation") or {}).get("unauthenticatedClientAction")
    enabled = bool((properties.get("platform") or {}).get("enabled"))
    return {"configured": True, "enabled": enabled, "unauthenticatedClientAction": action,
            "enforced": enabled and action in ENFORCED_AUTH_ACTIONS}


def audit_mcp_apps(runner) -> list[dict]:
    url = (f"{common.ARM_ENDPOINT}/subscriptions/{common.SUBSCRIPTION}/resourceGroups/{APPS_RESOURCE_GROUP}"
           "/providers/Microsoft.App/containerApps")
    apps = {str(item.get("name")): item for item in common.list_arm(runner, url, {"api-version": APPS_API_VERSION})}
    reports = []
    for name in MCP_APPS:
        app = apps.get(name)
        if app is None:
            reports.append({"name": name, "found": False})
            continue
        ingress = ((app.get("properties") or {}).get("configuration") or {}).get("ingress") or {}
        auth = _auth_config(runner, app["id"]) if app.get("id") else {"configured": False, "enforced": False}
        reports.append({
            "name": name, "found": True, "kind": "claim-review" if name in CLAIM_REVIEW_APPS else "lookup",
            "fqdn": ingress.get("fqdn"), "externalIngress": bool(ingress.get("external")),
            "ipRestrictions": len(ingress.get("ipSecurityRestrictions") or []), "authentication": auth,
            "exposure": mcp_exposure(ingress, auth),
        })
    return reports


def _diagnostic_settings(runner, resource_id: str) -> dict:
    try:
        items = common.list_arm(runner, f"{common.ARM_ENDPOINT}{resource_id}/providers/Microsoft.Insights/diagnosticSettings",
                                {"api-version": DIAGNOSTICS_API_VERSION})
    except common.GatewayError as error:
        return {"status": "unavailable", "error": str(error), "settings": []}
    settings = []
    for item in items:
        properties = item.get("properties") or {}
        logs = {log.get("category") or log.get("categoryGroup") for log in properties.get("logs") or [] if log.get("enabled")}
        settings.append({"name": item.get("name"), "workspaceId": properties.get("workspaceId"),
                         "enabledLogs": sorted(name for name in logs if name)})
    return {"status": "ok", "settings": settings}


def kql_queries(principal_ids: list[str]) -> dict:
    gateway = common.GATEWAY_IDENTITY_PRINCIPAL_ID
    account = common.FOUNDRY_ACCOUNT_NAME
    ids = ", ".join(f'"{principal}"' for principal in sorted(principal_ids))
    caller = ('extend props = parse_json(tostring(column_ifexists("properties_s", "")))\n'
              "| extend caller = coalesce(\n"
              "    tostring(props.objectId), tostring(props.callerObjectId),\n"
              '    tostring(column_ifexists("identity_claim_oid_g", "")),\n'
              '    tostring(column_ifexists("identity_claim_appid_g", "")),\n'
              '    tostring(parse_json(tostring(column_ifexists("identity_s", ""))).oid))')
    account_scope = (f'| where ResourceProvider == "MICROSOFT.COGNITIVESERVICES" and _ResourceId has "{account}"\n'
                     '| where Category == "RequestResponse"\n'
                     '| where OperationName matches regex "(?i)completion|embedding|response"')
    return {
        "foundryNotFromGateway": (
            "// Inference calls to the Foundry account made by anything other than the AI Gateway identity.\n"
            f'let gatewayPrincipal = "{gateway}";\n'
            "AzureDiagnostics\n| where TimeGenerated > ago(24h)\n" + account_scope + "\n| " + caller + "\n"
            "| where caller != gatewayPrincipal\n"
            "| summarize calls = count(), firstSeen = min(TimeGenerated), lastSeen = max(TimeGenerated)"
            " by caller, CallerIPAddress, OperationName\n| order by calls desc"),
        "inScopeIdentitiesBypassing": (
            "// Direct inference by the in-scope identities (agents and the app identity) instead of the gateway.\n"
            f"let inScope = dynamic([{ids}]);\n"
            "AzureDiagnostics\n| where TimeGenerated > ago(24h)\n" + account_scope + "\n| " + caller + "\n"
            "| where caller in (inScope)\n"
            "| summarize calls = count(), lastSeen = max(TimeGenerated) by caller, CallerIPAddress, OperationName\n"
            "| order by lastSeen desc"),
        "gatewayTraffic": (
            "// Requests that reached the AI Gateway, by route. Model calls log to ApiManagementGatewayLlmLog and MCP\n"
            "// calls to ApiManagementGatewayMCPLog; ApiManagementGatewayLogs had no rows for either when checked.\n"
            f'let gateway = "{common.GATEWAY_NAME}";\n'
            "union isfuzzy=true\n"
            '    (ApiManagementGatewayLlmLog | where TimeGenerated > ago(24h) and _ResourceId has gateway\n'
            '     | project TimeGenerated, route = "models", target = tostring(DeploymentName), failed = bool(null)),\n'
            '    (ApiManagementGatewayMCPLog | where TimeGenerated > ago(24h) and _ResourceId has gateway\n'
            '     | project TimeGenerated, route = "mcp", target = tostring(McpServerEndpoint), failed = isnotempty(Error)),\n'
            '    (ApiManagementGatewayLogs | where TimeGenerated > ago(24h) and _ResourceId has gateway\n'
            '     | project TimeGenerated, route = "other", target = tostring(Url), failed = toint(ResponseCode) >= 400)\n'
            "| summarize calls = count(), failures = countif(failed) by route, target, bin(TimeGenerated, 1h)\n"
            "| order by TimeGenerated desc"),
        "gatewayModelUsage": (
            "// Token usage per model through the gateway (needs gateway LLM logging).\n"
            "ApiManagementGatewayLlmLog\n"
            f'| where TimeGenerated > ago(24h) and _ResourceId has "{common.GATEWAY_NAME}"\n'
            "| summarize calls = count(), promptTokens = sum(PromptTokens), completionTokens = sum(CompletionTokens)"
            " by ModelName, bin(TimeGenerated, 1h)\n| order by TimeGenerated desc"),
    }


def audit_detection(runner, principal_ids: list[str]) -> dict:
    account = _diagnostic_settings(runner, common.FOUNDRY_ACCOUNT_ID)
    gateway = _diagnostic_settings(runner, common.GATEWAY_ID)
    ready = any(setting["workspaceId"] and setting["enabledLogs"] for setting in account["settings"])
    return {"diagnosticSettings": {"account": account, "gateway": gateway}, "bypassDetectionReady": ready,
            "kql": kql_queries(principal_ids), "kqlNotes": KQL_NOTES}


def audit_principals(extra: list[dict] | None) -> list[dict]:
    merged = {principal["id"]: principal for principal in common.preset_principals("app-uami")}
    merged.update({principal["id"]: principal for principal in extra or []})
    return sorted(merged.values(), key=lambda principal: principal["id"])


def run_audit(runner, principals: list[dict] | None, *, now) -> dict:
    scoped = audit_principals(principals)
    errors = []

    def section(name: str, function, fallback):
        try:
            return function()
        except common.GatewayError as error:
            errors.append({"section": name, "error": str(error)})
            return fallback

    account = section("account", lambda: audit_account(runner), {"status": "unavailable"})
    projects = section("projects", lambda: audit_projects(runner), [])
    access = section("identities", lambda: audit_identities(runner, scoped, [row["id"] for row in projects]),
                     {"identities": [], "otherPrincipals": []})
    mcp_apps = section("mcpApps", lambda: audit_mcp_apps(runner), [])
    detection = section("detection", lambda: audit_detection(runner, [principal["id"] for principal in scoped]),
                        {"kql": kql_queries([principal["id"] for principal in scoped]), "kqlNotes": KQL_NOTES,
                         "bypassDetectionReady": False})
    removable = sum(1 for identity in access["identities"] for item in identity["directInference"] if item["removableByEnforce"])
    recommendations = []
    if removable:
        recommendations.append(f"{removable} account-scope Cognitive Services OpenAI User assignment(s) can be removed: "
                               "run `plan --principals <file>` to review them.")
    if not detection["bypassDetectionReady"]:
        recommendations.append("Send the account's RequestResponse logs to Log Analytics so the bypass KQL can run.")
    if any(row.get("exposure") == "public-unauthenticated" for row in mcp_apps):
        recommendations.append("MCP container apps are public and unauthenticated: see mcpRecommendations "
                               "(a gated manual decision).")
    return {
        "schemaVersion": 1, "kind": AUDIT_KIND, "generatedAt": common.iso(now()), "gateway": common.GATEWAY_ID,
        "complete": not errors, "errors": errors, "limits": LIMITS, "account": account, "projects": projects,
        "identities": access["identities"], "otherPrincipals": access["otherPrincipals"], "mcpApps": mcp_apps,
        "mcpRecommendations": MCP_RECOMMENDATIONS, "detection": detection, "recommendations": recommendations,
    }


def plan_enforcement(runner, principals: list[dict]) -> dict:
    gateway_rows = common.list_role_assignments(runner, common.GATEWAY_ID)
    account_rows = common.list_role_assignments(runner, common.FOUNDRY_ACCOUNT_ID)
    remove, blocked, skipped, nothing, notes = [], [], [], [], []
    for principal in principals:
        base = {"principalId": principal["id"], "label": principal["label"]}
        eligible, reason = classify_principal(principal)
        if not eligible:
            skipped.append({**base, "reason": reason})
            continue
        if not holds_models_user(gateway_rows, principal["id"]):
            blocked.append({**base, "reason": "does not hold AI Gateway Models User on the gateway; grant it first "
                                              "with ai-gateway-grants.py"})
            continue
        held = [row for row in account_rows
                if row["principalId"] == principal["id"] and row["roleId"] == common.ROLE_OPENAI_USER]
        targets = [row for row in held if is_account_scope(row["scope"])]
        notes += [f"{principal['label']}: Cognitive Services OpenAI User is also granted at {row['scope']}; "
                  "only account-scope assignments are removed." for row in held if row not in targets]
        if not targets:
            nothing.append({**base, "note": "no account-scope Cognitive Services OpenAI User assignment"})
        # The canonical scope is recorded, not the live string, so a receipt always validates and rolls back.
        remove += [{**base, "principalType": "ServicePrincipal", "scope": common.FOUNDRY_ACCOUNT_ID,
                    "assignmentName": row["name"],
                    "role": INFERENCE_ROLES[common.ROLE_OPENAI_USER], "roleId": common.ROLE_OPENAI_USER,
                    "roleDefinitionId": row["roleDefinitionId"]} for row in sorted(targets, key=lambda item: item["name"])]
    return {
        "schemaVersion": 1, "kind": PLAN_KIND, "scope": common.FOUNDRY_ACCOUNT_ID, "gatewayScope": common.GATEWAY_ID,
        "remove": remove, "blocked": blocked, "skipped": skipped, "nothingToRemove": nothing, "notes": sorted(notes),
        "planSha256": common.sha256_json({"kind": PLAN_KIND, "scope": common.FOUNDRY_ACCOUNT_ID, "remove": remove}),
    }


def apply_enforcement(runner, principals: list[dict], receipt_path: Path, *, expected_sha: str | None, now) -> dict:
    plan = plan_enforcement(runner, principals)
    if plan["blocked"]:
        raise common.GatewayError("Refusing to remove any access: " + "; ".join(
            f"{row['label']} ({row['principalId']}) {row['reason']}" for row in plan["blocked"]))
    if expected_sha and expected_sha != plan["planSha256"]:
        raise common.GatewayError("The live state no longer matches the reviewed plan; run `plan` again.")
    if not plan["remove"]:
        return {**plan, "applied": False, "receipt": None}
    receipt = {
        "schemaVersion": 1, "kind": RECEIPT_KIND, "scope": common.FOUNDRY_ACCOUNT_ID, "createdAt": common.iso(now()),
        "status": "in-progress", "planSha256": plan["planSha256"],
        "entries": [{key: row[key] for key in ("principalId", "principalType", "label", "scope", "assignmentName",
                                               "roleId", "roleDefinitionId")} | {"state": "pending"}
                    for row in plan["remove"]],
    }
    # The rollback receipt exists before the first deletion so an interrupted run can always be reversed.
    common.write_json_exclusive(receipt_path, receipt)
    for entry in receipt["entries"]:
        try:
            common.delete_role_assignment(runner, entry["scope"], entry["assignmentName"])
        except common.GatewayError as error:
            receipt["status"] = "failed"
            entry["error"] = str(error)
            common.replace_json(receipt_path, receipt)
            raise common.GatewayError(f"{error} The rollback receipt lists every assignment touched: {receipt_path}") from None
        entry["state"] = "removed"
        common.replace_json(receipt_path, receipt)
    receipt["status"] = "complete"
    common.replace_json(receipt_path, receipt)
    return {**plan, "applied": True, "receipt": str(receipt_path), "removed": len(receipt["entries"])}


def validate_receipt(receipt) -> None:
    valid = (isinstance(receipt, dict) and receipt.get("kind") == RECEIPT_KIND and receipt.get("schemaVersion") == 1
             and is_account_scope(receipt.get("scope")) and isinstance(receipt.get("entries"), list))
    if not valid:
        raise common.GatewayError("The file is not an enforcement receipt for this account.")
    protected = (common.APP_UAMI_PRINCIPAL_ID, common.GATEWAY_IDENTITY_PRINCIPAL_ID)
    for index, entry in enumerate(receipt["entries"]):
        valid = (isinstance(entry, dict) and is_account_scope(entry.get("scope"))
                 and entry.get("roleId") == common.ROLE_OPENAI_USER and entry.get("principalType") == "ServicePrincipal"
                 and common.is_guid(entry.get("principalId")) and entry.get("principalId") not in protected
                 and common.is_guid(entry.get("assignmentName")) and entry.get("state") in ("pending", "removed"))
        if not valid:
            raise common.GatewayError(
                f"entries[{index}] was not produced by this tool (scope, role or principal mismatch); nothing was recreated.")


def rollback_enforcement(runner, receipt: dict) -> dict:
    validate_receipt(receipt)
    results = []
    for entry in receipt["entries"]:
        outcome = common.put_role_assignment(runner, entry["scope"], entry["assignmentName"], entry["principalId"],
                                             entry["principalType"], entry["roleId"])
        results.append({"principalId": entry["principalId"], "assignmentName": entry["assignmentName"],
                        "outcome": "restored" if outcome == "created" else "already-present"})
    return {"schemaVersion": 1, "kind": "ai-gateway-enforce-rollback", "scope": common.FOUNDRY_ACCOUNT_ID, "results": results}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-gateway-enforce.py", description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit", help="Report direct-access exposure and bypass-detection KQL (read-only).")
    audit.add_argument("--principals", type=Path, help="Extra in-scope identities; the app identity is always included.")
    audit.add_argument("--out", type=Path, help="Also write the report JSON to this file.")
    plan = commands.add_parser("plan", help="Show which account-scope OpenAI User assignments would be removed (read-only).")
    plan.add_argument("--principals", type=Path, required=True)
    plan.add_argument("--out", type=Path, help="Also write the plan JSON to this file.")
    apply = commands.add_parser("apply", help="Remove those assignments after the gateway-role check; writes a rollback receipt.")
    apply.add_argument("--principals", type=Path, required=True)
    apply.add_argument("--yes", action="store_true", help="Required to remove role assignments.")
    apply.add_argument("--receipt", type=Path,
                       help="Rollback receipt path (never overwritten). Default: .release-state/ai-gateway-enforce-<UTC time>.json")
    apply.add_argument("--expect-plan-sha256", metavar="SHA", help="Refuse unless the fresh plan has this planSha256.")
    rollback = commands.add_parser("rollback", help="Recreate exactly the assignments recorded in a receipt.")
    rollback.add_argument("--receipt", type=Path, required=True)
    rollback.add_argument("--yes", action="store_true", help="Required to create role assignments.")
    return parser


def main(argv=None, *, runner=None, now=None) -> int:
    args = build_parser().parse_args(argv)
    now = now or common.utc_now
    try:
        if args.command in ("apply", "rollback") and not args.yes:
            raise common.GatewayError("Refusing to change role assignments without --yes; run `plan` first.")
        runner = runner or common.AzRestRunner()
        status = 0
        if args.command == "audit":
            principals = common.load_principals(args.principals) if args.principals else None
            result = run_audit(runner, principals, now=now)
            status = 0 if result["complete"] else 1
        elif args.command == "rollback":
            result = rollback_enforcement(runner, common.read_json(args.receipt))
        else:
            principals = common.load_principals(args.principals)
            if args.command == "plan":
                result = plan_enforcement(runner, principals)
                if result["blocked"]:
                    print("AI Gateway enforce plan is blocked: see `blocked`; apply would refuse.", file=sys.stderr)
                    status = 1
            else:
                receipt = args.receipt or common.default_receipt_path("enforce", now())
                result = apply_enforcement(runner, principals, receipt, expected_sha=args.expect_plan_sha256, now=now)
        if getattr(args, "out", None):
            args.out.parent.mkdir(parents=True, exist_ok=True)
            common.replace_json(args.out, result)
        print(common.pretty_json(result), end="")
        return status
    except (common.GatewayError, OSError, ValueError) as error:
        print(f"AI Gateway enforce refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
