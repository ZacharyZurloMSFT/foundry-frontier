"""Offline tests for the AI Gateway enforcement script: audit, guarded removal and rollback."""
import copy
import importlib.util
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


enforce = load_script("ai-gateway-enforce")
common = enforce.common
ACCOUNT = common.FOUNDRY_ACCOUNT_ID
GATEWAY = common.GATEWAY_ID
RESOURCE_GROUP = f"/subscriptions/{common.SUBSCRIPTION}/resourceGroups/rg-northwind-ai"
PROJECT = f"{ACCOUNT}/projects/nw-insurance-claims"
PROJECT_NAMES = ("nw-insurance-claims", "nw-insurance-documents", "nw-insurance-support", "nw-insurance-contact-center")
ROLE_ASSIGNMENTS = "/providers/Microsoft.Authorization/roleAssignments"
AGENT_A = "3ce7bef0-2680-4589-8cb3-c0ba6e0bb2ff"
AGENT_B = "907c489e-0cef-47a5-87ca-14cdb1786702"
AGENT_C = "abb02470-327e-4baf-a731-2d7ab75e4c42"
USER = "f7ad0ab5-639a-4120-a9b4-ba6a077eab17"
OTHER = "9a2b7dee-7663-461f-90bc-32191e9c8b09"
UAMI = common.APP_UAMI_PRINCIPAL_ID
GATEWAY_MI = common.GATEWAY_IDENTITY_PRINCIPAL_ID
NAME_A = "00000000-0000-4000-8000-00000000a001"
NAME_B = "00000000-0000-4000-8000-00000000b001"
COGNITIVE_SERVICES_USER = "a97b65f3-24c7-4388-baec-2e87135dc908"
MCP_APPS = ("ca-insurance-lookup", "ca-insurance-review")


def fixed_now():
    return datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def arm_item(scope, name, principal, role, principal_type="ServicePrincipal"):
    return {
        "id": f"{scope}{ROLE_ASSIGNMENTS}/{name}", "name": name, "type": "Microsoft.Authorization/roleAssignments",
        "properties": {"roleDefinitionId": common.role_definition_id(role), "principalId": principal,
                       "principalType": principal_type, "scope": scope},
    }


def app_item(name, **ingress):
    settings = {"external": True, "fqdn": f"{name}.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io",
                "allowInsecure": False, "ipSecurityRestrictions": None, "targetPort": 8000, **ingress}
    return {"id": f"/subscriptions/{common.SUBSCRIPTION}/resourceGroups/rg-northwind-apps"
                  f"/providers/Microsoft.App/containerapps/{name}",
            "name": name, "properties": {"configuration": {"ingress": settings}}}


class FakeAzure:
    """Foundry account, role assignments, container apps and diagnostics behind the one runner function."""

    def __init__(self):
        self.assignments = {}
        self.calls = []
        self.errors = {}
        self.delete_errors = []
        self.on_delete = None
        self.account = {"id": ACCOUNT, "kind": "AIServices", "properties": {
            "publicNetworkAccess": "Enabled", "disableLocalAuth": True, "networkAcls": {"defaultAction": "Allow"},
            "privateEndpointConnections": []}}
        self.projects = [{"name": f"aisvc-zurlo-northwind/{name}", "id": f"{ACCOUNT}/projects/{name}"}
                         for name in PROJECT_NAMES]
        self.apps = {name: app_item(name) for name in (*MCP_APPS, "ca-insurance-api")}
        self.auth = {}
        self.diagnostics = {}

    def seed(self, principal, role, scope, name=None, principal_type="ServicePrincipal"):
        name = name or str(uuid.uuid4())
        self.assignments[(scope.lower(), name)] = arm_item(scope, name, principal, role, principal_type)
        return name

    def methods(self):
        return [call["method"] for call in self.calls]

    def __call__(self, method, url, *, params=None, headers=None, body=None, scope=None):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers, "body": body,
                           "scope": scope})
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "management.azure.com", url
        path = parts.path
        lowered = path.lower()
        if method == "GET" and lowered in self.errors:
            raise self.errors[lowered]
        if ROLE_ASSIGNMENTS.lower() in lowered:
            return self._role_assignments(method, path, params, body)
        assert method == "GET", f"unexpected {method} {path}"
        if lowered == ACCOUNT.lower():
            return common.Response(200, copy.deepcopy(self.account))
        if lowered == f"{ACCOUNT.lower()}/projects":
            return common.Response(200, {"value": copy.deepcopy(self.projects)})
        if lowered.endswith("/providers/microsoft.app/containerapps"):
            assert "/resourcegroups/rg-northwind-apps/" in lowered and params == {"api-version": "2024-03-01"}
            return common.Response(200, {"value": copy.deepcopy(list(self.apps.values()))})
        if lowered.endswith("/authconfigs/current"):
            name = lowered.split("/")[-3]
            if name not in self.auth:
                raise common.RunnerError("Azure request GET failed (HTTP 404) [ResourceNotFound]", status=404,
                                         code="ResourceNotFound")
            return common.Response(200, {"properties": copy.deepcopy(self.auth[name])})
        if lowered.endswith("/providers/microsoft.insights/diagnosticsettings"):
            resource = lowered.removesuffix("/providers/microsoft.insights/diagnosticsettings")
            return common.Response(200, {"value": copy.deepcopy(self.diagnostics.get(resource, []))})
        raise AssertionError(f"unexpected GET {path}")

    def _role_assignments(self, method, path, params, body):
        assert params["api-version"] == "2022-04-01"
        collection, _, name = path.rpartition(ROLE_ASSIGNMENTS)
        name = name.lstrip("/")
        key = (collection.lower(), name)
        if method == "GET":
            assert not name and params["$filter"] == "atScope()"
            return common.Response(200, {"value": copy.deepcopy([
                item for item in self.assignments.values() if common.scope_covers(item["properties"]["scope"], collection)])})
        if method == "PUT":
            if key in self.assignments:
                raise common.RunnerError("exists", status=409, code="RoleAssignmentExists")
            properties = body["properties"]
            role = properties["roleDefinitionId"].rsplit("/", 1)[-1]
            self.assignments[key] = arm_item(collection, name, properties["principalId"], role, properties["principalType"])
            return common.Response(200, copy.deepcopy(self.assignments[key]))
        assert method == "DELETE", method
        if self.on_delete:
            self.on_delete(name)
        if self.delete_errors:
            raise self.delete_errors.pop(0)
        if key not in self.assignments:
            raise common.RunnerError("not found", status=404)
        del self.assignments[key]
        return common.Response(200)


