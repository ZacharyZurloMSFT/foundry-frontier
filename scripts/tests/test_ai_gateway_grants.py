"""Offline tests for the AI Gateway grants script: idempotent grants, receipts, revoke and agent discovery."""
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


grants = load_script("ai-gateway-grants")
common = grants.common
GATEWAY = common.GATEWAY_ID
ROLE_ASSIGNMENTS = "/providers/Microsoft.Authorization/roleAssignments"
AGENT_A = "3ce7bef0-2680-4589-8cb3-c0ba6e0bb2ff"
AGENT_B = "907c489e-0cef-47a5-87ca-14cdb1786702"
USER = "f7ad0ab5-639a-4120-a9b4-ba6a077eab17"
ENDPOINT = "https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-claims"


def fixed_now():
    return datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def arm_item(scope, name, principal, role, principal_type="ServicePrincipal"):
    return {
        "id": f"{scope}{ROLE_ASSIGNMENTS}/{name}", "name": name, "type": "Microsoft.Authorization/roleAssignments",
        "properties": {"roleDefinitionId": common.role_definition_id(role), "principalId": principal,
                       "principalType": principal_type, "scope": scope},
    }


class FakeAuthorization:
    """Role assignments (ARM) and the Foundry agents data plane behind the one runner function."""

    def __init__(self):
        self.assignments = {}
        self.calls = []
        self.put_errors = []
        self.agent_pages = []

    def seed(self, principal, role, scope=GATEWAY, name=None, principal_type="ServicePrincipal"):
        name = name or str(uuid.uuid4())
        self.assignments[(scope.lower(), name)] = arm_item(scope, name, principal, role, principal_type)
        return name

    def methods(self):
        return [call["method"] for call in self.calls]

    def names(self, method):
        return [call["url"].rsplit("/", 1)[-1] for call in self.calls if call["method"] == method]

    def __call__(self, method, url, *, params=None, headers=None, body=None, scope=None):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers, "body": body,
                           "scope": scope})
        parts = urlsplit(url)
        if parts.netloc == "aisvc-zurlo-northwind.services.ai.azure.com":
            return self._agents(method, parts.path, params, scope)
        assert parts.netloc == "management.azure.com", url
        path = parts.path
        assert ROLE_ASSIGNMENTS in path, path
        assert params["api-version"] == "2022-04-01"
        collection, _, name = path.rpartition(ROLE_ASSIGNMENTS)
        name = name.lstrip("/")
        if method == "GET" and not name:
            assert params["$filter"] == "atScope()"
            return common.Response(200, {"value": copy.deepcopy([
                item for item in self.assignments.values() if common.scope_covers(item["properties"]["scope"], collection)])})
        key = (collection.lower(), name)
        if method == "GET":
            if key not in self.assignments:
                raise common.RunnerError("not found", status=404, code="RoleAssignmentNotFound")
            return common.Response(200, copy.deepcopy(self.assignments[key]))
        if method == "PUT":
            if self.put_errors:
                raise self.put_errors.pop(0)
            if key in self.assignments:
                raise common.RunnerError("exists", status=409, code="RoleAssignmentExists")
            properties = body["properties"]
            role = properties["roleDefinitionId"].rsplit("/", 1)[-1]
            self.assignments[key] = arm_item(collection, name, properties["principalId"], role, properties["principalType"])
            return common.Response(200, copy.deepcopy(self.assignments[key]))
        assert method == "DELETE", method
        if key not in self.assignments:
            raise common.RunnerError("not found", status=404)
        del self.assignments[key]
        return common.Response(200)

    def _agents(self, method, path, params, scope):
        assert method == "GET" and path.endswith("/agents")
        assert scope == "https://ai.azure.com/.default"
        assert params["api-version"] == "v1" and params["kind"] == "hosted"
        index = int(params.get("after", 0)) if params.get("after") else 0
        page = self.agent_pages[index]
        more = index + 1 < len(self.agent_pages)
        return common.Response(200, {"object": "list", "data": copy.deepcopy(page), "has_more": more,
                                     "last_id": str(index + 1) if more else None})


def run(azure, *args):
    return grants.main(list(args), runner=azure, now=fixed_now)


