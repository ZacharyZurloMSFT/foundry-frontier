import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_oidc_tests", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load("configure-github-oidc")
release = load("apim-release")

APIM_KEYS = ["apim-plan", "apim-apply"]
APIM_ENVIRONMENTS = ["northwind-apim-plan", "northwind-apim-apply"]
EXISTING_IDENTITIES = ["apps", "infra-plan", "infra-apply"]
EXISTING_ROLES = ["apps", "infra-plan", "infra-apply", "storage-approval"]
READS = [
    "Microsoft.ApiManagement/service/read", "Microsoft.ApiManagement/service/*/read",
    "Microsoft.App/containerApps/read", "Microsoft.Insights/components/read",
    "Microsoft.Resources/subscriptions/resourceGroups/read",
]
WRITES = [
    f"Microsoft.ApiManagement/service/{child}/write" for child in (
        "apis", "apis/operations", "apis/operations/policies", "apis/policies", "apis/diagnostics",
        "products", "products/apis", "subscriptions", "loggers",
    )
]
ALLOWED_WILDCARDS = {"Microsoft.ApiManagement/service/*/read", "Microsoft.Resources/deployments/*"}


class FakeGitHub:
    def __init__(self, *, actions_enabled=True, policies=None):
        self.calls = []
        self.actions_enabled = actions_enabled
        self.policies = policies or {}

    def __call__(self, path, method="GET", body=None):
        self.calls.append((method, path))
        if path.endswith("/actions/permissions"):
            return {"enabled": self.actions_enabled}
        if path.endswith("/deployment-branch-policies"):
            environment = path.split("/environments/")[1].split("/")[0]
            if method == "GET":
                return {"branch_policies": self.policies.get(environment, [])}
            self.policies.setdefault(environment, []).append(body)
        return {}


def test_apim_identities_have_their_own_environments_and_client_id_variables():
    assert setup.IDENTITIES["apim-plan"] == (
        "id-gh-northwind-apim-plan", "northwind-apim-plan", "AZURE_APIM_PLAN_CLIENT_ID")
    assert setup.IDENTITIES["apim-apply"] == (
        "id-gh-northwind-apim-apply", "northwind-apim-apply", "AZURE_APIM_APPLY_CLIENT_ID")
    for position in range(3):
        values = [identity[position] for identity in setup.IDENTITIES.values()]
        assert len(values) == len(set(values))
    assert list(setup.IDENTITIES)[:3] == EXISTING_IDENTITIES


def test_apim_plan_role_can_read_and_run_what_if_but_not_write_api_management():
    role = setup.ROLES["apim-plan"]
    assert role["name"] == "Northwind apim plan"
    assert role["actions"] == [
        *READS, "Microsoft.Resources/deployments/read", "Microsoft.Resources/deployments/write",
        "Microsoft.Resources/deployments/whatIf/action", "Microsoft.Resources/deployments/validate/action",
    ]
    assert not [action for action in role["actions"] if action.startswith("Microsoft.ApiManagement") and "write" in action]
    assert role["assignable"] == setup.APPS


def test_apim_configuration_role_writes_only_the_managed_child_types():
    role = setup.ROLES["apim-apply"]
    assert role["name"] == "Northwind apim configuration"
    assert role["actions"] == [*READS, "Microsoft.Resources/deployments/*", *WRITES]
    assert role["assignable"] == setup.APPS
    assert "Microsoft.ApiManagement/service/write" not in role["actions"]
    assert "Microsoft.ApiManagement/service/policies/write" not in role["actions"]
    assert "Microsoft.ApiManagement/service/apis/policies/write" in role["actions"]
    assert "Microsoft.ApiManagement/service/apis/operations/policies/write" in role["actions"]
    assert "Microsoft.ApiManagement/service/namedValues/write" not in role["actions"]