def baseline():
    azure = FakeAzure()
    for agent in (AGENT_A, AGENT_B, AGENT_C):
        azure.seed(agent, common.ROLE_MODELS_USER, GATEWAY)
        azure.seed(agent, common.ROLE_FOUNDRY_USER, PROJECT)
    azure.seed(AGENT_A, common.ROLE_OPENAI_USER, ACCOUNT, name=NAME_A)
    azure.seed(AGENT_B, common.ROLE_OPENAI_USER, ACCOUNT, name=NAME_B)
    azure.seed(UAMI, common.ROLE_OPENAI_USER, ACCOUNT)
    azure.seed(UAMI, common.ROLE_FOUNDRY_USER, ACCOUNT)
    azure.seed(USER, common.ROLE_OPENAI_USER, ACCOUNT, principal_type="User")
    azure.seed(USER, common.ROLE_FOUNDRY_USER, ACCOUNT, principal_type="User")
    azure.seed(GATEWAY_MI, common.ROLE_FOUNDRY_USER, ACCOUNT)
    azure.seed(OTHER, COGNITIVE_SERVICES_USER, ACCOUNT)
    return azure


def principal(identity, kind="ServicePrincipal", roles=("models",)):
    return {"id": identity, "type": kind, "label": identity[:8], "roles": list(roles)}


def principals_file(tmp_path, entries=None):
    entries = entries or [principal(AGENT_A), principal(AGENT_B), principal(AGENT_C), principal(UAMI, roles=("models", "tools")),
                          principal(USER, "User"), principal(GATEWAY_MI)]
    path = tmp_path / "principals.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def run(azure, *args):
    return enforce.main(list(args), runner=azure, now=fixed_now)


def output(capsys):
    return json.loads(capsys.readouterr().out)


# --- audit ----------------------------------------------------------------------------------------