def principals_file(tmp_path, entries):
    path = tmp_path / "principals.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def entry(principal, roles, kind="ServicePrincipal", label=None):
    return {"id": principal, "type": kind, "label": label or principal[:8], "roles": roles}


def agent(name, principal, *, version="1", kind="hosted", status="active"):
    latest = {"object": "agent.version", "name": name, "version": version, "status": status,
              "definition": {"kind": kind}}
    if principal:
        latest["instance_identity"] = {"principal_id": principal, "client_id": principal}
    return {"object": "agent", "id": name, "name": name, "state": "enabled", "versions": {"latest": latest}}


# --- principals -----------------------------------------------------------------------------------


def test_preset_is_the_app_identity_with_the_models_role_only():
    assert common.preset_principals("app-uami") == [{
        "id": "4345f6c9-3c1a-4f4f-9d58-e75d7fa6ee17", "type": "ServicePrincipal", "label": "id-app-zurlo-northwind",
        "roles": ["models"]}]


@pytest.mark.parametrize(("data", "fragment"), [
    ([], "non-empty JSON list"),
    ({"id": AGENT_A}, "non-empty JSON list"),
    ([{"id": "not-a-guid", "type": "ServicePrincipal", "roles": ["models"]}], "GUID"),
    ([{"id": AGENT_A, "type": "Group", "roles": ["models"]}], "type must be one of"),
    ([{"id": AGENT_A, "type": "User", "roles": ["owner"]}], "roles must be"),
    ([{"id": AGENT_A, "type": "User", "roles": []}], "roles must be"),
    ([{"id": AGENT_A, "type": "User", "roles": ["models"], "extra": 1}], "must be an object"),
    ([{"id": AGENT_A, "type": "User"}], "must be an object"),
    ([{"id": AGENT_A, "type": "User", "roles": ["models"], "label": "bad\nlabel"}], "label"),
    ([entry(AGENT_A, ["models"]), entry(AGENT_A.upper(), ["tools"])], "repeats principal"),
])
def test_invalid_principals_are_rejected(data, fragment):
    with pytest.raises(common.GatewayError, match=re.escape(fragment)):
        common.validate_principals(data)


def test_principals_are_normalised_and_roles_are_ordered():
    (principal,) = common.validate_principals([{"id": AGENT_A.upper(), "type": "User", "roles": ["tools", "models"]}])
    assert principal == {"id": AGENT_A, "type": "User", "label": AGENT_A.upper(), "roles": ["models", "tools"]}


# --- plan -----------------------------------------------------------------------------------------


def test_assignment_names_use_the_deploy_orders_hosted_scheme():
    for role in (common.ROLE_MODELS_USER, common.ROLE_TOOLS_USER):
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{GATEWAY.lower()}|{AGENT_A}|{role}"))
        assert common.assignment_name(GATEWAY, AGENT_A, role) == expected
    assert common.assignment_name(GATEWAY, AGENT_A, common.ROLE_MODELS_USER) != common.assignment_name(
        GATEWAY, AGENT_A, common.ROLE_TOOLS_USER)


def test_plan_lists_missing_and_present_assignments_and_is_read_only(tmp_path, capsys):
    azure = FakeAuthorization()
    azure.seed(AGENT_A, common.ROLE_MODELS_USER)
    resource_group = "/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-ai"
    azure.seed(AGENT_B, common.ROLE_MODELS_USER, scope=resource_group)
    azure.seed(AGENT_B, common.ROLE_TOOLS_USER, scope="/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/other")
    path = principals_file(tmp_path, [entry(AGENT_A, ["models", "tools"]), entry(AGENT_B, ["models", "tools"])])
    assert run(azure, "plan", "--principals", path) == 0
    plan = json.loads(capsys.readouterr().out)
    rows = {(row["principalId"], row["role"]): row for row in plan["grants"]}
    assert rows[(AGENT_A, "models")]["state"] == "already-present" and rows[(AGENT_A, "models")]["presentAt"] == [GATEWAY]
    assert rows[(AGENT_A, "tools")]["state"] == "would-create"
    assert rows[(AGENT_B, "models")]["state"] == "already-present" and rows[(AGENT_B, "models")]["presentAt"] == [resource_group]
    assert rows[(AGENT_B, "tools")]["state"] == "would-create"
    assert plan["summary"] == {"wouldCreate": 2, "alreadyPresent": 2} and plan["scope"] == GATEWAY
    assert rows[(AGENT_A, "tools")]["assignmentName"] == common.assignment_name(GATEWAY, AGENT_A, common.ROLE_TOOLS_USER)
    assert set(azure.methods()) == {"GET"}
    assert azure.calls[0]["url"] == f"https://management.azure.com{GATEWAY}{ROLE_ASSIGNMENTS}"


