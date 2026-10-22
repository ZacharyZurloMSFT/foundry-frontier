"""Pin the alias and URL rules observed on the live AI Gateway tier."""
import importlib.util
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ai_gateway_common", ROOT / "scripts" / "ai_gateway_common.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


@pytest.mark.parametrize("deployment,expected", [
    ("gpt-4o", "aisvc-zurlo-northwind/gpt-4o"),
    ("gpt-5.4", "aisvc-zurlo-northwind/gpt-5-4"),
    ("gpt-5.4-mini", "aisvc-zurlo-northwind/gpt-5-4-mini"),
    ("gpt-5.6-luna", "aisvc-zurlo-northwind/gpt-5-6-luna"),
    ("Kimi-K2.6", "aisvc-zurlo-northwind/kimi-k2-6"),
    ("text-embedding-3-small", "aisvc-zurlo-northwind/text-embedding-3-small"),
])
def test_model_alias_matches_live_registrations(deployment, expected):
    assert common.model_alias(deployment) == expected


def test_bare_deployment_names_are_not_gateway_aliases():
    # The live gateway answers 404 for bare names; only <provider>/<registration> routes.
    assert common.model_alias("gpt-5.4") != "gpt-5.4"


@pytest.mark.parametrize("deployment,provider", [("", common.MODEL_PROVIDER), ("gpt-4o", " ")])
def test_alias_requires_deployment_and_provider(deployment, provider):
    with pytest.raises(ValueError):
        common.model_alias(deployment, provider)


def test_urls_and_tool_names():
    assert common.models_base_url() == "https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/"
    assert common.toolserver_url("northwind-insurance-lookup") == (
        "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp")
    assert common.prefixed_tool("lookup", "lookup_policy") == "lookup_lookup_policy"
    assert common.prefixed_tool("review", "draft_claim_action") == "review_draft_claim_action"
    with pytest.raises(ValueError):
        common.toolserver_url("Bad Name")


# --- shared control-plane plumbing ----------------------------------------------------------------

SECRET = "s3cr3t-value-that-must-never-leak"
GATEWAY_MI = "47816336-79ee-4e60-9ab4-7c03e5e4d2b0"