def test_audit_reports_account_identities_apps_detection_and_limits(tmp_path, capsys):
    azure = baseline()
    azure.seed(AGENT_A, common.ROLE_TOOLS_USER, GATEWAY)
    assert run(azure, "audit", "--principals", principals_file(tmp_path, [principal(AGENT_A), principal(AGENT_C)])) == 0
    report = output(capsys)
    assert report["kind"] == "ai-gateway-enforce-audit" and report["complete"] is True and report["errors"] == []
    account = report["account"]
    assert (account["publicNetworkAccess"], account["disableLocalAuth"]) == ("Enabled", True)
    assert account["networkAcls"] == {"defaultAction": "Allow", "ipRules": 0, "virtualNetworkRules": 0}
    assert {row["severity"] for row in account["findings"]} == {"info", "warning"}
    assert [row["name"] for row in report["projects"]] == sorted(PROJECT_NAMES)
    identities = {row["principalId"]: row for row in report["identities"]}
    assert set(identities) == {AGENT_A, AGENT_C, UAMI}
    agent = identities[AGENT_A]
    assert agent["gatewayRoles"] == {"models": True, "tools": True} and agent["canInferWithoutGateway"] is True
    direct = {(row["role"], row["scopeKind"]): row for row in agent["directInference"]}
    assert direct[("Cognitive Services OpenAI User", "account")]["removableByEnforce"] is True
    assert direct[("Cognitive Services OpenAI User", "account")]["assignmentName"] == NAME_A
    assert direct[("Foundry User (Azure AI User)", "project")]["removableByEnforce"] is False
    assert "Foundry User cannot be removed" in agent["notes"][0] and "RBAC alone cannot enforce" in agent["notes"][0]
    assert identities[AGENT_C]["canInferWithoutGateway"] is True
    assert [row["removableByEnforce"] for row in identities[AGENT_C]["directInference"]] == [False]
    assert identities[UAMI]["gatewayRoles"] == {"models": False, "tools": False}
    assert all(row["removableByEnforce"] is False for row in identities[UAMI]["directInference"])
    others = {(row["principalId"], row["role"]): row for row in report["otherPrincipals"]}
    assert others[(GATEWAY_MI, "Foundry User (Azure AI User)")]["isGatewayIdentity"] is True
    assert (OTHER, "Cognitive Services User") in others and (AGENT_B, "Cognitive Services OpenAI User") in others
    assert (USER, "Cognitive Services OpenAI User") in others
    assert len(report["mcpApps"]) == 2, "only the MCP apps are audited, not the API"
    assert all(row["found"] and row["exposure"] == "public-unauthenticated" and row["externalIngress"]
               and row["ipRestrictions"] == 0 for row in report["mcpApps"])
    assert {row["name"]: row["kind"] for row in report["mcpApps"]} == {
        "ca-insurance-lookup": "lookup", "ca-insurance-review": "claim-review"}
    assert any("gated manual decision" in line for line in report["mcpRecommendations"])
    assert any("claim-review" in line and "stays direct" in line and "x-workflow-id" in line
               for line in report["mcpRecommendations"])
    assert "Foundry User" in report["limits"][0] and "never removed" in report["limits"][0]
    assert any("not implemented" in line and "gated manual decision" in line for line in report["limits"])
    assert report["detection"]["bypassDetectionReady"] is False
    assert any("RequestResponse" in line for line in report["recommendations"])
    assert any("1 account-scope Cognitive Services OpenAI User" in line for line in report["recommendations"])


def test_audit_is_read_only_and_uses_the_documented_api_versions(capsys):
    azure = baseline()
    assert run(azure, "audit") == 0
    capsys.readouterr()
    assert set(azure.methods()) == {"GET"}
    versions = {urlsplit(call["url"]).path.split("/")[-1]: call["params"]["api-version"] for call in azure.calls}
    assert versions[common.FOUNDRY_ACCOUNT_NAME] == "2025-06-01" and versions["projects"] == "2025-06-01"
    assert versions["diagnosticSettings"] == "2021-05-01-preview" and versions["containerApps"] == "2024-03-01"
    assert versions["roleAssignments"] == "2022-04-01"


def test_audit_always_includes_the_app_identity(capsys):
    azure = baseline()
    assert run(azure, "audit") == 0
    report = output(capsys)
    assert [row["principalId"] for row in report["identities"]] == [UAMI]
    assert report["identities"][0]["label"] == "id-app-zurlo-northwind"


@pytest.mark.parametrize(("ingress", "auth", "expected"), [
    ({}, {}, "no-ingress"),
    ({"external": False}, {}, "internal-only"),
    ({"external": True}, {"enforced": True}, "public-authenticated"),
    ({"external": True, "ipSecurityRestrictions": [{"name": "gateway"}]}, {"enforced": False}, "public-ip-restricted"),
    ({"external": True, "ipSecurityRestrictions": None}, {"enforced": False}, "public-unauthenticated"),
])
def test_mcp_exposure_classification(ingress, auth, expected):
    assert enforce.mcp_exposure(ingress, auth) == expected