def test_plan_accepts_the_preset(capsys):
    azure = FakeAuthorization()
    assert run(azure, "plan", "--preset", "app-uami") == 0
    plan = json.loads(capsys.readouterr().out)
    assert [(row["principalId"], row["role"], row["state"]) for row in plan["grants"]] == [
        (common.APP_UAMI_PRINCIPAL_ID, "models", "would-create")]
    assert [row["roleName"] for row in plan["grants"]] == ["AI Gateway Models User"]


# --- apply ----------------------------------------------------------------------------------------


def test_apply_requires_yes_and_makes_no_calls_without_it(tmp_path, capsys):
    azure = FakeAuthorization()
    receipt = tmp_path / "receipt.json"
    assert run(azure, "apply", "--preset", "app-uami", "--receipt", str(receipt)) == 1
    assert "--yes" in capsys.readouterr().err
    assert not azure.calls and not receipt.exists()


def test_apply_creates_only_missing_assignments_and_records_exactly_those(tmp_path, capsys):
    azure = FakeAuthorization()
    azure.seed(AGENT_A, common.ROLE_MODELS_USER)
    path = principals_file(tmp_path, [entry(AGENT_A, ["models", "tools"]), entry(AGENT_B, ["models"], kind="User")])
    receipt_path = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", path, "--yes", "--receipt", str(receipt_path)) == 0
    puts = [call for call in azure.calls if call["method"] == "PUT"]
    assert len(puts) == 2
    tools_name = common.assignment_name(GATEWAY, AGENT_A, common.ROLE_TOOLS_USER)
    models_name = common.assignment_name(GATEWAY, AGENT_B, common.ROLE_MODELS_USER)
    assert [call["url"].rsplit("/", 1)[-1] for call in puts] == [tools_name, models_name]
    assert all(call["params"] == {"api-version": "2022-04-01"} for call in puts)
    assert all(call["url"].startswith(f"https://management.azure.com{GATEWAY}{ROLE_ASSIGNMENTS}/") for call in puts)
    assert puts[0]["body"] == {"properties": {
        "roleDefinitionId": f"/subscriptions/{common.SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{common.ROLE_TOOLS_USER}",
        "principalId": AGENT_A, "principalType": "ServicePrincipal"}}
    assert puts[1]["body"]["properties"]["principalType"] == "User"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "complete" and receipt["scope"] == GATEWAY and receipt["kind"] == "ai-gateway-grants-receipt"
    assert [(row["principalId"], row["role"], row["assignmentName"]) for row in receipt["created"]] == [
        (AGENT_A, "tools", tools_name), (AGENT_B, "models", models_name)]
    assert [(row["principalId"], row["role"]) for row in receipt["alreadyPresent"]] == [(AGENT_A, "models")]
    assert receipt["alreadyPresent"][0]["presentAt"] == [GATEWAY] and "assignmentName" not in receipt["alreadyPresent"][0]
    assert json.loads(capsys.readouterr().out)["created"] == receipt["created"]
    assert set(azure.methods()) == {"GET", "PUT"}


def test_apply_is_idempotent(tmp_path):
    azure = FakeAuthorization()
    path = principals_file(tmp_path, [entry(AGENT_A, ["models", "tools"])])
    assert run(azure, "apply", "--principals", path, "--yes", "--receipt", str(tmp_path / "one.json")) == 0
    puts_after_first = azure.methods().count("PUT")
    assert run(azure, "apply", "--principals", path, "--yes", "--receipt", str(tmp_path / "two.json")) == 0
    assert azure.methods().count("PUT") == puts_after_first == 2
    second = json.loads((tmp_path / "two.json").read_text(encoding="utf-8"))
    assert second["created"] == [] and len(second["alreadyPresent"]) == 2


