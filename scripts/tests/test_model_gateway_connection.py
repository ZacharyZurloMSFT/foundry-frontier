"""AI Gateway model connection: body building, redaction and the apply path (offline)."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("connect_model_gateway", SCRIPTS / "connect-model-gateway.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

KEY = "sk-test-runtime-key-0123456789"
ACCOUNT = ("/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-ai"
           "/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind")
TARGET = "https://ai-gateway-northwind.azure-api.net/default/models/openai/v1"
ALIASES = [
    "aisvc-zurlo-northwind/gpt-4o", "aisvc-zurlo-northwind/gpt-5-4", "aisvc-zurlo-northwind/gpt-5-4-mini",
    "aisvc-zurlo-northwind/gpt-5-6-luna", "aisvc-zurlo-northwind/kimi-k2-6",
]


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


def models_of(body):
    return json.loads(body["properties"]["metadata"]["models"])


def test_default_models_are_the_live_gateway_registrations():
    entries = module.model_entries(module.DEFAULT_DEPLOYMENTS)
    assert [entry["name"] for entry in entries] == ALIASES
    assert [entry["properties"]["model"]["name"] for entry in entries] == [
        "gpt-4o", "gpt-5.4", "gpt-5.4-mini", "gpt-5.6-luna", "Kimi-K2.6"]
    assert all(entry["properties"]["model"]["version"] == "" and entry["properties"]["model"]["format"] == "OpenAI"
               for entry in entries)


def test_provider_and_alias_overrides_and_conflicts():
    entries = module.model_entries(["gpt-4o", "gpt-5.4"], "other", {"gpt-5.4": "other/gpt-5-4-custom"})
    assert [entry["name"] for entry in entries] == ["other/gpt-4o", "other/gpt-5-4-custom"]
    with pytest.raises(ValueError, match="same gateway alias"):
        module.model_entries(["gpt-5.4", "gpt-5-4"])
    with pytest.raises(ValueError, match="at least one model"):
        module.model_entries([])


def test_target_has_no_trailing_slash_and_follows_the_gateway_url():
    assert module.gateway_target() == TARGET
    assert module.gateway_target("https://other.example.net/") == "https://other.example.net/default/models/openai/v1"


def test_resource_ids_and_request_url():
    name = "nw-gateway"
    assert module.connection_resource_id(ACCOUNT, name) == f"{ACCOUNT}/connections/{name}"
    assert module.connection_resource_id(ACCOUNT + "/", name, "nw-insurance-claims") == (
        f"{ACCOUNT}/projects/nw-insurance-claims/connections/{name}")
    assert module.request_url("/x") == "https://management.azure.com/x?api-version=2025-10-01-preview"
    assert module.request_url("/x", "2025-12-01").endswith("?api-version=2025-12-01")
    assert module.account_resource_id() == ACCOUNT


@pytest.mark.parametrize("name", ["ab", "x" * 34, "has space", "-leading", "a/b", ""])
def test_connection_names_follow_the_arm_constraint(name):
    with pytest.raises(ValueError, match="connection name"):
        module.connection_resource_id(ACCOUNT, name)


def test_scope_arguments_are_validated():
    with pytest.raises(ValueError, match="--account-id"):
        module.connection_resource_id("/subscriptions/s/resourceGroups/rg", "nw-gateway")
    with pytest.raises(ValueError, match="--account-id"):
        module.connection_resource_id(ACCOUNT.replace("137c905b-7734-479e-acc5-ab8222462936", "not-a-guid"), "nw-gateway")
    with pytest.raises(ValueError, match="--account-id"):
        module.connection_resource_id(ACCOUNT.replace("rg-northwind-ai", "rg&calc"), "nw-gateway")
    with pytest.raises(ValueError, match="--project"):
        module.connection_resource_id(ACCOUNT, "nw-gateway", "bad/project")


def test_aad_body_matches_the_documented_apim_static_model_shape():
    models = module.model_entries(module.DEFAULT_DEPLOYMENTS)
    body = module.build_connection_body("nw-gateway", models, target=TARGET)
    assert body["name"] == "nw-gateway" and body["type"] == "Microsoft.CognitiveServices/accounts/connections"
    properties = body["properties"]
    assert properties["category"] == "ApiManagement" and properties["target"] == TARGET
    assert properties["authType"] == "AAD" and properties["isSharedToAll"] is True
    assert "credentials" not in properties and "audience" not in properties
    metadata = properties["metadata"]
    assert metadata["deploymentInPath"] == "false"
    assert "inferenceAPIVersion" not in metadata
    # ARM connection metadata values are strings, so the model list is serialized JSON.
    assert all(isinstance(value, str) for value in metadata.values())
    assert models_of(body) == models


def test_project_scope_uses_the_project_connection_type():
    body = module.build_connection_body("nw-gateway", module.model_entries(["gpt-4o"]), target=TARGET,
                                        project="nw-insurance-claims")
    assert body["type"] == "Microsoft.CognitiveServices/accounts/projects/connections"


def test_inference_api_version_is_sent_only_when_asked_for():
    models = module.model_entries(["gpt-4o"])
    body = module.build_connection_body("nw-gateway", models, target=TARGET, inference_api_version="preview")
    assert body["properties"]["metadata"]["inferenceAPIVersion"] == "preview"


def test_project_managed_identity_carries_the_gateway_audience():
    models = module.model_entries(["gpt-4o"])
    body = module.build_connection_body("nw-gateway", models, target=TARGET, entra_auth_type="ProjectManagedIdentity")
    assert body["properties"]["authType"] == "ProjectManagedIdentity"
    assert body["properties"]["audience"] == "https://azure-api.net/aigateway"
    assert "credentials" not in body["properties"]
    other = module.build_connection_body("nw-gateway", models, target=TARGET,
                                         entra_auth_type="ProjectManagedIdentity", audience="https://aud.example")
    assert other["properties"]["audience"] == "https://aud.example"


def test_api_key_body_sends_the_key_in_the_api_key_header():
    models = module.model_entries(["gpt-4o"])
    body = module.build_connection_body("nw-gateway", models, target=TARGET, auth="api-key", api_key=KEY)
    properties = body["properties"]
    assert properties["authType"] == "ApiKey" and properties["credentials"] == {"key": KEY}
    assert json.loads(properties["metadata"]["authConfig"]) == {
        "type": "api_key", "name": "api-key", "format": "{api_key}"}
    with pytest.raises(ValueError, match="runtime key"):
        module.build_connection_body("nw-gateway", models, target=TARGET, auth="api-key")


def test_model_gateway_category_is_api_key_only():
    models = module.model_entries(["gpt-4o"])
    body = module.build_connection_body("nw-gateway", models, target=TARGET, auth="api-key", api_key=KEY,
                                        category="ModelGateway")
    assert body["properties"]["category"] == "ModelGateway"
    with pytest.raises(ValueError, match="only API key"):
        module.build_connection_body("nw-gateway", models, target=TARGET, category="ModelGateway")


@pytest.mark.parametrize("override", [
    {"category": "Nope"}, {"auth": "basic"}, {"entra_auth_type": "ManagedIdentity"},
])
def test_unsupported_choices_are_rejected(override):
    with pytest.raises(ValueError):
        module.build_connection_body("nw-gateway", module.model_entries(["gpt-4o"]), target=TARGET, **override)


def test_redaction_masks_credentials_without_touching_the_original():
    body = module.build_connection_body("nw-gateway", module.model_entries(["gpt-4o"]), target=TARGET,
                                        auth="api-key", api_key=KEY)
    redacted = module.redact(body)
    assert redacted["properties"]["credentials"] == {"key": "<redacted>"}
    assert KEY not in json.dumps(redacted)
    assert body["properties"]["credentials"] == {"key": KEY}
    assert module.secret_values(body) == [KEY]
    assert module.scrub(f"failed with {KEY} in the body", [KEY]) == "failed with <redacted> in the body"
    tricky = 'key"with\\chars'
    escaped = json.dumps(tricky)[1:-1]
    assert module.scrub(f"plain {tricky} and json {escaped}", [tricky]) == "plain <redacted> and json <redacted>"


def test_dry_run_is_the_default_and_prints_nothing_secret(capsys):
    runner = Runner(error=AssertionError("a dry run must not send anything"))
    code = module.main(["--name", "nw-gateway", "--auth", "api-key"], runner=runner,
                       env={"AI_GATEWAY_RUNTIME_KEY": KEY})
    captured = capsys.readouterr()
    assert code == 0 and runner.calls == []
    assert KEY not in captured.out + captured.err
    assert "DRY RUN" in captured.out and '"key": "<redacted>"' in captured.out
    assert f"PUT https://management.azure.com{ACCOUNT}/connections/nw-gateway?api-version=2025-10-01-preview" in captured.out
    assert "gpt-4o -> nw-gateway/aisvc-zurlo-northwind/gpt-4o" in captured.out
    assert "Kimi-K2.6 -> nw-gateway/aisvc-zurlo-northwind/kimi-k2-6" in captured.out
    assert "NOT VERIFIED LIVE" in captured.out


def test_dry_run_without_the_key_still_works_and_says_apply_will_refuse(capsys):
    code = module.main(["--name", "nw-gateway", "--auth", "api-key"], runner=Runner(), env={})
    out = capsys.readouterr().out
    assert code == 0 and "AI_GATEWAY_RUNTIME_KEY is not set" in out


def test_apply_sends_one_put_with_the_real_key_and_never_prints_it(capsys):
    response = {"id": f"{ACCOUNT}/connections/nw-gateway",
                "properties": {"category": "ApiManagement", "target": TARGET, "authType": "ApiKey"}}
    runner = Runner(response=response)
    code = module.main(["--name", "nw-gateway", "--auth", "api-key", "--apply"], runner=runner,
                       env={"AI_GATEWAY_RUNTIME_KEY": KEY})
    captured = capsys.readouterr()
    assert code == 0
    ((method, url, body),) = runner.calls
    assert method == "PUT"
    assert url == f"https://management.azure.com{ACCOUNT}/connections/nw-gateway?api-version=2025-10-01-preview"
    assert body["properties"]["credentials"] == {"key": KEY}
    assert models_of(body)[0]["name"] == "aisvc-zurlo-northwind/gpt-4o"
    assert KEY not in captured.out + captured.err
    assert "Connection applied" in captured.out and TARGET in captured.out


def test_aad_runs_remind_that_the_calling_identity_needs_the_models_user_role(capsys):
    assert module.main(["--name", "nw-gateway"], runner=Runner(), env={}) == 0
    assert "AI Gateway Models User" in capsys.readouterr().out
    assert module.main(["--name", "nw-gateway", "--apply"], runner=Runner(), env={}) == 0
    assert "AI Gateway Models User" in capsys.readouterr().out
    module.main(["--name", "nw-gateway", "--auth", "api-key"], runner=Runner(), env={"AI_GATEWAY_RUNTIME_KEY": KEY})
    assert "AI Gateway Models User" not in capsys.readouterr().out


def test_apply_with_aad_needs_no_secret():
    runner = Runner()
    assert module.main(["--name", "nw-gateway", "--apply"], runner=runner, env={}) == 0
    ((_, _, body),) = runner.calls
    assert body["properties"]["authType"] == "AAD" and "credentials" not in body["properties"]


def test_apply_refuses_to_run_without_the_key(capsys):
    runner = Runner()
    code = module.main(["--name", "nw-gateway", "--auth", "api-key", "--apply"], runner=runner, env={})
    assert code == 2 and runner.calls == []
    assert "AI_GATEWAY_RUNTIME_KEY" in capsys.readouterr().err


def test_runner_failures_are_reported_without_the_key(capsys):
    runner = Runner(error=RuntimeError(f"az rest PUT failed: bad body with {KEY}"))
    code = module.main(["--name", "nw-gateway", "--auth", "api-key", "--apply"], runner=runner,
                       env={"AI_GATEWAY_RUNTIME_KEY": KEY})
    captured = capsys.readouterr()
    assert code == 1 and KEY not in captured.out + captured.err
    assert "<redacted>" in captured.err


def test_name_comes_from_the_environment_and_is_required(capsys):
    assert module.main([], runner=Runner(), env={"AI_GATEWAY_MODEL_CONNECTION": "env-conn"}) == 0
    assert "/connections/env-conn?" in capsys.readouterr().out
    assert module.main([], runner=Runner(), env={}) == 2
    assert "--name" in capsys.readouterr().err


def test_project_models_provider_and_aliases_are_honoured(capsys):
    runner = Runner()
    code = module.main(
        ["--name", "nw-gateway", "--project", "nw-insurance-claims", "--models", "gpt-4o, Kimi-K2.6",
         "--provider", "other", "--aliases", '{"Kimi-K2.6": "other/kimi-custom"}', "--apply"],
        runner=runner, env={})
    assert code == 0
    ((_, url, body),) = runner.calls
    assert "/projects/nw-insurance-claims/connections/nw-gateway?" in url
    assert [entry["name"] for entry in models_of(body)] == ["other/gpt-4o", "other/kimi-custom"]
    capsys.readouterr()
    assert module.main(["--name", "nw-gateway", "--aliases", "nope"], runner=runner, env={}) == 2


def test_provider_and_aliases_default_to_the_shared_environment_contract():
    runner = Runner()
    env = {"AI_GATEWAY_MODEL_PROVIDER": "env-provider",
           "AI_GATEWAY_MODEL_ALIASES": '{"gpt-5.4": "env-provider/gpt-five-four"}'}
    assert module.main(["--name", "nw-gateway", "--models", "gpt-4o,gpt-5.4", "--apply"], runner=runner, env=env) == 0
    ((_, _, body),) = runner.calls
    assert [entry["name"] for entry in models_of(body)] == ["env-provider/gpt-4o", "env-provider/gpt-five-four"]


def test_api_key_env_must_be_a_variable_name_and_a_pasted_key_is_not_echoed(capsys):
    with pytest.raises(SystemExit) as raised:
        module.main(["--name", "nw-gateway", "--auth", "api-key", "--api-key-env", KEY], runner=Runner(), env={})
    assert raised.value.code == 2
    assert KEY not in capsys.readouterr().err


def test_a_custom_key_variable_and_the_entra_canary_options(capsys):
    runner = Runner()
    code = module.main(["--name", "nw-gateway", "--auth", "api-key", "--api-key-env", "MY_GATEWAY_KEY", "--apply"],
                       runner=runner, env={"MY_GATEWAY_KEY": KEY})
    assert code == 0 and runner.calls[0][2]["properties"]["credentials"] == {"key": KEY}
    capsys.readouterr()
    assert module.main(["--name", "nw-gateway", "--entra-auth-type", "ProjectManagedIdentity"],
                       runner=Runner(), env={}) == 0
    assert '"audience": "https://azure-api.net/aigateway"' in capsys.readouterr().out


def test_the_docstring_states_the_payload_is_unverified_and_requires_a_canary():
    doc = " ".join(module.__doc__.split())
    assert "NOT VERIFIED LIVE" in doc and "classic API Management shape" in doc
    assert "confirmed with ONE canary prompt agent before any bulk use" in doc


def test_az_rest_passes_the_body_by_file_and_keeps_the_key_off_the_command_line(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        reference = argv[argv.index("--body") + 1]
        path = Path(kwargs["cwd"]) / reference.removeprefix("@")
        seen["reference"], seen["path"] = reference, path
        seen["body"] = json.loads(path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(argv, 0, stdout='{"id": "connection-id"}', stderr="")

    monkeypatch.setattr(module.shutil, "which", lambda name: "az-fixture")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    body = {"properties": {"credentials": {"key": KEY}}}
    assert module.az_rest("PUT", "https://management.azure.com/x?api-version=1", body) == {"id": "connection-id"}
    argv = seen["argv"]
    assert argv[:2] == ["az-fixture", "rest"] and argv[argv.index("--method") + 1] == "PUT"
    assert argv[argv.index("--url") + 1] == "https://management.azure.com/x?api-version=1"
    assert seen["reference"] == "@body.json"
    assert KEY not in " ".join(argv)
    assert seen["body"] == body
    assert not seen["path"].exists(), "the body file must be removed once az returns"
    assert seen["kwargs"]["check"] is False and seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL and seen["kwargs"]["encoding"] == "utf-8"


@pytest.mark.parametrize("url", [
    "https://management.azure.com/x&calc", "https://management.azure.com/x|y", 'https://management.azure.com/x"y',
    "https://management.azure.com/%PATH%", "https://management.azure.com/x\ny", "https://management.azure.com/x^y",
])
def test_az_rest_refuses_arguments_that_are_unsafe_for_the_cmd_launcher(monkeypatch, url):
    monkeypatch.setattr(module.shutil, "which", lambda name: "az-fixture")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("az must not be launched"))
    with pytest.raises(RuntimeError, match="unsafe"):
        module.az_rest("PUT", url, {"properties": {"credentials": {"key": KEY}}})


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
        module.az_rest("PUT", "https://management.azure.com/x", {"properties": {"credentials": {"key": KEY}}})
    assert KEY not in str(raised.value)


def test_az_rest_errors_never_contain_the_key(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"BadRequest: invalid credential {KEY}")

    monkeypatch.setattr(module.shutil, "which", lambda name: "az-fixture")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as raised:
        module.az_rest("PUT", "https://management.azure.com/x", {"properties": {"credentials": {"key": KEY}}})
    assert KEY not in str(raised.value) and "exit code 1" in str(raised.value)


def test_az_rest_requires_the_azure_cli(monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Azure CLI"):
        module.az_rest("PUT", "https://management.azure.com/x", {})