def test_audit_reads_authentication_restrictions_and_missing_apps(capsys):
    azure = baseline()
    azure.auth["ca-insurance-lookup"] = {"platform": {"enabled": True},
                                         "globalValidation": {"unauthenticatedClientAction": "Return401"}}
    azure.auth["ca-insurance-review"] = {"platform": {"enabled": True},
                                         "globalValidation": {"unauthenticatedClientAction": "AllowAnonymous"}}
    assert run(azure, "audit") == 0
    rows = {row["name"]: row for row in output(capsys)["mcpApps"]}
    assert rows["ca-insurance-lookup"]["exposure"] == "public-authenticated"
    assert rows["ca-insurance-lookup"]["authentication"]["enforced"] is True
    assert rows["ca-insurance-review"]["exposure"] == "public-unauthenticated"
    assert rows["ca-insurance-review"]["authentication"]["unauthenticatedClientAction"] == "AllowAnonymous"
    azure.auth.clear()
    azure.apps["ca-insurance-lookup"] = app_item("ca-insurance-lookup", ipSecurityRestrictions=[{"name": "gw"}])
    azure.apps["ca-insurance-review"] = app_item("ca-insurance-review", external=False)
    assert run(azure, "audit") == 0
    rows = {row["name"]: row for row in output(capsys)["mcpApps"]}
    assert rows["ca-insurance-lookup"]["exposure"] == "public-ip-restricted" and rows["ca-insurance-lookup"]["ipRestrictions"] == 1
    assert rows["ca-insurance-review"]["exposure"] == "internal-only"
    del azure.apps["ca-insurance-lookup"]
    assert run(azure, "audit") == 0
    assert {row["name"]: row for row in output(capsys)["mcpApps"]}["ca-insurance-lookup"] == {
        "name": "ca-insurance-lookup", "found": False}


def test_audit_reports_key_auth_enabled_as_a_high_finding(capsys):
    azure = baseline()
    azure.account["properties"].update(disableLocalAuth=False, publicNetworkAccess="Disabled")
    assert run(azure, "audit") == 0
    findings = output(capsys)["account"]["findings"]
    assert [row["severity"] for row in findings] == ["high"]


def test_audit_reads_diagnostic_settings_and_enables_the_detection_flag(capsys):
    azure = baseline()
    azure.diagnostics[ACCOUNT.lower()] = [{"name": "to-law", "properties": {
        "workspaceId": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/law",
        "logs": [{"category": "RequestResponse", "enabled": True}, {"categoryGroup": "audit", "enabled": False}]}}]
    assert run(azure, "audit") == 0
    detection = output(capsys)["detection"]
    assert detection["bypassDetectionReady"] is True
    account = detection["diagnosticSettings"]["account"]
    assert account["status"] == "ok" and account["settings"][0]["enabledLogs"] == ["RequestResponse"]
    assert detection["diagnosticSettings"]["gateway"] == {"status": "ok", "settings": []}


def test_audit_degrades_per_section_and_flags_incomplete_runs(capsys):
    azure = baseline()
    azure.errors[f"{ACCOUNT}/providers/Microsoft.Insights/diagnosticSettings".lower()] = common.RunnerError(
        "Azure request GET failed (HTTP 403) [AuthorizationFailed]", status=403, code="AuthorizationFailed")
    azure.errors[ACCOUNT.lower()] = common.RunnerError("Azure request GET failed (HTTP 500)", status=500)
    assert run(azure, "audit") == 1
    report = output(capsys)
    assert report["complete"] is False and [row["section"] for row in report["errors"]] == ["account"]
    assert report["account"] == {"status": "unavailable"} and report["identities"]
    assert report["detection"]["diagnosticSettings"]["account"]["status"] == "unavailable"
    assert "AuthorizationFailed" in report["detection"]["diagnosticSettings"]["account"]["error"]


def test_audit_out_file_matches_stdout(tmp_path, capsys):
    azure = baseline()
    out = tmp_path / "reports" / "audit.json"
    assert run(azure, "audit", "--out", str(out)) == 0
    assert output(capsys) == json.loads(out.read_text(encoding="utf-8"))


def test_kql_queries_target_the_gateway_identity_and_in_scope_principals():
    queries = enforce.kql_queries([AGENT_B, AGENT_A])
    assert set(queries) == {"foundryNotFromGateway", "inScopeIdentitiesBypassing", "gatewayTraffic", "gatewayModelUsage"}
    assert f'let gatewayPrincipal = "{GATEWAY_MI}"' in queries["foundryNotFromGateway"]
    assert "caller != gatewayPrincipal" in queries["foundryNotFromGateway"]
    assert f'dynamic(["{AGENT_A}", "{AGENT_B}"])' in queries["inScopeIdentitiesBypassing"]
    for name in ("foundryNotFromGateway", "inScopeIdentitiesBypassing"):
        assert "AzureDiagnostics" in queries[name] and '"aisvc-zurlo-northwind"' in queries[name]
        assert "column_ifexists" in queries[name]
        assert "props.objectId" in queries[name]
        assert 'matches regex "(?i)completion|embedding|response"' in queries[name]
    traffic = queries["gatewayTraffic"]
    assert "ApiManagementGatewayLlmLog" in traffic and "ApiManagementGatewayMCPLog" in traffic
    assert "isfuzzy=true" in traffic and f'let gateway = "{common.GATEWAY_NAME}"' in traffic
    assert not any(re.search(r"\{[a-z_]+\}", query) for query in queries.values())


# --- plan -----------------------------------------------------------------------------------------