@pytest.mark.parametrize("key", APIM_KEYS)
def test_apim_roles_have_no_secrets_deletes_global_policy_or_wildcard_writes(key):
    role = setup.ROLES[key]
    assert not role.get("dataActions") and not role.get("notActions")
    for action in role["actions"]:
        lowered = action.lower()
        assert "listsecrets" not in lowered and "listvalue" not in lowered and "listkeys" not in lowered
        assert "delete" not in lowered and "roleassignments" not in lowered and "roledefinitions" not in lowered
        assert not lowered.endswith("*/write") and not lowered.endswith("/write/*") and action != "*"
        if "*" in action:
            assert action in ALLOWED_WILDCARDS
        assert action not in {
            "Microsoft.ApiManagement/*", "Microsoft.ApiManagement/service/*",
            "Microsoft.ApiManagement/service/write", "Microsoft.ApiManagement/service/policies/write",
            "Microsoft.ApiManagement/service/backends/write", "Microsoft.ApiManagement/service/certificates/write",
            "Microsoft.ApiManagement/service/namedValues/write",
        }


def test_only_the_configuration_role_may_use_the_deployment_wildcard():
    assert "Microsoft.Resources/deployments/*" not in setup.ROLES["apim-plan"]["actions"]
    assert "Microsoft.Resources/deployments/*" in setup.ROLES["apim-apply"]["actions"]


APIM_PREFIX = "microsoft.apimanagement/"


def role_drift(declared, actions):
    """(writes the role lacks for the declared types, APIM permissions it grants beyond them)."""
    granted = {action.lower() for action in actions}
    needed = {f"{kind.lower()}/write" for kind in declared}
    surplus = {action for action in granted if action.startswith(APIM_PREFIX) and not action.endswith("/read")}
    return needed - granted, surplus - needed


def declared_types(template):
    """Lower-cased types the deployment creates or changes; `existing` references need no permission."""
    resources = template["resources"]
    entries = resources.values() if isinstance(resources, dict) else resources
    return {entry["type"].lower() for entry in entries if entry.get("existing") is not True}


def test_role_drift_reports_a_missing_write_and_every_surplus_permission():
    declared = {"microsoft.apimanagement/service/apis", "microsoft.apimanagement/service/apis/operations/policies"}
    actions = [*READS, "Microsoft.Resources/deployments/*", "Microsoft.ApiManagement/service/apis/write"]
    assert role_drift(declared, actions) == ({"microsoft.apimanagement/service/apis/operations/policies/write"}, set())
    actions += [
        "Microsoft.ApiManagement/service/apis/operations/policies/write", "Microsoft.ApiManagement/service/namedValues/write",
        "Microsoft.ApiManagement/service/subscriptions/listSecrets/action", "Microsoft.ApiManagement/service/apis/delete",
    ]
    assert role_drift(declared, actions) == (set(), {
        "microsoft.apimanagement/service/namedvalues/write",
        "microsoft.apimanagement/service/subscriptions/listsecrets/action",
        "microsoft.apimanagement/service/apis/delete",
    })
    assert role_drift(set(), [*READS, "Microsoft.Resources/deployments/*"]) == (set(), set())


def test_declared_types_ignore_existing_references_and_collapse_copy_loops():
    symbolic = {"resources": {
        "apim": {"existing": True, "type": "Microsoft.ApiManagement/service"},
        "apis": {"type": "Microsoft.ApiManagement/service/apis", "copy": {"name": "apis"}},
        "more": {"type": "Microsoft.ApiManagement/service/APIS"},
    }}
    assert declared_types(symbolic) == {"microsoft.apimanagement/service/apis"}
    legacy = {"resources": [{"type": "Microsoft.ApiManagement/service/loggers"}] * 2}
    assert declared_types(legacy) == {"microsoft.apimanagement/service/loggers"}


def test_apply_role_and_release_allow_list_widen_together():
    # See the comments at APIM_WRITES and RESOURCE_TYPES: a new type needs the role, the allow-list and tests.
    assert role_drift(release.RESOURCE_TYPES, setup.ROLES["apim-apply"]["actions"]) == (set(), set())
    assert role_drift(set(), setup.ROLES["apim-plan"]["actions"]) == (set(), set())


