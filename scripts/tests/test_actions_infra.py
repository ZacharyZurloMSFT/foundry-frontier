import copy
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load("infra-release")
setup = load("configure-github-oidc")


@pytest.fixture(scope="module")
def compiled():
    bicep = shutil.which("bicep")
    if not bicep:
        path = Path.home() / ".azure" / "bin" / ("bicep.exe" if __import__("os").name == "nt" else "bicep")
        if not path.exists():
            pytest.skip("Existing Bicep CLI is required for compiled-template tests")
        bicep = str(path)
    result = subprocess.run([bicep, "build", str(release.TEMPLATE), "--stdout"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_live_template_only_manages_private_storage_networking(compiled):
    release.validate_template(compiled)
    assert len(compiled["resources"]) == 4
    assert all(r["type"].startswith("Microsoft.Network/") for r in compiled["resources"])
    assert {r["type"] for r in compiled["resources"]} == set(release.RESOURCE_NAMES)
    assert not any(r["type"] in {"Microsoft.Storage/storageAccounts", "Microsoft.Network/virtualNetworks"} for r in compiled["resources"])
    assert len(release.allowed_ids()) == 4
    assert all("/resourcegroups/rg-northwind-networking/" in r for r in release.allowed_ids())


@pytest.mark.parametrize("mutation", ["extra_resource", "duplicate_resource", "wrong_group", "wrong_vnet", "wrong_endpoint"])
def test_template_changes_cannot_silently_expand_the_management_boundary(compiled, mutation):
    candidate = copy.deepcopy(compiled)
    if mutation == "extra_resource":
        candidate["resources"].append({"type": "Microsoft.App/containerApps", "name": "ca-api"})
    elif mutation == "duplicate_resource":
        candidate["resources"].append(copy.deepcopy(candidate["resources"][0]))
    else:
        key = {"wrong_group": "storageResourceGroup", "wrong_vnet": "vnetName", "wrong_endpoint": "privateEndpointName"}[mutation]
        candidate["parameters"][key]["defaultValue"] = "other-target"
    with pytest.raises(ValueError):
        release.validate_template(candidate)


def test_plan_allows_only_named_create_modify_and_rejects_deletes_unknowns():
    identifier = sorted(release.allowed_ids())[0]
    for change in ("Create", "Modify"):
        result = release.safe_plan({"status": "Succeeded", "changes": [{"changeType": change, "resourceId": identifier}]})
        assert result["changes"][0]["change_type"] == change
    assert release.safe_plan({"changes": [{"changeType": "Ignore", "resourceId": "out-of-scope"}]}) == {"changes": []}
    for result in (
        {"status": "Failed", "changes": []}, {"changes": [{"changeType": "Delete", "resourceId": identifier}]},
        {"changes": [{"changeType": "Unsupported", "resourceId": identifier}]},
        {"changes": [{"changeType": "Create", "resourceId": "other-resource"}]}, {},
    ):
        with pytest.raises(ValueError):
            release.safe_plan(result)


def test_envelope_binds_source_template_and_full_plan(compiled, tmp_path):
    path = tmp_path / "template.json"
    path.write_text(json.dumps(compiled), encoding="utf-8")
    result = {"status": "Succeeded", "changes": []}
    sha = "a" * 40
    first = release.envelope(path, result, sha)
    assert first == release.envelope(path, result, sha)
    assert first != release.envelope(path, result, "b" * 40)
    changed = {**result, "changes": [{"changeType": "Create", "resourceId": sorted(release.allowed_ids())[0]}]}
    assert first["plan_sha256"] != release.envelope(path, changed, sha)["plan_sha256"]


def test_apply_requires_reviewed_plan_same_source_and_no_drift(compiled, tmp_path, monkeypatch):
    template = tmp_path / "template.json"
    template.write_text(json.dumps(compiled), encoding="utf-8")
    result = {"status": "Succeeded", "changes": []}
    monkeypatch.setattr(release, "make_plan", lambda _: result)
    calls = []
    monkeypatch.setattr(release, "command", lambda *args: calls.append(args) or {
        "provisioningState": "Succeeded", "privateLinkServiceConnections": [
            {"privateLinkServiceConnectionState": {"status": "Approved"}},
        ],
    })
    sha = "a" * 40
    approved = tmp_path / "approved.json"
    approved.write_text(json.dumps(release.envelope(template, result, sha)), encoding="utf-8")
    args = [
        "infra-release", "--compiled-template", str(template), "--output", str(tmp_path / "out.json"),
        "--source-sha", sha, "--approved-plan", str(approved), "--apply",
    ]
    monkeypatch.setattr("sys.argv", args)
    with pytest.raises(ValueError, match="explicit confirmation"):
        release.main()
    assert not calls
    monkeypatch.setenv("NORTHWIND_INFRA_CONFIRM", sha)
    altered = json.loads(approved.read_text())
    altered["source_sha"] = "b" * 40
    approved.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after review"):
        release.main()
    assert not calls
    approved.write_text(json.dumps(release.envelope(template, result, sha)), encoding="utf-8")
    release.main()
    assert len(calls) == 2
    assert "--mode" in calls[0] and calls[0][calls[0].index("--mode") + 1] == "Incremental"
    assert json.loads((tmp_path / "out.json").read_text())["applied"] is True


def test_infra_workflow_does_not_apply_on_push_or_pr():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy-infra.yml").read_text())
    jobs = workflow["jobs"]
    assert "workflow_dispatch" in jobs["apply"]["if"]
    assert "plan_run_id" in json.dumps(workflow)
    assert "getWorkflowRun" in json.dumps(jobs["source"])
    assert "getBranch" in json.dumps(jobs["apply"])
    assert jobs["apply"]["concurrency"] == {"group": "northwind-live-mutations", "cancel-in-progress": False}
    assert jobs["plan"]["environment"] != jobs["apply"]["environment"]
    assert "id-token" not in jobs["source"].get("permissions", {})
    assert not any("azd " in str(step.get("run", "")) for job in jobs.values() for step in job.get("steps", []))


def test_retired_workflow_cannot_provision_or_delete():
    text = (ROOT / ".github" / "workflows" / "deploy-progressive.yml").read_text()
    assert "azd up" not in text and "azd down" not in text and "azure/login" not in text
    assert "exit 1" in text


def test_oidc_plan_has_no_credentials_and_no_subscription_wide_assignments():
    preview = setup.preview()
    assert preview["credentials"] == "OIDC only; no client secret"
    assert not preview["actions_enabled_by_this_script"]
    assert preview["environment_branch_allowlist"] == ["main"]
    assert len(preview["identities"]) == 3
    for identity in preview["identities"]:
        assert identity["federated_subject"] == f"{setup.SUBJECT_PREFIX}:environment:{identity['environment']}"
    assert all(binding["scope"] != setup.BASE for binding in preview["role_bindings"])
    assert not any("roleAssignments/write" in action for role in setup.ROLES.values() for action in role["actions"])
    assert all(not role.get("dataActions") for role in setup.ROLES.values())
    app_scopes = [scope for key, scope, role in setup.bindings() if key == "apps" and role == setup.role_id("apps")]
    assert len(app_scopes) == 5 and all("/Microsoft.App/containerApps/ca-" in scope for scope in app_scopes)


def test_oidc_preview_has_no_cloud_calls(monkeypatch):
    monkeypatch.setattr(setup, "gh", lambda *_: pytest.fail("Preview cannot access GitHub"))
    monkeypatch.setattr(setup, "AzureCliCredential", lambda **_: pytest.fail("Preview cannot authenticate"))
    assert setup.preview()["repository"] == setup.REPOSITORY


def test_oidc_policy_requires_the_observed_immutable_repository_subject():
    repo = {"id": setup.REPOSITORY_ID, "owner": {"id": setup.OWNER_ID}}
    policy = {"use_default": True, "use_immutable_subject": True, "sub_claim_prefix": setup.SUBJECT_PREFIX}
    setup.validate_oidc_policy(repo, policy)
    for changes in (
        {"use_default": False}, {"use_immutable_subject": False},
        {"sub_claim_prefix": f"repo:{setup.REPOSITORY}"},
        {"sub_claim_prefix": setup.SUBJECT_PREFIX + "-other"},
    ):
        with pytest.raises(ValueError, match="subject policy changed"):
            setup.validate_oidc_policy(repo, {**policy, **changes})
    with pytest.raises(ValueError, match="Repository identity"):
        setup.validate_oidc_policy({**repo, "id": 123}, policy)