def test_plan_removes_only_account_scope_openai_user_of_eligible_service_principals(tmp_path, capsys):
    azure = baseline()
    assert run(azure, "plan", "--principals", principals_file(tmp_path)) == 0
    plan = output(capsys)
    assert [(row["principalId"], row["assignmentName"]) for row in plan["remove"]] == [(AGENT_A, NAME_A), (AGENT_B, NAME_B)]
    assert {row["roleId"] for row in plan["remove"]} == {common.ROLE_OPENAI_USER}
    assert {row["scope"] for row in plan["remove"]} == {ACCOUNT}
    assert {row["principalId"]: row["reason"] for row in plan["skipped"]} == {
        UAMI: "the app user-assigned identity keeps its direct access",
        USER: "only service principals are changed; users keep their access",
        GATEWAY_MI: "the gateway identity needs its access to call the account"}
    assert [row["principalId"] for row in plan["nothingToRemove"]] == [AGENT_C]
    assert plan["blocked"] == [] and plan["scope"] == ACCOUNT
    assert set(azure.methods()) == {"GET"}
    assert run(azure, "plan", "--principals", principals_file(tmp_path)) == 0
    assert output(capsys)["planSha256"] == plan["planSha256"]


def test_plan_hash_changes_with_the_assignment_set(tmp_path, capsys):
    azure = baseline()
    assert run(azure, "plan", "--principals", principals_file(tmp_path)) == 0
    first = output(capsys)["planSha256"]
    azure.seed(AGENT_C, common.ROLE_OPENAI_USER, ACCOUNT)
    assert run(azure, "plan", "--principals", principals_file(tmp_path)) == 0
    assert output(capsys)["planSha256"] != first


def test_plan_notes_inherited_openai_user_without_removing_it(tmp_path, capsys):
    azure = baseline()
    azure.seed(AGENT_A, common.ROLE_OPENAI_USER, RESOURCE_GROUP)
    assert run(azure, "plan", "--principals", principals_file(tmp_path, [principal(AGENT_A)])) == 0
    plan = output(capsys)
    assert [row["assignmentName"] for row in plan["remove"]] == [NAME_A]
    assert len(plan["notes"]) == 1 and RESOURCE_GROUP in plan["notes"][0] and "only account-scope" in plan["notes"][0]


def test_plan_blocks_a_principal_without_the_gateway_models_role(tmp_path, capsys):
    azure = baseline()
    del azure.assignments[(GATEWAY.lower(), next(name for scope, name in azure.assignments
                                                  if scope == GATEWAY.lower() and
                                                  azure.assignments[(scope, name)]["properties"]["principalId"] == AGENT_B))]
    assert run(azure, "plan", "--principals", principals_file(tmp_path)) == 1
    plan = output(capsys)
    assert [row["principalId"] for row in plan["blocked"]] == [AGENT_B]
    assert "AI Gateway Models User" in plan["blocked"][0]["reason"]
    assert [row["principalId"] for row in plan["remove"]] == [AGENT_A]


def test_a_tools_only_gateway_role_does_not_satisfy_the_guard(tmp_path, capsys):
    azure = baseline()
    models = [key for key, item in azure.assignments.items()
              if key[0] == GATEWAY.lower() and item["properties"]["principalId"] == AGENT_A]
    for key in models:
        del azure.assignments[key]
    azure.seed(AGENT_A, common.ROLE_TOOLS_USER, GATEWAY)
    azure.seed(AGENT_A, common.ROLE_GATEWAY_USER, GATEWAY)
    assert run(azure, "plan", "--principals", principals_file(tmp_path, [principal(AGENT_A)])) == 1
    assert [row["principalId"] for row in output(capsys)["blocked"]] == [AGENT_A]


# --- apply ----------------------------------------------------------------------------------------


def test_apply_requires_yes_and_makes_no_calls_without_it(tmp_path, capsys):
    azure = baseline()
    receipt = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--receipt", str(receipt)) == 1
    assert "--yes" in capsys.readouterr().err and not azure.calls and not receipt.exists()


def test_apply_removes_only_the_planned_assignments_after_writing_the_receipt(tmp_path, capsys):
    azure = baseline()
    before = copy.deepcopy(azure.assignments)
    receipt_path = tmp_path / "receipt.json"
    seen = []

    def check_receipt(name):
        document = json.loads(receipt_path.read_text(encoding="utf-8"))
        seen.append((name, {row["assignmentName"]: row["state"] for row in document["entries"]}))

    azure.on_delete = check_receipt
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt_path)) == 0
    result = output(capsys)
    assert result["applied"] is True and result["removed"] == 2 and result["receipt"] == str(receipt_path)
    assert seen[0] == (NAME_A, {NAME_A: "pending", NAME_B: "pending"})
    assert seen[1] == (NAME_B, {NAME_A: "removed", NAME_B: "pending"})
    assert set(before) - set(azure.assignments) == {(ACCOUNT.lower(), NAME_A), (ACCOUNT.lower(), NAME_B)}
    for key in azure.assignments:
        assert azure.assignments[key] == before[key]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "complete" and receipt["kind"] == "ai-gateway-enforce-receipt"
    assert [(row["principalId"], row["state"]) for row in receipt["entries"]] == [(AGENT_A, "removed"), (AGENT_B, "removed")]
    assert all(row["roleId"] == common.ROLE_OPENAI_USER and row["scope"] == ACCOUNT for row in receipt["entries"])