class FakeCli:
    """Stands in for subprocess.run and records what the runner hands to `az rest`."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        self.calls = []
        self.body_path = None
        self.body_text = None

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        for index, argument in enumerate(argv):
            if argument == "--body":
                self.body_path = Path(argv[index + 1].removeprefix("@"))
                self.body_text = self.body_path.read_text(encoding="utf-8")
        return self.result

    @property
    def argv(self):
        return self.calls[-1][0]


def runner_for(cli, tmp_path):
    return common.AzRestRunner(az="az-test", temp_dir=tmp_path, run=cli)


def test_constants_pin_the_live_coordinates_and_principals():
    assert common.GATEWAY_ID == (
        "/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-ai"
        "/providers/Microsoft.ApiManagement/service/ai-gateway-northwind")
    assert common.FOUNDRY_ACCOUNT_ID == (
        "/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-ai"
        "/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind")
    assert common.APP_UAMI_PRINCIPAL_ID == "4345f6c9-3c1a-4f4f-9d58-e75d7fa6ee17"
    assert common.GATEWAY_IDENTITY_PRINCIPAL_ID == GATEWAY_MI
    assert common.GATEWAY_ROLES == {"models": common.ROLE_MODELS_USER, "tools": common.ROLE_TOOLS_USER}
    assert set(common.ROLE_NAMES) == {common.ROLE_MODELS_USER, common.ROLE_TOOLS_USER, common.ROLE_GATEWAY_USER,
                                      common.ROLE_FOUNDRY_USER, common.ROLE_OPENAI_USER}
    assert common.ROLE_NAMES[common.ROLE_MODELS_USER] == "AI Gateway Models User"


def test_canonical_json_and_hashes_ignore_key_order():
    assert common.canonical_json({"b": [1, 2], "a": "é"}) == '{"a":"\\u00e9","b":[1,2]}'
    assert common.sha256_json({"a": 1, "b": 2}) == common.sha256_json({"b": 2, "a": 1})
    assert common.sha256_json({"a": 1}) != common.sha256_json({"a": 2})
    assert common.pretty_json({"b": 1, "a": 2}) == '{\n  "a": 2,\n  "b": 1\n}\n'


def test_assignment_names_follow_the_hosted_deploy_scheme():
    scope, principal = common.GATEWAY_ID, "3ce7bef0-2680-4589-8cb3-c0ba6e0bb2ff"
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.lower()}|{principal}|{common.ROLE_MODELS_USER}"))
    assert common.assignment_name(scope, principal, common.ROLE_MODELS_USER) == expected
    assert common.assignment_name(scope.upper(), principal, common.ROLE_MODELS_USER) == expected


@pytest.mark.parametrize(("parent", "child", "expected"), [
    ("/subscriptions/s", "/subscriptions/s", True),
    ("/subscriptions/s", "/subscriptions/s/resourceGroups/rg/providers/x/y", True),
    ("/SUBSCRIPTIONS/S/", "/subscriptions/s/resourcegroups/rg", True),
    ("/subscriptions/s/resourceGroups/rg", "/subscriptions/s", False),
    ("/subscriptions/s/resourceGroups/rg", "/subscriptions/s/resourceGroups/rg2", False),
    ("/subscriptions/s/resourceGroups/rg", "/subscriptions/s/resourceGroups/rg-other/providers/x", False),
    ("/", "/subscriptions/s", True),
])
def test_scope_covers(parent, child, expected):
    assert common.scope_covers(parent, child) is expected


def test_guid_validation():
    assert common.is_guid("3ce7bef0-2680-4589-8cb3-c0ba6e0bb2ff") and common.is_guid(GATEWAY_MI.upper())
    assert not any(common.is_guid(value) for value in ("", "x", None, 5, "3ce7bef0-2680-4589-8cb3-c0ba6e0bb2f"))


def test_placeholders_resolve_from_the_environment_without_echoing_values():
    assert common.placeholder_names("a ${ONE} b ${TWO_2} ${lower} $THREE") == ["ONE", "TWO_2"]
    assert common.resolve_placeholders("k=${ONE}&${TWO_2}", {"ONE": "1", "TWO_2": "x\\y"}) == "k=1&x\\y"
    assert common.resolve_placeholders("no placeholder", {}) == "no placeholder"
    with pytest.raises(common.GatewayError, match="ONE, TWO_2") as error:
        common.resolve_placeholders("${ONE}${TWO_2}", {"OTHER": SECRET, "TWO_2": ""})
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(("stderr", "status", "code"), [
    ('ERROR: Not Found({"error":{"code":"ResourceNotFound","message":"ToolServer not found."}})', 404, "ResourceNotFound"),
    ('Forbidden({"error":{"code":"AuthorizationFailed","message":"no"}})', 403, "AuthorizationFailed"),
    ('Precondition Failed({"error":{"code":"PreconditionFailed"}})', 412, "PreconditionFailed"),
    ('ERROR: Conflict({"error":{"code":"RoleAssignmentExists"}})', 409, "RoleAssignmentExists"),
    ("ERROR: Please run 'az login' to setup account.", None, "AuthenticationRequired"),
    ('Odd reason({"error":{"code":"ParentResourceNotFound"}})', 404, "ParentResourceNotFound"),
    ("ERROR: Not Found", 404, None),
    ("WARNING: something odd\nERROR: Too Many Requests\n", 429, None),
    ("something unrelated", None, None),
])
def test_parse_cli_failure_reads_status_and_code(stderr, status, code):
    parsed_status, parsed_code, _ = common.parse_cli_failure(stderr)
    assert (parsed_status, parsed_code) == (status, code)


def test_parse_cli_failure_extracts_the_service_message():
    _, _, message = common.parse_cli_failure('Forbidden({"error":{"code":"X","message":"line \\"quoted\\" text"}})')
    assert message == 'line "quoted" text'


def test_az_runner_builds_az_rest_commands(tmp_path):
    cli = FakeCli(stdout='{"value": []}')
    response = runner_for(cli, tmp_path)(
        "get", "https://management.azure.com/subscriptions/s/x", params={"api-version": "2022-04-01", "$filter": "atScope()"},
        headers={"Accept": "application/json, text/event-stream"}, scope="https://azure-api.net/aigateway/.default")
    assert cli.argv == [
        "az-test", "rest", "--method", "get", "--url", "https://management.azure.com/subscriptions/s/x",
        "--output", "json", "--only-show-errors",
        "--url-parameters", "api-version=2022-04-01", "$filter=atScope()",
        "--headers", "Accept=application/json, text/event-stream",
        "--resource", "https://azure-api.net/aigateway"]
    options = cli.calls[0][1]
    assert options["capture_output"] is True and options["check"] is False and options["stdin"] is subprocess.DEVNULL
    assert options["encoding"] == "utf-8" and options["timeout"] == 120
    assert response.body == {"value": []} and response.status == 200 and cli.body_path is None


def test_az_runner_keeps_bodies_out_of_argv_and_removes_the_temp_file(tmp_path):
    cli = FakeCli(stdout="")
    response = runner_for(cli, tmp_path)("PUT", "https://management.azure.com/x", body={"key": SECRET})
    assert SECRET not in " ".join(cli.argv)
    assert cli.argv[-2] == "--body" and cli.argv[-1].startswith("@") and str(tmp_path) in cli.argv[-1]
    assert json.loads(cli.body_text) == {"key": SECRET}
    assert not cli.body_path.exists() and not list(tmp_path.iterdir())
    assert response.body is None and response.text == ""


def test_az_runner_removes_the_temp_file_when_the_cli_fails(tmp_path):
    cli = FakeCli(returncode=1, stderr="Bad Request({})")
    with pytest.raises(common.RunnerError):
        runner_for(cli, tmp_path)("PUT", "https://management.azure.com/x", body={"key": SECRET})
    assert not list(tmp_path.iterdir())


def test_az_runner_withholds_response_content_for_requests_with_a_body(tmp_path):
    stderr = f'Bad Request({{"error":{{"code":"InvalidRequest","message":"echo {SECRET}"}}}})'
    with pytest.raises(common.RunnerError) as put_error:
        runner_for(FakeCli(returncode=1, stderr=stderr), tmp_path)("PUT", "https://management.azure.com/x?y", body={"k": SECRET})
    assert SECRET not in str(put_error.value) and "echo" not in str(put_error.value)
    assert (put_error.value.status, put_error.value.code) == (400, "InvalidRequest")
    assert "HTTP 400" in str(put_error.value) and "[InvalidRequest]" in str(put_error.value)
    with pytest.raises(common.RunnerError) as get_error:
        runner_for(FakeCli(returncode=1, stderr=stderr.replace(SECRET, "details")), tmp_path)("GET", "https://management.azure.com/x")
    assert "echo details" in str(get_error.value)
    assert "?y" not in str(put_error.value)


def test_az_runner_hints_at_az_login_when_signed_out(tmp_path):
    cli = FakeCli(returncode=1, stderr="ERROR: Please run 'az login' to setup account.")
    with pytest.raises(common.RunnerError, match="az login") as error:
        runner_for(cli, tmp_path)("GET", "https://management.azure.com/x")
    assert error.value.code == "AuthenticationRequired" and error.value.status is None


def test_az_runner_returns_json_text_and_etag(tmp_path):
    assert runner_for(FakeCli(stdout='{"etag": "abc", "a": 1}'), tmp_path)("GET", "https://x.example/y").etag == "abc"
    sse = "event: message\ndata: {}\n\n"
    response = runner_for(FakeCli(stdout=sse), tmp_path)("POST", "https://x.example/mcp", body={"a": 1})
    assert response.body is None and response.text == sse and response.etag is None
    assert runner_for(FakeCli(stdout='["x"]'), tmp_path)("GET", "https://x.example/y").etag is None


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"method": "GET", "url": "http://management.azure.com/x"}, "Only HTTPS"),
    ({"method": "TRACE", "url": "https://management.azure.com/x"}, "Unsupported HTTP method"),
    ({"method": "GET", "url": "https://management.azure.com/x", "headers": {"X": "a&b"}}, "unsafe"),
    ({"method": "GET", "url": "https://management.azure.com/x", "headers": {"X": 'a"b'}}, "unsafe"),
    ({"method": "GET", "url": "https://management.azure.com/x", "params": {"a": "100%"}}, "unsafe"),
    ({"method": "GET", "url": "https://management.azure.com/x", "headers": {"X": "a|b"}}, "unsafe"),
])
def test_az_runner_rejects_unsafe_requests_before_running_anything(tmp_path, kwargs, message):
    cli = FakeCli()
    kwargs = dict(kwargs)
    method, url = kwargs.pop("method"), kwargs.pop("url")
    with pytest.raises((common.GatewayError, ValueError), match=message):
        runner_for(cli, tmp_path)(method, url, **kwargs)
    assert not cli.calls


def _windows_shim(tmp_path, *, with_python):
    wbin = tmp_path / "CLI2" / "wbin"
    wbin.mkdir(parents=True)
    shim = wbin / "az.cmd"
    shim.write_text("rem shim", encoding="utf-8")
    if with_python:
        (tmp_path / "CLI2" / "python.exe").write_text("", encoding="utf-8")
    return shim


def test_cli_command_starts_the_bundled_python_for_a_windows_shim(tmp_path):
    shim = _windows_shim(tmp_path, with_python=True)
    command, via_shim = common.cli_command(str(shim), windows=True)
    assert command == [str((tmp_path / "CLI2" / "python.exe").resolve()), "-IBm", "azure.cli"]
    assert via_shim is False


def test_cli_command_keeps_the_shim_when_the_bundled_python_is_missing(tmp_path):
    shim = _windows_shim(tmp_path, with_python=False)
    assert common.cli_command(str(shim), windows=True) == ([str(shim)], True)


def test_cli_command_leaves_other_launchers_alone(tmp_path):
    shim = _windows_shim(tmp_path, with_python=True)
    assert common.cli_command("/usr/bin/az", windows=False) == (["/usr/bin/az"], False)
    assert common.cli_command(str(shim), windows=False) == ([str(shim)], False)
    assert common.cli_command("az", windows=True) == (["az"], False)


def test_az_runner_allows_parentheses_when_the_cli_is_started_directly(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "cli_command", lambda executable: (["python", "-IBm", "azure.cli"], False))
    cli = FakeCli(stdout='{"value": []}')
    runner_for(cli, tmp_path)("GET", "https://management.azure.com/x", params={"$filter": "atScope()"})
    assert cli.argv[:3] == ["python", "-IBm", "azure.cli"] and "$filter=atScope()" in cli.argv


def test_az_runner_refuses_parentheses_while_a_cmd_shim_launches_the_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "cli_command", lambda executable: ([executable], True))
    cli = FakeCli()
    with pytest.raises(common.GatewayError, match="unsafe"):
        runner_for(cli, tmp_path)("GET", "https://management.azure.com/x", params={"$filter": "atScope()"})
    assert not cli.calls


def test_az_runner_reports_timeouts_and_a_missing_cli(tmp_path, monkeypatch):
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(common.GatewayError, match="timed out; do not blindly retry"):
        runner_for(slow, tmp_path)("PUT", "https://management.azure.com/x", body={})
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(common.shutil, "which", lambda name: None)
    with pytest.raises(common.GatewayError, match="Azure CLI"):
        common.AzRestRunner(run=FakeCli())("GET", "https://management.azure.com/x")


def test_response_repr_never_shows_the_payload():
    assert SECRET not in repr(common.Response(200, {"k": SECRET}, SECRET, SECRET))


class PagedArm:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, method, url, *, params=None, **kwargs):
        self.calls.append((method, url, dict(params or {})))
        return common.Response(200, self.pages[len(self.calls) - 1])


def test_list_arm_follows_next_link_on_the_same_host():
    runner = PagedArm([
        {"value": [{"name": "a"}], "nextLink": "https://management.azure.com/x/items?api-version=1&$skiptoken=t%2B1"},
        {"value": [{"name": "b"}, "ignored"]},
    ])
    assert common.list_arm(runner, "https://management.azure.com/x/items", {"api-version": "1"}) == [
        {"name": "a"}, {"name": "b"}]
    assert runner.calls[1] == ("GET", "https://management.azure.com/x/items", {"api-version": "1", "$skiptoken": "t+1"})


@pytest.mark.parametrize(("pages", "message"), [
    ([{"value": [], "nextLink": "https://evil.example.com/x/items?api-version=1"}], "another host"),
    ([{"value": [], "nextLink": "http://management.azure.com/x/items"}], "another host"),
    ([{"nope": []}], "Unexpected list response"),
    ([[]], "Unexpected list response"),
])
def test_list_arm_refuses_foreign_continuations_and_bad_payloads(pages, message):
    with pytest.raises(common.GatewayError, match=message):
        common.list_arm(PagedArm(pages), "https://management.azure.com/x/items")


def test_list_arm_stops_at_the_page_limit():
    page = {"value": [], "nextLink": "https://management.azure.com/x/items?p=1"}
    with pytest.raises(common.GatewayError, match="page limit"):
        common.list_arm(PagedArm([page] * 3), "https://management.azure.com/x/items", max_pages=3)


def test_role_assignment_helpers_use_the_authorization_api():
    item = {"id": f"{common.GATEWAY_ID}/providers/Microsoft.Authorization/roleAssignments/n1", "name": "n1",
            "properties": {"roleDefinitionId": common.role_definition_id(common.ROLE_MODELS_USER),
                           "principalId": GATEWAY_MI.upper(), "principalType": "ServicePrincipal"}}
    assert common.normalize_role_assignment(item) == {
        "name": "n1", "scope": common.GATEWAY_ID, "principalId": GATEWAY_MI, "principalType": "ServicePrincipal",
        "roleDefinitionId": common.role_definition_id(common.ROLE_MODELS_USER), "roleId": common.ROLE_MODELS_USER}
    assert common.normalize_role_assignment({"name": "n2", "properties": {}}) is None
    runner = PagedArm([{"value": [item, {"name": "broken"}]}])
    rows = common.list_role_assignments(runner, common.GATEWAY_ID)
    assert [row["name"] for row in rows] == ["n1"]
    assert runner.calls[0] == ("GET", f"https://management.azure.com{common.GATEWAY_ID}/providers/Microsoft.Authorization/roleAssignments",
                               {"api-version": "2022-04-01", "$filter": "atScope()"})


def test_put_and_delete_role_assignment_outcomes():
    calls = []

    def runner(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "PUT" and url.endswith("/exists"):
            raise common.RunnerError("x", status=409, code="RoleAssignmentExists")
        if method == "PUT" and url.endswith("/denied"):
            raise common.RunnerError("x", status=403, code="AuthorizationFailed")
        if method == "DELETE" and url.endswith("/gone"):
            raise common.RunnerError("x", status=404)
        return common.Response(200)

    scope = common.FOUNDRY_ACCOUNT_ID
    assert common.put_role_assignment(runner, scope, "new", GATEWAY_MI, "ServicePrincipal", common.ROLE_OPENAI_USER) == "created"
    assert common.put_role_assignment(runner, scope, "exists", GATEWAY_MI, "ServicePrincipal", common.ROLE_OPENAI_USER) == "exists"
    with pytest.raises(common.RunnerError):
        common.put_role_assignment(runner, scope, "denied", GATEWAY_MI, "ServicePrincipal", common.ROLE_OPENAI_USER)
    assert common.delete_role_assignment(runner, scope, "present") is True
    assert common.delete_role_assignment(runner, scope, "gone") is False
    assert calls[0][2]["body"] == {"properties": {
        "roleDefinitionId": common.role_definition_id(common.ROLE_OPENAI_USER), "principalId": GATEWAY_MI,
        "principalType": "ServicePrincipal"}}
    assert calls[0][2]["params"] == {"api-version": "2022-04-01"}


def test_get_role_assignment_distinguishes_absent_from_malformed():
    def absent(method, url, **kwargs):
        raise common.RunnerError("x", status=404)

    def malformed(method, url, **kwargs):
        return common.Response(200, {"name": "n"})

    assert common.get_role_assignment(absent, common.GATEWAY_ID, "n") is None
    with pytest.raises(common.GatewayError, match="Unexpected role assignment"):
        common.get_role_assignment(malformed, common.GATEWAY_ID, "n")


def test_json_files_are_never_overwritten_by_the_exclusive_writer(tmp_path):
    target = tmp_path / "nested" / "receipt.json"
    common.write_json_exclusive(target, {"b": 1, "a": 2})
    assert target.read_text(encoding="utf-8") == '{\n  "a": 2,\n  "b": 1\n}\n'
    with pytest.raises(common.GatewayError, match="Refusing to overwrite"):
        common.write_json_exclusive(target, {"other": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2, "b": 1}
    common.replace_json(target, {"c": 3})
    assert json.loads(target.read_text(encoding="utf-8")) == {"c": 3} and not (tmp_path / "nested" / "receipt.json.tmp").exists()
    if os.name != "nt":
        assert (target.stat().st_mode & 0o777) == 0o600
    with pytest.raises(common.GatewayError, match="not valid JSON"):
        bad = tmp_path / "bad.json"
        bad.write_text("{", encoding="utf-8")
        common.read_json(bad)
    with pytest.raises(common.GatewayError, match="File not found"):
        common.read_json(tmp_path / "missing.json")


def test_default_receipts_land_in_the_git_ignored_release_state_folder():
    moment = datetime(2026, 10, 22, 12, 30, 5, tzinfo=timezone.utc)
    path = common.default_receipt_path("enforce", moment)
    assert path.name == "ai-gateway-enforce-20261022T123005Z.json" and path.parent.name == ".release-state"
    assert common.iso(moment) == "2026-10-22T12:30:05Z"
    gitignore = (Path(__file__).resolve().parents[2] / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^\.release-state/$", gitignore, re.MULTILINE)


def test_principal_validation_normalises_and_presets_are_known():
    assert common.PRESETS == ("app-uami",)
    assert common.preset_principals("app-uami")[0]["roles"] == ["models"]
    with pytest.raises(common.GatewayError, match="Unknown preset"):
        common.preset_principals("everyone")
    with pytest.raises(common.GatewayError, match="Principals must be"):
        common.validate_principals([])