def test_apply_never_overwrites_an_existing_receipt(tmp_path):
    azure = FakeAuthorization()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("keep", encoding="utf-8")
    assert run(azure, "apply", "--preset", "app-uami", "--yes", "--receipt", str(receipt)) == 1
    assert receipt.read_text(encoding="utf-8") == "keep" and "PUT" not in azure.methods()


def test_apply_defaults_the_receipt_to_the_ignored_release_state_folder():
    path = common.default_receipt_path("grants", fixed_now())
    assert path == ROOT / ".release-state" / "ai-gateway-grants-20261022T120000Z.json"
    assert ".release-state/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_a_racing_duplicate_is_present_not_created(tmp_path):
    azure = FakeAuthorization()
    azure.put_errors = [common.RunnerError("exists", status=409, code="RoleAssignmentExists")]
    receipt_path = tmp_path / "receipt.json"
    assert run(azure, "apply", "--preset", "app-uami", "--yes", "--receipt", str(receipt_path)) == 0
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["created"] == []
    assert [row["role"] for row in receipt["alreadyPresent"]] == ["models"]
    assert receipt["alreadyPresent"][0]["note"] == "created concurrently by another writer"
    assert receipt["alreadyPresent"][0]["assignmentName"] == common.assignment_name(
        GATEWAY, common.APP_UAMI_PRINCIPAL_ID, common.ROLE_MODELS_USER)


def test_a_failed_write_keeps_a_partial_receipt(tmp_path, capsys):
    azure = FakeAuthorization()
    receipt_path = tmp_path / "receipt.json"
    path = principals_file(tmp_path, [entry(AGENT_A, ["models", "tools"])])
    original = azure.__call__

    calls = {"puts": 0}

    def flaky(method, url, **kwargs):
        if method == "PUT":
            calls["puts"] += 1
            if calls["puts"] == 2:
                raise common.RunnerError("Azure request failed (HTTP 403) [AuthorizationFailed]", status=403,
                                         code="AuthorizationFailed")
        return original(method, url, **kwargs)

    assert grants.main(["apply", "--principals", path, "--yes", "--receipt", str(receipt_path)],
                       runner=flaky, now=fixed_now) == 1
    err = capsys.readouterr().err
    assert "AuthorizationFailed" in err and str(receipt_path) in err
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "failed" and [row["role"] for row in receipt["created"]] == ["models"]
    assert receipt["failed"][0]["role"] == "tools"


# --- revoke ---------------------------------------------------------------------------------------


def applied_receipt(azure, tmp_path, entries=None):
    path = principals_file(tmp_path, entries or [entry(AGENT_A, ["models", "tools"]), entry(AGENT_B, ["models"])])
    receipt = tmp_path / "receipt.json"
    assert run(azure, "apply", "--principals", path, "--yes", "--receipt", str(receipt)) == 0
    return receipt


def test_revoke_requires_yes(tmp_path, capsys):
    azure = FakeAuthorization()
    receipt = applied_receipt(azure, tmp_path)
    azure.calls.clear()
    assert run(azure, "revoke", "--receipt", str(receipt)) == 1
    assert "--yes" in capsys.readouterr().err and not azure.calls


def test_revoke_deletes_only_what_the_receipt_recorded(tmp_path, capsys):
    azure = FakeAuthorization()
    azure.seed(AGENT_A, common.ROLE_MODELS_USER, name="pre-existing-models-assignment")
    other = azure.seed(USER, common.ROLE_MODELS_USER, principal_type="User")
    receipt = applied_receipt(azure, tmp_path)
    recorded = json.loads(receipt.read_text(encoding="utf-8"))["created"]
    assert {row["role"] for row in recorded} == {"tools", "models"} and len(recorded) == 2
    capsys.readouterr()
    azure.calls.clear()
    assert run(azure, "revoke", "--receipt", str(receipt), "--yes") == 0
    report = json.loads(capsys.readouterr().out)
    assert [row["outcome"] for row in report["results"]] == ["deleted", "deleted"]
    assert sorted(azure.names("DELETE")) == sorted(row["assignmentName"] for row in recorded)
    remaining = {name for _, name in azure.assignments}
    assert remaining == {"pre-existing-models-assignment", other}