def test_apply_never_touches_the_app_identity_users_gateway_identity_or_foundry_user(tmp_path):
    azure = baseline()
    azure.seed(AGENT_A, common.ROLE_FOUNDRY_USER, ACCOUNT)
    protected = {key: item for key, item in azure.assignments.items()
                 if item["properties"]["roleDefinitionId"].endswith(common.ROLE_FOUNDRY_USER)
                 or item["properties"]["principalId"] in (UAMI, USER, GATEWAY_MI, OTHER)}
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(tmp_path / "r.json")) == 0
    for key, item in protected.items():
        assert azure.assignments[key] == item
    deleted = [call["url"].rsplit("/", 1)[-1] for call in azure.calls if call["method"] == "DELETE"]
    assert deleted == [NAME_A, NAME_B]


def test_apply_refuses_the_whole_batch_when_one_principal_is_blocked(tmp_path, capsys):
    azure = baseline()
    azure.assignments = {key: item for key, item in azure.assignments.items()
                         if not (key[0] == GATEWAY.lower() and item["properties"]["principalId"] == AGENT_B)}
    receipt = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt)) == 1
    assert "AI Gateway Models User" in capsys.readouterr().err
    assert "DELETE" not in azure.methods() and not receipt.exists()
    assert (ACCOUNT.lower(), NAME_A) in azure.assignments


def test_apply_honours_the_reviewed_plan_hash(tmp_path, capsys):
    azure = baseline()
    assert run(azure, "plan", "--principals", principals_file(tmp_path)) == 0
    reviewed = output(capsys)["planSha256"]
    receipt = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt),
               "--expect-plan-sha256", "0" * 64) == 1
    assert "no longer matches the reviewed plan" in capsys.readouterr().err and "DELETE" not in azure.methods()
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt),
               "--expect-plan-sha256", reviewed) == 0
    assert azure.methods().count("DELETE") == 2


def test_apply_with_nothing_to_remove_writes_no_receipt(tmp_path, capsys):
    azure = baseline()
    receipt = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path, [principal(AGENT_C)]), "--yes",
               "--receipt", str(receipt)) == 0
    assert output(capsys)["applied"] is False and not receipt.exists() and "DELETE" not in azure.methods()


def test_apply_never_overwrites_an_existing_receipt(tmp_path):
    azure = baseline()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("keep", encoding="utf-8")
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt)) == 1
    assert receipt.read_text(encoding="utf-8") == "keep" and "DELETE" not in azure.methods()


def test_apply_only_deletes_role_assignments_and_changes_nothing_else(tmp_path):
    azure = baseline()
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(tmp_path / "r.json")) == 0
    writes = [call for call in azure.calls if call["method"] != "GET"]
    assert {call["method"] for call in writes} == {"DELETE"}
    assert all(f"{ACCOUNT}{ROLE_ASSIGNMENTS}/" in call["url"] for call in writes)
    assert not any("containerapps" in call["url"].lower() or "networkacls" in call["url"].lower() for call in azure.calls)


# --- rollback -------------------------------------------------------------------------------------


def test_rollback_recreates_exactly_the_removed_assignments(tmp_path, capsys):
    azure = baseline()
    before = copy.deepcopy(azure.assignments)
    receipt = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt)) == 0
    capsys.readouterr()
    assert azure.assignments != before
    azure.calls.clear()
    assert run(azure, "rollback", "--receipt", str(receipt), "--yes") == 0
    report = output(capsys)
    assert [row["outcome"] for row in report["results"]] == ["restored", "restored"]
    assert azure.assignments == before
    puts = [call for call in azure.calls if call["method"] == "PUT"]
    assert [call["url"].rsplit("/", 1)[-1] for call in puts] == [NAME_A, NAME_B]
    assert puts[0]["body"] == {"properties": {
        "roleDefinitionId": f"/subscriptions/{common.SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{common.ROLE_OPENAI_USER}",
        "principalId": AGENT_A, "principalType": "ServicePrincipal"}}
    assert set(azure.methods()) == {"PUT"}