def bicep_executable():
    if not release.TEMPLATE.exists():
        pytest.skip("infra/live/apim-apis.bicep is not present")
    bicep = shutil.which("bicep")
    if not bicep:
        path = Path.home() / ".azure" / "bin" / ("bicep.exe" if os.name == "nt" else "bicep")
        if not path.exists():
            pytest.skip("Existing Bicep CLI is required for compiled-template tests")
        bicep = str(path)
    return bicep


@pytest.fixture(scope="module")
def live_template():
    result = subprocess.run([bicep_executable(), "build", str(release.TEMPLATE), "--stdout"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_apply_role_can_write_every_type_the_compiled_template_declares(live_template):
    declared = declared_types(live_template)
    assert declared
    missing, _ = role_drift(declared, setup.ROLES["apim-apply"]["actions"])
    assert not missing, f"apim-apply cannot write: {sorted(missing)}"


def test_release_allow_list_covers_every_type_the_compiled_template_declares(live_template):
    declared = declared_types(live_template)
    assert declared <= release.RESOURCE_TYPES, sorted(declared - release.RESOURCE_TYPES)


def test_apply_role_is_exactly_as_wide_as_the_compiled_template(live_template):
    _, surplus = role_drift(declared_types(live_template), setup.ROLES["apim-apply"]["actions"])
    assert not surplus, f"apim-apply grants more than the template declares: {sorted(surplus)}"


def test_role_drift_against_the_compiled_template_has_teeth(live_template):
    declared = declared_types(live_template)
    actions = setup.ROLES["apim-apply"]["actions"]
    without_handshake = [action for action in actions if not action.endswith("operations/policies/write")]
    assert role_drift(declared, without_handshake) == (
        {"microsoft.apimanagement/service/apis/operations/policies/write"}, set())
    with_named_values = [*actions, "Microsoft.ApiManagement/service/namedValues/write"]
    assert role_drift(declared, with_named_values) == (set(), {"microsoft.apimanagement/service/namedvalues/write"})


def test_apim_bindings_are_resource_group_scoped_and_match_their_roles():
    selected = [binding for binding in setup.bindings() if binding[0] in APIM_KEYS]
    assert selected == [
        ("apim-plan", setup.APPS, setup.role_id("apim-plan")),
        ("apim-apply", setup.APPS, setup.role_id("apim-apply")),
    ]
    assert setup.APPS.endswith("/resourceGroups/rg-northwind-apps")
    for key, scope, role in selected:
        assert scope != setup.BASE and scope in setup.ROLES[key]["assignable"]
        assert role.endswith(setup.stable_id("role:" + key))
    assert setup.role_id("apim-plan") != setup.role_id("apim-apply")
    assert not any(binding[0] in APIM_KEYS and "/providers/" in binding[1] for binding in setup.bindings())


def test_default_selection_keeps_every_existing_identity_role_and_binding_and_adds_apim():
    assert setup.identity_keys() == [*EXISTING_IDENTITIES, *APIM_KEYS]
    assert setup.role_keys() == [*EXISTING_ROLES, *APIM_KEYS]
    assert setup.bindings() == setup.bindings(None)
    assert [key for key, _, _ in setup.bindings() if key in EXISTING_IDENTITIES].count("apps") == 6
    preview = setup.preview()
    assert preview["only"] is None
    assert [identity["environment"] for identity in preview["identities"]][-2:] == APIM_ENVIRONMENTS
    assert set(preview["role_definitions"]) == set(setup.ROLES)
    assert "AZURE_APIM_PLAN_CLIENT_ID" in preview["variables"] and "NORTHWIND_APIM_ENABLED" in preview["variables"]
    assert "AZURE_APPS_CLIENT_ID" in preview["variables"] and "NORTHWIND_INFRA_ENABLED" in preview["variables"]


def test_only_apim_selects_just_the_two_apim_identities_roles_bindings_and_variables():
    assert setup.identity_keys("apim") == APIM_KEYS
    assert setup.role_keys("apim") == APIM_KEYS
    assert [binding[0] for binding in setup.bindings("apim")] == APIM_KEYS
    assert setup.static_variables("apim") == {"NORTHWIND_APIM_ENABLED": "true"}
    preview = setup.preview("apim")
    assert preview["only"] == "apim"
    assert [identity["name"] for identity in preview["identities"]] == [
        "id-gh-northwind-apim-plan", "id-gh-northwind-apim-apply"]
    assert [identity["environment"] for identity in preview["identities"]] == APIM_ENVIRONMENTS
    assert list(preview["role_definitions"]) == APIM_KEYS
    assert [binding["identity"] for binding in preview["role_bindings"]] == APIM_KEYS
    assert preview["variables"] == ["AZURE_APIM_PLAN_CLIENT_ID", "AZURE_APIM_APPLY_CLIENT_ID", "NORTHWIND_APIM_ENABLED"]
    assert not any(binding["scope"] != setup.APPS for binding in preview["role_bindings"])
    for identity in preview["identities"]:
        assert identity["federated_subject"] == f"{setup.SUBJECT_PREFIX}:environment:{identity['environment']}"
    for existing in (*EXISTING_IDENTITIES, "storage-approval"):
        assert existing not in preview["role_definitions"]
        assert all(binding["identity"] != existing for binding in preview["role_bindings"])
    assert preview["environment_branch_allowlist"] == ["main"]
    assert preview["actions_enabled_by_this_script"] is False


def test_unknown_selection_is_refused():
    for function in (setup.identity_keys, setup.role_keys, setup.static_variables):
        with pytest.raises(KeyError):
            function("everything")


def test_full_setup_enables_the_apim_workflow_next_to_the_existing_gates():
    variables = setup.static_variables()
    assert variables["NORTHWIND_APIM_ENABLED"] == "true"
    assert variables["NORTHWIND_INFRA_ENABLED"] == variables["NORTHWIND_DEPLOY_ENABLED"] == "true"
    assert {"AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "AZURE_APPS_RESOURCE_GROUP", "ACR_NAME"} <= set(variables)


def test_apim_preview_has_no_cloud_calls(monkeypatch):
    monkeypatch.setattr(setup, "gh", lambda *_: pytest.fail("Preview cannot access GitHub"))
    monkeypatch.setattr(setup, "AzureCliCredential", lambda **_: pytest.fail("Preview cannot authenticate"))
    assert setup.preview("apim")["repository"] == setup.REPOSITORY
    assert "api_management" in setup.preview()


def test_full_setup_still_requires_actions_to_stay_disabled(monkeypatch):
    github = FakeGitHub(actions_enabled=True)
    monkeypatch.setattr(setup, "gh", github)
    with pytest.raises(ValueError, match="Keep repository Actions disabled"):
        setup.check_preconditions()
    assert github.calls == [("GET", f"repos/{setup.REPOSITORY}/actions/permissions")]
    github = FakeGitHub(actions_enabled=False)
    monkeypatch.setattr(setup, "gh", github)
    setup.check_preconditions(None)
    assert len(github.calls) == 1


def test_only_apim_replaces_the_actions_check_with_main_only_environments(monkeypatch):
    github = FakeGitHub(actions_enabled=True)
    monkeypatch.setattr(setup, "gh", github)
    setup.check_preconditions("apim")
    base = f"repos/{setup.REPOSITORY}/environments"
    assert github.calls == [
        call for environment in APIM_ENVIRONMENTS for call in (
            ("PUT", f"{base}/{environment}"),
            ("GET", f"{base}/{environment}/deployment-branch-policies"),
            ("POST", f"{base}/{environment}/deployment-branch-policies"),
        )
    ]
    assert github.policies == {environment: [{"name": "main", "type": "branch"}] for environment in APIM_ENVIRONMENTS}
    assert not any(path.endswith("/actions/permissions") for _, path in github.calls)


def test_only_apim_accepts_environments_that_are_already_restricted_to_main(monkeypatch):
    main = [{"name": "main", "type": "branch"}]
    github = FakeGitHub(policies={environment: list(main) for environment in APIM_ENVIRONMENTS})
    monkeypatch.setattr(setup, "gh", github)
    setup.check_preconditions("apim")
    assert not any(method == "POST" for method, _ in github.calls)


@pytest.mark.parametrize("policy", [
    {"name": "*", "type": "branch"}, {"name": "feature/x", "type": "branch"}, {"name": "main", "type": "tag"},
])
def test_only_apim_refuses_environments_that_allow_other_refs(monkeypatch, policy):
    github = FakeGitHub(policies={"northwind-apim-apply": [policy]})
    monkeypatch.setattr(setup, "gh", github)
    with pytest.raises(ValueError, match="unexpected allowed branches"):
        setup.check_preconditions("apim")
    posts = [path for method, path in github.calls if method == "POST"]
    assert posts == [f"repos/{setup.REPOSITORY}/environments/northwind-apim-plan/deployment-branch-policies"]
    assert github.policies["northwind-apim-apply"] == [policy]


def test_only_apim_never_touches_existing_environments(monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(setup, "gh", github)
    setup.check_preconditions("apim")
    touched = {path.split("/environments/")[1].split("/")[0] for _, path in github.calls}
    assert touched == set(APIM_ENVIRONMENTS)


def test_a_public_repository_is_refused_unless_the_operator_opts_in():
    private = {"private": True, "default_branch": "main"}
    public = {"private": False, "default_branch": "main"}
    setup.validate_repository(private)
    setup.validate_repository(private, allow_public=True)
    setup.validate_repository(public, allow_public=True)
    with pytest.raises(ValueError, match="public.*--allow-public-repository"):
        setup.validate_repository(public)
    with pytest.raises(ValueError, match="public"):
        setup.validate_repository({"default_branch": "main"})


@pytest.mark.parametrize("repo", [
    {"private": True, "default_branch": "develop"}, {"private": False, "default_branch": "develop"},
    {"private": False}, {},
])
def test_opting_in_to_a_public_repository_never_relaxes_the_main_branch_check(repo):
    for allow_public in (False, True):
        with pytest.raises(ValueError, match="main-branch"):
            setup.validate_repository(repo, allow_public=allow_public)


def test_configure_checks_visibility_before_identity_and_before_any_azure_call(monkeypatch):
    def github(path, method="GET", body=None):
        if path == f"repos/{setup.REPOSITORY}":
            return {"private": False, "default_branch": "main", "id": 1, "owner": {"id": 2}}
        raise AssertionError(f"unexpected GitHub call {path}")
    monkeypatch.setattr(setup, "gh", github)
    monkeypatch.setattr(setup, "AzureCliCredential", lambda **_: pytest.fail("No Azure call before the trust checks"))
    with pytest.raises(ValueError, match="public"):
        setup.configure("apim")
    # The opt-in passes the visibility check but the immutable repository identity is still enforced.
    monkeypatch.setattr(setup, "gh", lambda path, *a, **k: (
        {"private": False, "default_branch": "main", "id": 1, "owner": {"id": 2}}
        if path == f"repos/{setup.REPOSITORY}" else {}))
    with pytest.raises(ValueError, match="Repository identity"):
        setup.configure("apim", allow_public_repository=True)


def run_preview(*arguments):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "configure-github-oidc.py"), *arguments],
                          capture_output=True, text=True, check=False)


def test_command_line_previews_the_apim_selection_without_applying():
    default = run_preview()
    assert default.returncode == 0, default.stderr
    assert len(json.loads(default.stdout)["identities"]) == 5
    selected = run_preview("--only", "apim")
    assert selected.returncode == 0, selected.stderr
    preview = json.loads(selected.stdout)
    assert preview["only"] == "apim" and len(preview["identities"]) == 2 and len(preview["role_bindings"]) == 2
    refused = run_preview("--only", "everything")
    assert refused.returncode == 2 and "invalid choice" in refused.stderr
    opted_in = run_preview("--only", "apim", "--allow-public-repository")
    assert opted_in.returncode == 0, opted_in.stderr
    assert json.loads(opted_in.stdout) == preview, "the opt-in changes only what --apply may do, never the preview"