def test_revoke_is_idempotent(tmp_path, capsys):
    azure = FakeAuthorization()
    receipt = applied_receipt(azure, tmp_path)
    assert run(azure, "revoke", "--receipt", str(receipt), "--yes") == 0
    capsys.readouterr()
    azure.calls.clear()
    assert run(azure, "revoke", "--receipt", str(receipt), "--yes") == 0
    assert {row["outcome"] for row in json.loads(capsys.readouterr().out)["results"]} == {"already-absent"}
    assert "DELETE" not in azure.methods()


def _scope(receipt):
    receipt["created"][0]["scope"] = common.FOUNDRY_ACCOUNT_ID


def _name(receipt):
    receipt["created"][0]["assignmentName"] = str(uuid.uuid4())


def _role(receipt):
    receipt["created"][0]["roleId"] = common.ROLE_OPENAI_USER


def _role_key(receipt):
    receipt["created"][0]["role"] = "owner"


def _principal(receipt):
    receipt["created"][0]["principalId"] = USER


def _kind(receipt):
    receipt["kind"] = "ai-gateway-enforce-receipt"


def _receipt_scope(receipt):
    receipt["scope"] = common.FOUNDRY_ACCOUNT_ID


def _not_a_list(receipt):
    receipt["created"] = {"scope": GATEWAY}


@pytest.mark.parametrize("tamper", [_scope, _name, _role, _role_key, _principal, _kind, _receipt_scope, _not_a_list])
def test_revoke_refuses_receipts_this_tool_did_not_produce(tmp_path, tamper):
    azure = FakeAuthorization()
    receipt = applied_receipt(azure, tmp_path)
    document = json.loads(receipt.read_text(encoding="utf-8"))
    tamper(document)
    receipt.write_text(json.dumps(document), encoding="utf-8")
    azure.calls.clear()
    assert run(azure, "revoke", "--receipt", str(receipt), "--yes") == 1
    assert not azure.calls


def test_revoke_refuses_when_a_recorded_assignment_changed_and_deletes_nothing(tmp_path, capsys):
    azure = FakeAuthorization()
    receipt = applied_receipt(azure, tmp_path)
    recorded = json.loads(receipt.read_text(encoding="utf-8"))["created"]
    first = azure.assignments[(GATEWAY.lower(), recorded[0]["assignmentName"])]
    first["properties"]["principalId"] = USER
    capsys.readouterr()
    azure.calls.clear()
    assert run(azure, "revoke", "--receipt", str(receipt), "--yes") == 1
    assert "no longer matches the receipt" in capsys.readouterr().err
    assert "DELETE" not in azure.methods()


# --- discover-agents ------------------------------------------------------------------------------


def test_discover_agents_prints_the_latest_hosted_principals(capsys):
    azure = FakeAuthorization()
    azure.agent_pages = [[
        agent("northwind-insurance-triage", AGENT_B, version="3"),
        agent("northwind-insurance-orchestrator", AGENT_A),
        agent("northwind-claims-triage", "b9336c14-a434-4aa4-b583-d51452ed487c", kind="prompt"),
        agent("northwind-insurance-failed", "11111111-1111-4111-8111-111111111111", status="failed"),
        agent("northwind-insurance-noidentity", None),
        agent("northwind-insurance-shared", AGENT_A),
    ]]
    assert run(azure, "discover-agents", "--project-endpoint", ENDPOINT + "/") == 0
    captured = capsys.readouterr()
    principals = json.loads(captured.out)
    assert principals == [
        {"id": AGENT_A, "type": "ServicePrincipal", "label": "northwind-insurance-orchestrator:v1", "roles": ["models"]},
        {"id": AGENT_B, "type": "ServicePrincipal", "label": "northwind-insurance-triage:v3", "roles": ["models"]},
    ]
    assert common.validate_principals(principals) == principals
    assert "northwind-insurance-failed" in captured.err and "northwind-insurance-shared" in captured.err
    (call,) = azure.calls
    assert call["url"] == f"{ENDPOINT}/agents" and call["scope"] == "https://ai.azure.com/.default"
    assert call["params"] == {"api-version": "v1", "kind": "hosted", "limit": "100"}