def test_rollback_is_idempotent_and_requires_yes(tmp_path, capsys):
    azure = baseline()
    receipt = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt)) == 0
    capsys.readouterr()
    azure.calls.clear()
    assert run(azure, "rollback", "--receipt", str(receipt)) == 1
    assert "--yes" in capsys.readouterr().err and not azure.calls
    assert run(azure, "rollback", "--receipt", str(receipt), "--yes") == 0
    capsys.readouterr()
    assert run(azure, "rollback", "--receipt", str(receipt), "--yes") == 0
    assert {row["outcome"] for row in output(capsys)["results"]} == {"already-present"}


def test_a_failed_deletion_leaves_a_receipt_that_rollback_can_use(tmp_path, capsys):
    azure = baseline()
    before = copy.deepcopy(azure.assignments)
    receipt_path = tmp_path / "receipt.json"
    def fail_second_deletion(name):
        if name == NAME_B:
            azure.delete_errors.append(common.RunnerError(
                "Azure request DELETE failed (HTTP 403) [AuthorizationFailed]", status=403, code="AuthorizationFailed"))

    azure.on_delete = fail_second_deletion
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt_path)) == 1
    assert "AuthorizationFailed" in capsys.readouterr().err
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert [(row["assignmentName"], row["state"]) for row in receipt["entries"]] == [(NAME_A, "removed"), (NAME_B, "pending")]
    assert "error" in receipt["entries"][1]
    assert run(azure, "rollback", "--receipt", str(receipt_path), "--yes") == 0
    assert {row["outcome"] for row in output(capsys)["results"]} == {"restored", "already-present"}
    assert azure.assignments == before


RECASED_ACCOUNT = ACCOUNT.replace("resourceGroups", "resourcegroups").replace("rg-northwind-ai", "RG-NORTHWIND-AI")


def recased_baseline():
    """The first agent's account-scope assignment comes back from ARM with different casing than the canonical id."""
    azure = baseline()
    azure.seed(AGENT_A, common.ROLE_OPENAI_USER, RECASED_ACCOUNT, name=NAME_A)
    assert azure.assignments[(ACCOUNT.lower(), NAME_A)]["properties"]["scope"] == RECASED_ACCOUNT != ACCOUNT
    return azure


def test_a_recased_account_scope_is_still_removed_and_recorded_with_the_canonical_scope(tmp_path, capsys):
    azure = recased_baseline()
    assert run(azure, "plan", "--principals", principals_file(tmp_path)) == 0
    plan = output(capsys)
    assert [(row["assignmentName"], row["scope"]) for row in plan["remove"]] == [(NAME_A, ACCOUNT), (NAME_B, ACCOUNT)]
    assert plan["notes"] == []
    receipt_path = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt_path)) == 0
    capsys.readouterr()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "complete" and {row["scope"] for row in receipt["entries"]} == {ACCOUNT}
    assert (ACCOUNT.lower(), NAME_A) not in azure.assignments


def test_a_recased_account_scope_assignment_can_be_rolled_back(tmp_path, capsys):
    azure = recased_baseline()
    receipt_path = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt_path)) == 0
    capsys.readouterr()
    azure.calls.clear()
    assert run(azure, "rollback", "--receipt", str(receipt_path), "--yes") == 0
    assert [row["outcome"] for row in output(capsys)["results"]] == ["restored", "restored"]
    assert {key for key in azure.assignments if key[1] in (NAME_A, NAME_B)} == {
        (ACCOUNT.lower(), NAME_A), (ACCOUNT.lower(), NAME_B)}


def test_the_plan_hash_does_not_depend_on_the_live_scope_casing(tmp_path, capsys):
    hashes = []
    for azure in (baseline(), recased_baseline()):
        assert run(azure, "plan", "--principals", principals_file(tmp_path)) == 0
        hashes.append(output(capsys)["planSha256"])
    assert hashes[0] == hashes[1]


def test_rollback_accepts_a_receipt_whose_scope_differs_only_in_casing(tmp_path, capsys):
    azure = baseline()
    receipt_path = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt_path)) == 0
    capsys.readouterr()
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document["scope"] = ACCOUNT.upper()
    for entry in document["entries"]:
        entry["scope"] = RECASED_ACCOUNT
    receipt_path.write_text(json.dumps(document), encoding="utf-8")
    assert run(azure, "rollback", "--receipt", str(receipt_path), "--yes") == 0
    assert [row["outcome"] for row in output(capsys)["results"]] == ["restored", "restored"]


def _scope(receipt):
    receipt["entries"][0]["scope"] = RESOURCE_GROUP


def _role(receipt):
    receipt["entries"][0]["roleId"] = common.ROLE_FOUNDRY_USER


def _uami(receipt):
    receipt["entries"][0]["principalId"] = UAMI


def _gateway_identity(receipt):
    receipt["entries"][0]["principalId"] = GATEWAY_MI


def _user(receipt):
    receipt["entries"][0]["principalType"] = "User"


