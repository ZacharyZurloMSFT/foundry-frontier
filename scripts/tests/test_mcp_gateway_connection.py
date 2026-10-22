"""Gateway MCP project connection: body building, redaction and the apply path (offline)."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


module = load("connect_mcp_gateway", "connect-mcp-gateway.py")
knowledge = load("connect_knowledge_base_for_mcp_gateway", "connect-knowledge-base.py")

KEY = "gw-runtime-key-0123456789abcdef"
PROJECT = ("/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-ai"
           "/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind/projects/nw-insurance-claims")
TARGET = "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp"
ARGUMENTS = ["--project-arm-id", PROJECT, "--toolserver", "northwind-insurance-lookup",
             "--connection-name", "nw-insurance-claims-gateway-mcp"]
URL = (f"https://management.azure.com{PROJECT}/connections/nw-insurance-claims-gateway-mcp"
       "?api-version=2025-10-01-preview")


class Runner:
    """Records requests instead of calling Azure."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = {} if response is None else response
        self.error = error

    def __call__(self, method, url, body):
        self.calls.append((method, url, json.loads(json.dumps(body))))
        if self.error is not None:
            raise self.error
        return self.response


def test_target_is_the_gateway_tool_server_endpoint_without_a_trailing_slash():
    assert module.gateway_target("northwind-insurance-lookup") == TARGET
    assert module.gateway_target("northwind-insurance-knowledge", "https://other.example.net/") == (
        "https://other.example.net/default/toolservers/northwind-insurance-knowledge/mcp")
    with pytest.raises(ValueError):
        module.gateway_target("Bad Name")


def test_connection_url_is_project_scoped_and_validated():
    assert module.connection_url(PROJECT, "nw-insurance-claims-gateway-mcp") == URL
    assert module.connection_url(PROJECT + "/", "nw-insurance-claims-gateway-mcp", "2025-12-01").endswith(
        "?api-version=2025-12-01")
    with pytest.raises(ValueError, match="--project-arm-id"):
        module.connection_url(PROJECT.rsplit("/projects/", 1)[0], "nw-conn")
    for broken in (PROJECT.replace("137c905b-7734-479e-acc5-ab8222462936", "not-a-guid"),
                   PROJECT.replace("rg-northwind-ai", "rg&calc"), PROJECT + "/extra"):
        with pytest.raises(ValueError, match="--project-arm-id"):
            module.connection_url(broken, "nw-conn")
    for name in ("ab", "x" * 34, "has space", "-leading"):
        with pytest.raises(ValueError, match="connection name"):
            module.connection_url(PROJECT, name)


def test_body_is_a_remote_tool_with_the_runtime_key_under_api_key():
    body = module.build_connection_body("nw-insurance-claims-gateway-mcp", TARGET, KEY)
    assert body == {
        "name": "nw-insurance-claims-gateway-mcp",
        "type": "Microsoft.CognitiveServices/accounts/projects/connections",
        "properties": {
            "authType": "CustomKeys", "category": "RemoteTool", "target": TARGET, "isSharedToAll": True,
            "credentials": {"keys": {"api-key": KEY}}, "metadata": {"ApiType": "Azure"},
        },
    }
    with pytest.raises(ValueError, match="runtime key"):
        module.build_connection_body("nw-insurance-claims-gateway-mcp", TARGET, "")


def test_body_follows_the_knowledge_base_connection_pattern():
    class Client:
        def put(self, url, headers, json):
            self.url, self.body = url, json
            return type("Response", (), {"status_code": 200, "text": ""})()

    client = Client()
    knowledge._connect_one(client, "token", PROJECT, "nw-insurance-claims-gateway-mcp",
                           "https://search.example.net", "search-key", "northwind-insurance-kb")
    ours = module.build_connection_body("nw-insurance-claims-gateway-mcp", TARGET, KEY)
    assert client.body["type"] == ours["type"] and client.body["name"] == ours["name"]
    for field in ("authType", "category", "isSharedToAll", "metadata"):
        assert client.body["properties"][field] == ours["properties"][field]
    assert set(client.body["properties"]) == set(ours["properties"])
    assert set(client.body["properties"]["credentials"]["keys"]) == set(ours["properties"]["credentials"]["keys"])
    assert client.url == URL
    assert knowledge._ARM_API == module.ARM_API_VERSION


