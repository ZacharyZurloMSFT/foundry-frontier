import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("configure_insurance_cu", ROOT / "scripts" / "configure-insurance-cu.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
ENDPOINT = f"https://{module.ACCOUNT}.services.ai.azure.com"
SECRET = "test-sensitive-bearer-token"
EMBEDDING_ROLE = "prebuilt-analyzer-embedding"
SMALL = "text-embedding-3-small"


def apply_config(*args, **kwargs):
    kwargs.setdefault("embedding_deployment", SMALL)
    return module.apply(*args, **kwargs)


def test_analyzer_identifier_avoids_service_rejected_hyphens():
    assert module.ANALYZER_ID == "northwindInsuranceClaimsV1"
    assert module.ANALYZER_ID.isascii() and module.ANALYZER_ID.isalnum()
    assert module.definition()["tags"]["owner"] == "northwind-insurance-cu-v1"
    assert module.definition()["tags"]["schema"] == "insurance-claims-v1"


def test_preview_is_offline_and_contains_only_the_insurance_analyzer(monkeypatch, capsys):
    monkeypatch.setattr(module, "az_json", lambda *args: pytest.fail("Offline preview must not call Azure."))
    monkeypatch.setattr(module, "AzureDeveloperCliCredential", lambda **kwargs: pytest.fail("Offline preview must not authenticate."))
    assert module.main([]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["mode"] == "offline_preview"
    assert body["target"] == module.ACCOUNT_ID
    assert body["credential_source"] == "azd"
    assert body["configure_defaults"] is False
    assert any("--apply --configure-defaults" in write for write in body["cloud_writes"])
    assert "Foundry defaultProject changes" in body["not_performed"]
    assert body["definition"]["models"] == {"completion": "gpt-4.1"}
    assert body["definition"]["baseAnalyzerId"] == "prebuilt-document"
    assert "fine" not in body["definition"]
    assert "knowledgeSources" not in body["definition"]
    for name, kind in (("claim_id", "string"), ("policy_id", "string"), ("loss_date", "date"), ("estimate_total_usd", "number"), ("invoice_total_usd", "number")):
        field = body["definition"]["fieldSchema"]["fields"][name]
        assert field["type"] == kind and field["method"] == "extract"
    assert body["definition"]["config"]["estimateFieldSourceAndConfidence"] is True


def test_apply_requires_existing_deployment_without_auth(monkeypatch, capsys):
    monkeypatch.setattr(module, "az_json", lambda *args: pytest.fail("Must fail before auth."))
    assert module.main(["--apply"]) == 1
    assert "existing supported deployment" in capsys.readouterr().out


def test_azd_token_is_tenant_pinned_and_never_uses_cli(monkeypatch):
    calls = []
    class Credential:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            calls.append("closed")
        def get_token(self, scope):
            calls.append(scope)
            return SimpleNamespace(token=SECRET)
    monkeypatch.setattr(module, "AzureDeveloperCliCredential", Credential)
    monkeypatch.setattr(module, "az_json", lambda *args: pytest.fail("azd token must not use Azure CLI."))
    assert module.data_plane_token() == SECRET
    assert calls == [{"tenant_id": module.TENANT, "process_timeout": 30}, module.SCOPE, "closed"]


def test_explicit_cli_token_never_constructs_azd(monkeypatch):
    calls = []
    def az(*args):
        calls.append(args)
        return {"accessToken": SECRET}
    monkeypatch.setattr(module, "az_json", az)
    monkeypatch.setattr(module, "AzureDeveloperCliCredential", lambda **kwargs: pytest.fail("CLI token must not use azd."))
    assert module.data_plane_token("cli") == SECRET
    assert calls == [("account", "get-access-token", "--resource", "https://cognitiveservices.azure.com/")]


def test_azd_auth_failure_has_no_fallback_or_secret_output(monkeypatch, capsys):
    class Credential:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_token(self, scope):
            raise module.ClientAuthenticationError(message=SECRET)
    monkeypatch.setattr(module, "AzureDeveloperCliCredential", Credential)
    monkeypatch.setattr(module, "az_json", lambda *args: pytest.fail("No credential fallback allowed."))
    with pytest.raises(ValueError, match="no CLI fallback") as error:
        module.data_plane_token("azd")
    assert SECRET not in str(error.value)
    assert capsys.readouterr().out == ""


def test_empty_cli_token_does_not_fall_back_to_azd(monkeypatch):
    monkeypatch.setattr(module, "az_json", lambda *args: {"accessToken": ""})
    monkeypatch.setattr(module, "AzureDeveloperCliCredential", lambda **kwargs: pytest.fail("No credential fallback allowed."))
    with pytest.raises(ValueError, match="no fallback"):
        module.data_plane_token("cli")


@pytest.mark.parametrize("source", ["cli", "azd"])
def test_command_passes_exact_selected_credential_source(monkeypatch, capsys, source):
    calls = []
    def apply(model, deployment, **kwargs):
        calls.append((model, deployment, kwargs))
        return {"status": "mocked", "credential_source": kwargs["credential_source"]}
    monkeypatch.setattr(module, "apply", apply)
    assert module.main(["--apply", "--completion-model", "gpt-4o", "--completion-deployment", "gpt-4o", "--credential-source", source]) == 0
    assert calls == [("gpt-4o", "gpt-4o", {"reconcile": False, "credential_source": source, "configure_defaults": False, "embedding_deployment": ""})]
    assert SECRET not in capsys.readouterr().out


def test_defaults_flag_preview_is_offline_and_apply_passes_opt_in(monkeypatch, capsys):
    monkeypatch.setattr(module, "az_json", lambda *args: pytest.fail("Preview must remain offline."))
    assert module.main(["--configure-defaults", "--completion-model", "gpt-4o"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["configure_defaults"] is True
    calls = []
    def apply(model, deployment, **kwargs):
        calls.append(kwargs)
        return {"status": "mocked"}
    monkeypatch.setattr(module, "apply", apply)
    assert module.main(["--apply", "--configure-defaults", "--completion-model", "gpt-4o", "--completion-deployment", "gpt-4o", "--embedding-deployment", SMALL]) == 0
    assert calls[0]["configure_defaults"] is True
    assert calls[0]["embedding_deployment"] == SMALL


@pytest.mark.parametrize("account", [
    {"id": module.ACCOUNT_ID.replace(module.ACCOUNT, "foreign"), "kind": "AIServices"},
    {"id": module.ACCOUNT_ID, "kind": "OpenAI"},
    {"id": module.ACCOUNT_ID, "kind": "AIServices", "properties": {"endpoint": "https://evil.example"}},
    {"id": module.ACCOUNT_ID, "kind": "AIServices", "properties": {"endpoint": ENDPOINT + "/api/projects/foreign"}},
])
def test_account_scope_is_fixed(account):
    with pytest.raises(ValueError):
        module.endpoint_from_account(account)


def test_deployment_model_and_provisioning_are_verified():
    deployments = [{"name": "existing", "properties": {"model": {"name": "gpt-4.1"}, "provisioningState": "Succeeded"}}]
    module.existing_deployment(deployments, "existing", "gpt-4.1")
    for name, model in (("missing", "gpt-4.1"), ("existing", "gpt-4o"), ("../existing", "gpt-4.1")):
        with pytest.raises(ValueError):
            module.existing_deployment(deployments, name, model)


def test_owned_compatible_schema_requires_explicit_reconcile():
    desired = module.definition()
    current = copy.deepcopy(desired)
    current["status"] = "ready"
    current["config"]["enableFormula"] = False
    assert module.check_existing(current, desired, False) is False
    current["description"] = "Owned older description"
    with pytest.raises(ValueError, match="reconcile"):
        module.check_existing(current, desired, False)
    assert module.check_existing(current, desired, True) is True
    current["tags"]["owner"] = "foreign"
    with pytest.raises(ValueError, match="ownership"):
        module.check_existing(current, desired, True)


@pytest.mark.parametrize("mutate", [
    lambda body: body["fieldSchema"]["fields"].update({"foreign": {"type": "string"}}),
    lambda body: body["fieldSchema"]["fields"]["loss_date"].update({"type": "string"}),
])
def test_even_reconcile_refuses_foreign_or_incompatible_schema(mutate):
    desired = module.definition()
    current = copy.deepcopy(desired)
    mutate(current)
    with pytest.raises(ValueError, match="schema"):
        module.check_existing(current, desired, True)


class Cloud:
    def __init__(self):
        self.requests = []
        self.current = None
        self.statuses = iter(("Pending", "Running", "Succeeded"))
        self.operation_host = ENDPOINT
        self.supported = ["gpt-4.1", "gpt-4o"]
        self.base_models = {"embedding": EMBEDDING_ROLE}
        self.supported_embedding = [SMALL, "text-embedding-3-large", "text-embedding-ada-002"]
        self.fail_put = False
        self.defaults = {"gpt-4.1": "existing", EMBEDDING_ROLE: SMALL}
        self.fail_patch = False
        self.ignore_patch = False
        self.drop_other_mapping = False

    def handle(self, request):
        self.requests.append(request)
        assert request.url.host == f"{module.ACCOUNT}.services.ai.azure.com"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        if request.url.path.endswith("prebuilt-document"):
            return httpx.Response(200, json={"models": self.base_models, "supportedModels": {"completion": self.supported, "embedding": self.supported_embedding}})
        if request.url.path.endswith("/defaults"):
            if request.method == "GET":
                return httpx.Response(200, json={"modelDeployments": self.defaults})
            assert request.method == "PATCH"
            assert request.headers["Content-Type"] == "application/merge-patch+json"
            if self.fail_patch:
                return httpx.Response(400, text=SECRET)
            if not self.ignore_patch:
                patch = json.loads(request.content)["modelDeployments"]
                self.defaults = patch if self.drop_other_mapping else {**self.defaults, **patch}
            return httpx.Response(200, json={"modelDeployments": self.defaults})
        if "/operations/" in request.url.path:
            return httpx.Response(200, json={"status": next(self.statuses, "Succeeded")})
        if request.method == "GET":
            return httpx.Response(200, json=self.current) if self.current else httpx.Response(404)
        assert request.method == "PUT"
        if self.fail_put:
            return httpx.Response(400, text=SECRET)
        self.current = json.loads(request.content)
        self.current["models"] = {**self.base_models, **self.current["models"]}
        self.current["status"] = "ready"
        self.current["createdAt"] = "2026-10-14T18:00:00Z"
        location = self.operation_host + f"/contentunderstanding/analyzers/{module.ANALYZER_ID}/operations/create-op?api-version={module.API_VERSION}"
        return httpx.Response(201, json=self.current, headers={"Operation-Location": location})


@pytest.fixture
def cloud(monkeypatch):
    cloud = Cloud()
    real_client = httpx.Client
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(cloud.handle), **kwargs))
    def az(*args):
        if args[:2] == ("account", "show"):
            return {"id": module.SUBSCRIPTION, "tenantId": module.TENANT}
        if args[:3] == ("cognitiveservices", "account", "show"):
            return {"id": module.ACCOUNT_ID, "kind": "AIServices", "properties": {"endpoint": ENDPOINT}}
        if args[:4] == ("cognitiveservices", "account", "deployment", "list"):
            return [
                {"name": "existing", "properties": {"model": {"name": "gpt-4.1"}, "provisioningState": "Succeeded"}},
                {"name": "gpt-4o", "properties": {"model": {"name": "gpt-4o"}, "provisioningState": "Succeeded"}},
                {"name": SMALL, "properties": {"model": {"name": SMALL}, "provisioningState": "Succeeded"}},
            ]
        pytest.fail("Unexpected Azure CLI call")
    class Credential:
        def __init__(self, **kwargs):
            assert kwargs == {"tenant_id": module.TENANT, "process_timeout": 30}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_token(self, scope):
            assert scope == module.SCOPE
            return SimpleNamespace(token=SECRET)
    monkeypatch.setattr(module, "az_json", az)
    monkeypatch.setattr(module, "AzureDeveloperCliCredential", Credential)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    return cloud


def test_apply_only_puts_the_owned_analyzer_and_polls_real_lro(cloud):
    result = apply_config("gpt-4.1", "existing")
    assert result["status"] == "analyzer_ready" and result["smoke_required"] is True
    assert result["credential_source"] == "azd"
    assert result["runtime_environment"]["NORTHWIND_CU_ENABLED"] == "false"
    writes = [r for r in cloud.requests if r.method != "GET"]
    assert len(writes) == 1 and writes[0].method == "PUT"
    assert writes[0].url.path.endswith("/analyzers/" + module.ANALYZER_ID)
    assert writes[0].url.params["allowReplace"] == "false"
    assert len([r for r in cloud.requests if "/operations/" in r.url.path]) == 3
    assert SECRET not in json.dumps(result)
    assert not any(":analyze" in r.url.path for r in cloud.requests)
    assert all(r.method == "GET" for r in cloud.requests if "/defaults" in r.url.path)
    assert result["cu_defaults_updated"] is False
    assert result["runtime_environment"]["NORTHWIND_CU_EMBEDDING_DEPLOYMENT"] == SMALL
    assert result["embedding"] == {"role": EMBEDDING_ROLE, "model": SMALL, "deployment": SMALL}


def test_missing_cu_defaults_require_opt_in_before_any_write(cloud):
    cloud.defaults = {}
    with pytest.raises(ValueError, match="--configure-defaults"):
        apply_config("gpt-4o", "gpt-4o")
    assert all(r.method == "GET" for r in cloud.requests)
    assert cloud.defaults == {} and cloud.current is None


def test_opt_in_adds_only_selected_existing_model_and_preserves_all_mappings(cloud):
    preserved = {"prebuilt-analyzer-completion": "unrelated-completion", "text-embedding-3-large": "existing-embedding"}
    cloud.defaults = copy.deepcopy(preserved)
    result = apply_config("gpt-4o", "gpt-4o", configure_defaults=True)
    assert result["cu_defaults_updated"] is True
    assert cloud.defaults == {**preserved, "gpt-4o": "gpt-4o", EMBEDDING_ROLE: SMALL}
    patch = next(r for r in cloud.requests if r.method == "PATCH")
    assert patch.url.path == "/contentunderstanding/defaults"
    assert json.loads(patch.content) == {"modelDeployments": {"gpt-4o": "gpt-4o", EMBEDDING_ROLE: SMALL}}
    assert patch.headers["Content-Type"] == "application/merge-patch+json"
    patch_index = cloud.requests.index(patch)
    assert cloud.requests[patch_index + 1].method == "GET"
    assert cloud.requests[patch_index + 1].url.path == "/contentunderstanding/defaults"
    assert cloud.current["models"]["completion"] == "gpt-4o"
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("configure", [False, True])
def test_conflicting_mapping_is_never_overwritten_even_with_opt_in(cloud, configure):
    cloud.defaults = {"gpt-4o": "different-existing-deployment"}
    with pytest.raises(ValueError, match="refusing to overwrite"):
        apply_config("gpt-4o", "gpt-4o", configure_defaults=configure)
    assert all(r.method == "GET" for r in cloud.requests)
    assert cloud.defaults["gpt-4o"] == "different-existing-deployment"


def test_matching_mapping_with_flag_is_idempotent_and_not_patched(cloud):
    apply_config("gpt-4.1", "existing", configure_defaults=True)
    assert not any(r.method == "PATCH" for r in cloud.requests)


@pytest.mark.parametrize("behavior", ["ignore_patch", "drop_other_mapping"])
def test_defaults_readback_must_verify_new_and_preserved_mappings_before_registration(cloud, behavior):
    cloud.defaults = {"text-embedding-3-large": "existing-embedding"}
    setattr(cloud, behavior, True)
    with pytest.raises(ValueError, match="not verified"):
        apply_config("gpt-4o", "gpt-4o", configure_defaults=True)
    assert any(r.method == "PATCH" for r in cloud.requests)
    assert not any(r.method == "PUT" for r in cloud.requests)


def test_foreign_analyzer_blocks_defaults_changes_before_any_write(cloud):
    cloud.current = {**module.definition("gpt-4o"), "status": "ready", "tags": {"owner": "foreign"}}
    cloud.defaults = {}
    with pytest.raises(ValueError, match="ownership"):
        apply_config("gpt-4o", "gpt-4o", configure_defaults=True)
    assert all(r.method == "GET" for r in cloud.requests)


def test_defaults_patch_error_suppresses_body_and_never_creates_analyzer(cloud):
    cloud.defaults = {}
    cloud.fail_patch = True
    with pytest.raises(ValueError) as error:
        apply_config("gpt-4o", "gpt-4o", configure_defaults=True)
    assert SECRET not in str(error.value) and "HTTP 400" in str(error.value)
    assert not any(r.method == "PUT" for r in cloud.requests)


def test_existing_matching_analyzer_is_not_replaced(cloud):
    cloud.current = {**module.definition(), "status": "ready"}
    assert apply_config("gpt-4.1", "existing")["status"] == "unchanged"
    assert all(r.method == "GET" for r in cloud.requests)


def test_live_model_support_is_required_before_write(cloud):
    cloud.supported = []
    with pytest.raises(ValueError, match="advertise"):
        apply_config("gpt-4.1", "existing")
    assert all(r.method == "GET" for r in cloud.requests)


def test_creation_bad_host_is_not_polled(cloud):
    cloud.operation_host = "https://evil.example"
    with pytest.raises(ValueError, match="unsafe operation"):
        apply_config("gpt-4.1", "existing")
    assert not any("/operations/" in r.url.path for r in cloud.requests)


def test_creation_failed_is_not_readiness(cloud):
    cloud.statuses = iter(("Failed",))
    with pytest.raises(ValueError, match="failed"):
        apply_config("gpt-4.1", "existing")


def test_creation_timeout_is_explicit_and_does_not_retry_put(cloud):
    with pytest.raises(ValueError, match="timed out"):
        apply_config("gpt-4.1", "existing", timeout=-1)
    assert len([r for r in cloud.requests if r.method == "PUT"]) == 1


def test_upstream_error_does_not_expose_body_or_credentials(cloud):
    cloud.fail_put = True
    with pytest.raises(ValueError) as error:
        apply_config("gpt-4.1", "existing")
    assert SECRET not in str(error.value) and "HTTP 400" in str(error.value)


def test_existing_completion_needs_only_inherited_embedding_alias_patch(cloud):
    cloud.defaults = {"gpt-4o": "gpt-4o"}
    result = apply_config("gpt-4o", "gpt-4o", configure_defaults=True)
    patch = next(r for r in cloud.requests if r.method == "PATCH")
    assert json.loads(patch.content) == {"modelDeployments": {EMBEDDING_ROLE: SMALL}}
    assert result["embedding"] == {"role": EMBEDDING_ROLE, "model": SMALL, "deployment": SMALL}
    assert cloud.current["models"] == {"completion": "gpt-4o", "embedding": EMBEDDING_ROLE}
    put = next(r for r in cloud.requests if r.method == "PUT")
    assert json.loads(put.content)["models"] == {"completion": "gpt-4o"}
    assert "text-embedding-3-large" not in patch.content.decode()


def test_inherited_embedding_requires_explicit_deployment_before_any_write(cloud):
    with pytest.raises(ValueError, match="--embedding-deployment"):
        module.apply("gpt-4o", "gpt-4o", configure_defaults=True)
    assert all(r.method == "GET" for r in cloud.requests)


def test_small_embedding_is_never_mislabelled_as_large_to_pass_validation(cloud):
    cloud.supported_embedding = ["text-embedding-3-large"]
    with pytest.raises(ValueError, match="actual embedding deployment model"):
        apply_config("gpt-4o", "gpt-4o", configure_defaults=True)
    assert all(r.method == "GET" for r in cloud.requests)


def test_completion_deployment_cannot_fill_embedding_role(cloud):
    with pytest.raises(ValueError, match="actual embedding deployment model"):
        module.apply("gpt-4o", "gpt-4o", embedding_deployment="gpt-4o", configure_defaults=True)
    assert all(r.method == "GET" for r in cloud.requests)


def test_embedding_alias_conflict_is_never_overwritten(cloud):
    cloud.defaults = {"gpt-4o": "gpt-4o", EMBEDDING_ROLE: "another-embedding-deployment"}
    with pytest.raises(ValueError, match="refusing to overwrite"):
        apply_config("gpt-4o", "gpt-4o", configure_defaults=True)
    assert all(r.method == "GET" for r in cloud.requests)