def _state(receipt):
    receipt["entries"][0]["state"] = "unknown"


def _name(receipt):
    receipt["entries"][0]["assignmentName"] = "not-a-guid"


def _kind(receipt):
    receipt["kind"] = "ai-gateway-grants-receipt"


def _receipt_scope(receipt):
    receipt["scope"] = GATEWAY


def _entries(receipt):
    receipt["entries"] = "nope"


@pytest.mark.parametrize("tamper", [_scope, _role, _uami, _gateway_identity, _user, _state, _name, _kind,
                                    _receipt_scope, _entries])
def test_rollback_refuses_receipts_this_tool_did_not_produce(tmp_path, tamper):
    azure = baseline()
    receipt = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", principals_file(tmp_path), "--yes", "--receipt", str(receipt)) == 0
    document = json.loads(receipt.read_text(encoding="utf-8"))
    tamper(document)
    receipt.write_text(json.dumps(document), encoding="utf-8")
    azure.calls.clear()
    assert run(azure, "rollback", "--receipt", str(receipt), "--yes") == 1
    assert not azure.calls


# --- guard rails ----------------------------------------------------------------------------------


def test_the_script_has_no_other_mutation_path_and_no_network_or_container_app_changes():
    source = (ROOT / "scripts" / "ai-gateway-enforce.py").read_text(encoding="utf-8")
    assert not re.search(r"""["'](PUT|PATCH|POST|DELETE)["']""", source)
    assert source.count("delete_role_assignment(") == 1 and source.count("put_role_assignment(") == 1
    assert "ipSecurityRestrictions" in source and "networkAcls" in source
    assert not re.search(r"""runner\(\s*["'](?!GET)""", source)


def test_plan_only_ever_targets_the_account_scope_openai_user_role(tmp_path, capsys):
    azure = baseline()
    other_roles = {
        "Foundry User": common.ROLE_FOUNDRY_USER, "Cognitive Services User": COGNITIVE_SERVICES_USER,
        "Azure AI Developer": "64702f94-c441-49e6-a78b-ef80e0188fee",
        "Cognitive Services OpenAI Contributor": "a001fd3d-188f-4b5d-821b-7da978bf7442",
    }
    kept = {name: azure.seed(AGENT_C, role, ACCOUNT) for name, role in other_roles.items()}
    openai_user = azure.seed(AGENT_C, common.ROLE_OPENAI_USER, ACCOUNT)
    assert run(azure, "plan", "--principals", principals_file(tmp_path, [principal(AGENT_C)])) == 0
    plan = output(capsys)
    assert [(row["assignmentName"], row["roleId"]) for row in plan["remove"]] == [(openai_user, common.ROLE_OPENAI_USER)]
    assert not set(kept.values()) & {row["assignmentName"] for row in plan["remove"]}
    assert run(azure, "audit", "--principals", principals_file(tmp_path, [principal(AGENT_C)])) == 0
    direct = {row["role"]: row["removableByEnforce"] for identity in output(capsys)["identities"]
              if identity["principalId"] == AGENT_C for row in identity["directInference"]}
    assert direct == {"Cognitive Services OpenAI User": True, "Foundry User (Azure AI User)": False,
                      "Cognitive Services User": False, "Azure AI Developer": False,
                      "Cognitive Services OpenAI Contributor": False}


def test_classification_never_allows_the_app_identity_users_or_the_gateway_identity():
    assert enforce.classify_principal(principal(UAMI)) == (False, "the app user-assigned identity keeps its direct access")
    assert enforce.classify_principal(principal(GATEWAY_MI))[0] is False
    assert enforce.classify_principal(principal(AGENT_A, "User"))[0] is False
    assert enforce.classify_principal(principal(AGENT_A)) == (True, "")


def option_strings(parser):
    found = set()
    for action in parser._actions:
        found.update(action.option_strings)
        if isinstance(getattr(action, "choices", None), dict):
            for subparser in action.choices.values():
                found |= option_strings(subparser)
    return found


def test_script_uses_only_the_injected_runner_and_takes_no_secret_arguments():
    source = (ROOT / "scripts" / "ai-gateway-enforce.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:import|from)\s+(?:subprocess|httpx|requests|urllib\.request|http\.client|socket|azure)\b",
                         source, re.MULTILINE)
    options = option_strings(enforce.build_parser())
    assert {"--principals", "--yes", "--receipt", "--expect-plan-sha256", "--out"} <= options
    assert not [option for option in options if re.search(r"key|token|password|secret", option)]


def test_help_states_the_limits_and_required_roles(capsys):
    with pytest.raises(SystemExit) as stop:
        enforce.main(["--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    assert "Foundry User" in text and "never removed" in text and "gated manual decision" in text
    assert "User Access Administrator" in text and "NOT implemented here" in text