def test_redaction_masks_the_key_without_touching_the_original():
    body = module.build_connection_body("nw-insurance-claims-gateway-mcp", TARGET, KEY)
    redacted = module.redact(body)
    assert redacted["properties"]["credentials"] == {"keys": {"api-key": "<redacted>"}}
    assert KEY not in json.dumps(redacted)
    assert body["properties"]["credentials"]["keys"]["api-key"] == KEY
    assert module.secret_values(body) == [KEY]
    assert module.scrub(f"rejected {KEY}", [KEY]) == "rejected <redacted>"
    tricky = 'key"with\\chars'
    escaped = json.dumps(tricky)[1:-1]
    assert module.scrub(f"plain {tricky} and json {escaped}", [tricky]) == "plain <redacted> and json <redacted>"


def test_dry_run_is_the_default_and_never_prints_the_key(capsys):
    runner = Runner(error=AssertionError("a dry run must not send anything"))
    code = module.main(ARGUMENTS, runner=runner, env={"AI_GATEWAY_RUNTIME_KEY": KEY})
    captured = capsys.readouterr()
    assert code == 0 and runner.calls == []
    assert KEY not in captured.out + captured.err
    assert "DRY RUN" in captured.out and f"PUT {URL}" in captured.out and '"api-key": "<redacted>"' in captured.out
    entry = json.loads(captured.out.strip().splitlines()[-1])
    assert entry == {"url": TARGET, "tool_prefix": "<namespace>_", "connection": "nw-insurance-claims-gateway-mcp"}


def test_dry_run_without_the_key_says_apply_will_refuse(capsys):
    assert module.main(ARGUMENTS, runner=Runner(), env={}) == 0
    assert "AI_GATEWAY_RUNTIME_KEY is not set" in capsys.readouterr().out


def test_apply_sends_one_put_with_the_real_key_and_never_prints_it(capsys):
    response = {"id": f"{PROJECT}/connections/nw-insurance-claims-gateway-mcp",
                "properties": {"category": "RemoteTool", "target": TARGET, "authType": "CustomKeys"}}
    runner = Runner(response=response)
    code = module.main([*ARGUMENTS, "--apply"], runner=runner, env={"AI_GATEWAY_RUNTIME_KEY": KEY})
    captured = capsys.readouterr()
    assert code == 0
    ((method, url, body),) = runner.calls
    assert (method, url) == ("PUT", URL)
    assert body["properties"]["credentials"] == {"keys": {"api-key": KEY}}
    assert body["properties"]["target"] == TARGET
    assert KEY not in captured.out + captured.err
    assert "Connection applied" in captured.out


def test_apply_reads_the_key_from_the_named_variable_only(capsys):
    runner = Runner()
    code = module.main([*ARGUMENTS, "--key-env", "MY_GATEWAY_KEY", "--apply"], runner=runner,
                       env={"AI_GATEWAY_RUNTIME_KEY": "wrong", "MY_GATEWAY_KEY": KEY})
    assert code == 0 and runner.calls[0][2]["properties"]["credentials"] == {"keys": {"api-key": KEY}}
    capsys.readouterr()
    refused = Runner()
    assert module.main([*ARGUMENTS, "--key-env", "MY_GATEWAY_KEY", "--apply"], runner=refused,
                       env={"AI_GATEWAY_RUNTIME_KEY": KEY}) == 2
    assert refused.calls == []
    err = capsys.readouterr().err
    assert "MY_GATEWAY_KEY" in err and KEY not in err


def test_apply_refuses_to_run_without_the_key(capsys):
    runner = Runner()
    assert module.main([*ARGUMENTS, "--apply"], runner=runner, env={"AI_GATEWAY_RUNTIME_KEY": "  "}) == 2
    assert runner.calls == []
    assert "AI_GATEWAY_RUNTIME_KEY" in capsys.readouterr().err