def test_discover_agents_follows_pagination_and_writes_the_out_file(tmp_path):
    azure = FakeAuthorization()
    azure.agent_pages = [[agent("a-one", AGENT_A)], [agent("b-two", AGENT_B)]]
    out = tmp_path / "principals.json"
    assert run(azure, "discover-agents", "--project-endpoint", ENDPOINT, "--roles", "tools,models", "--out", str(out)) == 0
    assert [call["params"].get("after") for call in azure.calls] == [None, "1"]
    principals = common.load_principals(out)
    assert [(row["label"], row["roles"]) for row in principals] == [("a-one:v1", ["models", "tools"]),
                                                                   ("b-two:v1", ["models", "tools"])]


def test_discovered_principals_feed_the_plan(tmp_path, capsys):
    azure = FakeAuthorization()
    azure.agent_pages = [[agent("a-one", AGENT_A)]]
    out = tmp_path / "principals.json"
    assert run(azure, "discover-agents", "--project-endpoint", ENDPOINT, "--out", str(out)) == 0
    capsys.readouterr()
    assert run(azure, "plan", "--principals", str(out)) == 0
    assert json.loads(capsys.readouterr().out)["summary"] == {"wouldCreate": 1, "alreadyPresent": 0}


@pytest.mark.parametrize("endpoint", [
    "http://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/p",
    "https://evil.example.com/api/projects/p",
    "https://aisvc-zurlo-northwind.services.ai.azure.com.evil.example.com/api/projects/p",
    "https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/p?x=1",
    "https://user:pass@aisvc-zurlo-northwind.services.ai.azure.com/api/projects/p",
    "https://aisvc-zurlo-northwind.services.ai.azure.com/other",
])
def test_discover_agents_rejects_unexpected_endpoints(endpoint, capsys):
    azure = FakeAuthorization()
    assert run(azure, "discover-agents", "--project-endpoint", endpoint) == 1
    assert "--project-endpoint" in capsys.readouterr().err and not azure.calls


def test_discover_agents_rejects_unknown_roles():
    with pytest.raises(SystemExit) as stop:
        grants.main(["discover-agents", "--project-endpoint", ENDPOINT, "--roles", "owner"], runner=FakeAuthorization())
    assert stop.value.code == 2


# --- guard rails ----------------------------------------------------------------------------------


def test_only_revoke_can_delete_and_nothing_else_is_modified():
    source = (ROOT / "scripts" / "ai-gateway-grants.py").read_text(encoding="utf-8")
    assert source.count("delete_role_assignment(") == 1
    assert not re.search(r"""["'](PATCH|POST)["']""", source)


def test_grants_scope_is_always_the_gateway_resource(tmp_path):
    azure = FakeAuthorization()
    assert run(azure, "apply", "--preset", "app-uami", "--yes", "--receipt", str(tmp_path / "r.json")) == 0
    scopes = {urlsplit(call["url"]).path.split("/providers/Microsoft.Authorization")[0] for call in azure.calls}
    assert scopes == {GATEWAY}


def option_strings(parser):
    found = set()
    for action in parser._actions:
        found.update(action.option_strings)
        if isinstance(getattr(action, "choices", None), dict):
            for subparser in action.choices.values():
                found |= option_strings(subparser)
    return found


def test_script_uses_only_the_injected_runner_and_takes_no_secret_arguments():
    source = (ROOT / "scripts" / "ai-gateway-grants.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:import|from)\s+(?:subprocess|httpx|requests|urllib\.request|http\.client|socket|azure)\b",
                         source, re.MULTILINE)
    options = option_strings(grants.build_parser())
    assert {"--principals", "--preset", "--yes", "--receipt", "--project-endpoint", "--roles"} <= options
    assert not [option for option in options if re.search(r"key|token|password|secret", option)]


def test_help_documents_the_required_roles(capsys):
    with pytest.raises(SystemExit) as stop:
        grants.main(["--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    assert "User Access Administrator" in text and "Role Based Access Control Administrator" in text
    assert "AI Gateway Models User" in text and "AI Gateway Tools User" in text and "--preset app-uami" in text