def test_runner_failures_are_reported_without_the_key(capsys):
    runner = Runner(error=RuntimeError(f"az rest PUT failed: rejected {KEY}"))
    code = module.main([*ARGUMENTS, "--apply"], runner=runner, env={"AI_GATEWAY_RUNTIME_KEY": KEY})
    captured = capsys.readouterr()
    assert code == 1 and KEY not in captured.out + captured.err
    assert "<redacted>" in captured.err


@pytest.mark.parametrize("replacement", [
    ("--toolserver", "Bad Name"),
    ("--connection-name", "ab"),
    ("--project-arm-id", "/subscriptions/s/resourceGroups/rg"),
])
def test_invalid_arguments_exit_with_status_two_before_any_request(replacement, capsys):
    arguments = list(ARGUMENTS)
    arguments[arguments.index(replacement[0]) + 1] = replacement[1]
    runner = Runner()
    assert module.main([*arguments, "--apply"], runner=runner, env={"AI_GATEWAY_RUNTIME_KEY": KEY}) == 2
    assert runner.calls == [] and "ERROR" in capsys.readouterr().err


def test_key_env_must_be_a_variable_name_and_a_pasted_key_is_not_echoed(capsys):
    with pytest.raises(SystemExit) as raised:
        module.main([*ARGUMENTS, "--key-env", KEY], runner=Runner(), env={})
    assert raised.value.code == 2
    assert KEY not in capsys.readouterr().err


def test_az_rest_passes_the_body_by_file_and_keeps_the_key_off_the_command_line(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        reference = argv[argv.index("--body") + 1]
        path = Path(kwargs["cwd"]) / reference.removeprefix("@")
        seen["reference"], seen["path"] = reference, path
        seen["body"] = json.loads(path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(argv, 0, stdout='{"id": "connection-id"}', stderr="")

    monkeypatch.setattr(module.shutil, "which", lambda name: "az-fixture")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    body = module.build_connection_body("nw-insurance-claims-gateway-mcp", TARGET, KEY)
    assert module.az_rest("PUT", URL, body) == {"id": "connection-id"}
    assert seen["argv"][:2] == ["az-fixture", "rest"]
    assert seen["reference"] == "@body.json"
    assert KEY not in " ".join(seen["argv"])
    assert seen["body"] == body
    assert not seen["path"].exists(), "the body file must be removed once az returns"


@pytest.mark.parametrize("url", [
    "https://management.azure.com/x&calc", "https://management.azure.com/x|y", 'https://management.azure.com/x"y',
    "https://management.azure.com/%PATH%", "https://management.azure.com/x\ny", "https://management.azure.com/x^y",
])
def test_az_rest_refuses_arguments_that_are_unsafe_for_the_cmd_launcher(monkeypatch, url):
    monkeypatch.setattr(module.shutil, "which", lambda name: "az-fixture")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("az must not be launched"))
    with pytest.raises(RuntimeError, match="unsafe"):
        module.az_rest("PUT", url, {"properties": {"credentials": {"keys": {"api-key": KEY}}}})


@pytest.mark.parametrize("failure,message", [
    (subprocess.TimeoutExpired(["az", "--body", "@body.json"], 180), "timed out"),
    (FileNotFoundError(2, "No such file or directory"), "could not be started"),
])
def test_az_rest_reports_timeouts_and_launch_failures_plainly(monkeypatch, failure, message):
    def fake_run(argv, **kwargs):
        raise failure

    monkeypatch.setattr(module.shutil, "which", lambda name: "az-fixture")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match=message) as raised:
        module.az_rest("PUT", URL, {"properties": {"credentials": {"keys": {"api-key": KEY}}}})
    assert KEY not in str(raised.value)


def test_az_rest_errors_never_contain_the_key(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, stdout="", stderr=f"Forbidden for {KEY}")

    monkeypatch.setattr(module.shutil, "which", lambda name: "az-fixture")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    body = module.build_connection_body("nw-insurance-claims-gateway-mcp", TARGET, KEY)
    with pytest.raises(RuntimeError) as raised:
        module.az_rest("PUT", URL, body)
    assert KEY not in str(raised.value) and "exit code 3" in str(raised.value)


def test_az_rest_requires_the_azure_cli(monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Azure CLI"):
        module.az_rest("PUT", URL, {})
