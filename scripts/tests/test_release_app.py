import argparse
import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_app", ROOT / "scripts" / "release-app.py")
release_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_app)
SOURCE_SHA = "a" * 40
DIGEST = "b" * 64


def arguments(app="ca-api", **overrides):
    values = {
        "subscription": release_app.SUBSCRIPTION, "tenant": release_app.TENANT,
        "resource_group": release_app.RESOURCE_GROUP, "app": app,
        "image": f"{release_app.REGISTRY}/{app.removeprefix('ca-')}@sha256:{DIGEST}",
        "expected_revision": f"{app}--previous", "revision": f"gha-123456-1-{SOURCE_SHA[:12]}",
        "rollback_file": ROOT / ".release-test-state" / "rollback.json",
        "git_sha": SOURCE_SHA if app == "ca-api" else None,
        "deployed_at": "2026-10-15T21:00:00Z" if app == "ca-api" else None,
        "github_repository": release_app.REPOSITORY if app == "ca-api" else None,
        "timeout": 600, "apply": True,
    }
    return argparse.Namespace(**(values | overrides))


def live_app(args):
    return {
        "etag": '"before"',
        "identity": {"type": "UserAssigned", "userAssignedIdentities": {"existing-identity": {}}},
        "properties": {
            "latestReadyRevisionName": args.expected_revision,
            "latestRevisionName": args.expected_revision, "provisioningState": "Succeeded",
            "managedEnvironmentId": "existing-environment",
            "configuration": {
                "activeRevisionsMode": "Single",
                "secrets": [{"name": "secret", "value": "INLINE_SECRET_DO_NOT_PERSIST"}],
                "ingress": {"fqdn": f"{args.app}.existing.eastus.azurecontainerapps.io", "external": True},
                "registries": [{"server": release_app.REGISTRY, "identity": "existing-identity"}],
            },
            "template": {
                "revisionSuffix": "previous",
                "containers": [{
                    "name": args.app, "image": f"{release_app.REGISTRY}/{args.app.removeprefix('ca-')}:previous",
                    "resources": {"cpu": 1, "memory": "2Gi"},
                    "probes": [{"type": "Readiness", "httpGet": {"path": "/health", "port": 8000}}],
                    "env": [
                        {"name": "COPILOT_HARNESS_ENABLED", "value": "true"},
                        {"name": "NORTHWIND_INSURANCE_AGENTS_READY", "value": "false"},
                        {"name": "NORTHWIND_BUSINESS_API_ENABLED", "value": "false"},
                        {"name": "FOUNDRY_PROJECT_ENDPOINT", "value": "https://existing/projects/insurance"},
                        {"name": "FOUNDRY_PROJECT_ENDPOINTS", "value": '{"existing":"preserved"}'},
                        {"name": "TOKEN", "secretRef": "secret"},
                        {"name": "INLINE_TOKEN", "value": "INLINE_SECRET_DO_NOT_PERSIST"},
                        {"name": "GIT_SHA", "value": "c" * 40},
                        {"name": "BUILD_ENV", "value": "local"},
                    ],
                }],
                "scale": {"minReplicas": 1, "maxReplicas": 3},
                "volumes": [{"name": "scratch", "storageType": "EmptyDir"}],
            },
        },
    }


class Arm:
    def __init__(self, args):
        self.args = args
        self.before = live_app(args)
        self.patches = []
        self.requests = []
        self.polls = 0
        self.drift = None
        self.status = 200
        self.smoke_status = 200
        self.smoke_timeout = False
        self.patch_timeout = False
        self.ready = True
        self.superseded = False
        self.failed = False
        self.traffic = 100

    def handle(self, request):
        self.requests.append(request)
        if request.url.host != "management.azure.com":
            assert "Authorization" not in request.headers
            if self.smoke_timeout:
                raise httpx.ReadTimeout("secret upstream detail", request=request)
            return httpx.Response(self.smoke_status)
        assert request.headers["Authorization"] == "Bearer TEST_TOKEN"
        if request.method == "PATCH":
            self.patches.append(json.loads(request.content))
            if self.patch_timeout:
                raise httpx.ReadTimeout("secret upstream detail", request=request)
            return httpx.Response(self.status, json={"error": {"message": "INLINE_SECRET_DO_NOT_PERSIST"}})
        if "/revisions/" in request.url.path:
            return httpx.Response(200, json={"properties": {
                "active": True, "trafficWeight": self.traffic, "healthState": "Healthy",
                "runningState": "Running", "provisioningState": "Provisioned",
            }})
        current = copy.deepcopy(self.before)
        if not self.patches and self.drift and len(self.requests) > 1:
            self.drift(current)
        if self.patches:
            self.polls += 1
            current["properties"]["template"] = self.patches[0]["properties"]["template"]
            if self.ready:
                expected = f"{self.args.app}--{self.args.revision}"
                current["properties"].update(latestReadyRevisionName=expected, latestRevisionName=expected)
            if self.superseded:
                current["properties"]["latestRevisionName"] = f"{self.args.app}--newer"
            if self.failed:
                current["properties"]["provisioningState"] = "Failed"
        return httpx.Response(200, json=current)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))


@pytest.fixture
def record(monkeypatch):
    """Keep rollback writes entirely in memory; tests never authenticate or write temporary files."""
    captured = {}

    class MemoryRecord(io.StringIO):
        def close(self):
            captured["text"] = self.getvalue()
            super().close()

    original_open, original_fdopen, original_mkdir = os.open, os.fdopen, Path.mkdir
    path = arguments().rollback_file

    def open_record(target, flags, mode=0o777, **kwargs):
        if Path(target) != path:
            return original_open(target, flags, mode, **kwargs)
        assert flags & os.O_EXCL
        assert mode == 0o600
        if "opened" in captured:
            raise FileExistsError
        captured["opened"] = True
        return 123456

    monkeypatch.setattr(os, "open", open_record)
    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **kw: MemoryRecord() if fd == 123456
                        else original_fdopen(fd, *a, **kw))
    monkeypatch.setattr(Path, "mkdir", lambda target, *a, **kw: None if target == path.parent
                        else original_mkdir(target, *a, **kw))
    return captured


def execute(args, arm, **kwargs):
    with arm.client() as client:
        return release_app.release(args, client, lambda: "TEST_TOKEN", **kwargs)


def test_api_only_patches_template_and_build_stamp_preserving_all_other_fields(record):
    args = arguments()
    arm = Arm(args)
    before = copy.deepcopy(arm.before)
    result = execute(args, arm)
    assert result["state"] == "ready"
    assert len(arm.patches) == 1
    assert set(arm.patches[0]) == {"properties"}
    assert set(arm.patches[0]["properties"]) == {"template"}
    template = arm.patches[0]["properties"]["template"]
    expected = copy.deepcopy(before["properties"]["template"])
    expected["revisionSuffix"] = args.revision
    expected["containers"][0]["image"] = args.image
    env = expected["containers"][0]["env"]
    env[:] = [entry for entry in env if entry["name"] not in release_app.BUILD_NAMES]
    env.extend({"name": key, "value": value} for key, value in release_app.validate_release(args).items())
    assert template == expected
    assert arm.before == before
    patch = next(request for request in arm.requests if request.method == "PATCH")
    assert patch.headers["If-Match"] == '"before"'
    assert arm.requests[-1].url.path == "/health"


@pytest.mark.parametrize("app", list(release_app.APPS)[1:])
def test_other_apps_change_only_image_and_revision_never_environment(app, record):
    args = arguments(app)
    arm = Arm(args)
    execute(args, arm)
    expected = copy.deepcopy(arm.before["properties"]["template"])
    expected["revisionSuffix"] = args.revision
    expected["containers"][0]["image"] = args.image
    assert arm.patches[0]["properties"]["template"] == expected
    assert "previous_build_metadata" not in json.loads(record["text"])
    assert arm.requests[-1].url.path == ("/healthz" if "mcp-" in app else "/")


def test_rollback_is_secret_free_and_contains_only_previous_approved_values(record):
    args = arguments()
    arm = Arm(args)
    execute(args, arm)
    rollback = json.loads(record["text"])
    assert rollback == {
        "app": "ca-api", "previous_revision": args.expected_revision,
        "previous_image": f"{release_app.REGISTRY}/api:previous",
        "previous_build_metadata": {"GIT_SHA": "c" * 40, "BUILD_ENV": "local",
                                    "DEPLOYED_AT": None, "GITHUB_REPOSITORY": None},
    }
    for forbidden in ("INLINE_SECRET", "TOKEN", "FOUNDRY", "HARNESS", "template", "configuration", "identity"):
        assert forbidden not in record["text"]


def test_default_cli_is_dry_run_and_does_not_write_or_mutate(record):
    args = arguments(apply=False)
    arm = Arm(args)
    assert execute(args, arm)["state"] == "dry-run"
    assert not record and not arm.patches
    cli = release_app.parser().parse_args([
        "release", "--subscription", args.subscription, "--tenant", args.tenant,
        "--resource-group", args.resource_group, "--app", args.app, "--image", args.image,
        "--revision", args.revision, "--expected-revision", args.expected_revision,
        "--rollback-file", str(args.rollback_file),
    ])
    assert cli.apply is False


@pytest.mark.parametrize("field,value", [
    ("subscription", "another-subscription"), ("tenant", "another-tenant"),
    ("resource_group", "rg-other"), ("app", "ca-agent"),
    ("image", f"{release_app.REGISTRY}/api:latest"),
    ("image", f"{release_app.REGISTRY}/api@sha256:short"),
    ("image", f"evil.azurecr.io/api@sha256:{DIGEST}"),
    ("image", f"{release_app.REGISTRY}/web-tech@sha256:{DIGEST}"),
    ("image", f"{release_app.REGISTRY}/api@sha256:{DIGEST};echo injected"),
    ("revision", "gha-0-1-aaaaaaaaaaaa"), ("revision", "gha-123-0-aaaaaaaaaaaa"),
    ("revision", "gha-123-1-BBBBBBBBBBBB"), ("revision", "gha-123-1-bbbbbbbbbbbb"),
    ("expected_revision", "ca-web-tech--previous"), ("expected_revision", "ca-api--previous/escape"),
    ("git_sha", "main"), ("deployed_at", "2026-10-15T00:00:00"),
    ("github_repository", "untrusted/repo"),
])
def test_scope_digest_run_and_metadata_allowlists_reject_before_http(field, value, record):
    args = arguments(**{field: value})
    arm = Arm(args)
    with pytest.raises(ValueError):
        execute(args, arm)
    assert not arm.requests and not record


def test_non_api_cannot_receive_build_environment(record):
    args = arguments("ca-web-tech", git_sha=SOURCE_SHA)
    with pytest.raises(ValueError, match="Only the API"):
        execute(args, Arm(args))
    assert not record


@pytest.mark.parametrize("field,value", [
    ("latestReadyRevisionName", "ca-api--newer"), ("latestRevisionName", "ca-api--pending"),
    ("provisioningState", "InProgress"),
])
def test_stale_revision_or_pending_deployment_refuses_before_record(field, value, record):
    args = arguments()
    arm = Arm(args)
    arm.before["properties"][field] = value
    with pytest.raises(release_app.ReleaseError, match="revision changed"):
        execute(args, arm)
    assert not arm.patches and not record


@pytest.mark.parametrize("change", ["etag", "template", "configuration", "identity"])
def test_drift_after_snapshot_is_detected_without_mutation(change, record):
    args = arguments()
    arm = Arm(args)

    def drift(current):
        if change == "etag":
            current["etag"] = '"changed"'
        elif change == "identity":
            current["identity"]["type"] = "SystemAssigned"
        else:
            current["properties"][change]["drift"] = True

    arm.drift = drift
    with pytest.raises(release_app.ReleaseError, match="drifted"):
        execute(args, arm)
    assert not arm.patches
    assert record["text"]


def test_missing_etag_uses_last_moment_revision_and_config_guard(record):
    args = arguments()
    arm = Arm(args)
    del arm.before["etag"]
    execute(args, arm)
    patch = next(request for request in arm.requests if request.method == "PATCH")
    assert "If-Match" not in patch.headers


def test_arm_header_etag_is_honored_even_if_not_in_response_body(record):
    args = arguments()
    arm = Arm(args)
    del arm.before["etag"]

    def handle(request):
        response = arm.handle(request)
        if request.method == "GET" and request.url.host == "management.azure.com":
            response.headers["etag"] = '"header-version"'
        return response

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        release_app.release(args, client, lambda: "TEST_TOKEN")
    patch = next(request for request in arm.requests if request.method == "PATCH")
    assert patch.headers["If-Match"] == '"header-version"'


def test_arm_read_failure_or_timeout_is_sanitized_and_never_mutates(record):
    args = arguments()
    for failure in ("status", "timeout"):
        def handle(request, failure=failure):
            if failure == "timeout":
                raise httpx.ReadTimeout("INLINE_SECRET_DO_NOT_PERSIST", request=request)
            return httpx.Response(403, json={"error": "INLINE_SECRET_DO_NOT_PERSIST"})

        with (httpx.Client(transport=httpx.MockTransport(handle)) as client,
              pytest.raises(release_app.ReleaseError) as error):
            release_app.release(args, client, lambda: "TEST_TOKEN")
        assert "INLINE_SECRET" not in str(error.value)
    assert not record


def test_accepted_patch_with_empty_body_and_not_yet_existing_revision_is_polled(record):
    args = arguments(timeout=30)
    arm = Arm(args)
    revision_gets = 0

    def handle(request):
        nonlocal revision_gets
        response = arm.handle(request)
        if request.method == "PATCH":
            return httpx.Response(202)
        if "/revisions/" in request.url.path:
            revision_gets += 1
            if revision_gets == 1:
                return httpx.Response(404)
        return response

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = release_app.release(args, client, lambda: "TEST_TOKEN", sleep=lambda _: None)
    assert result["state"] == "ready"
    assert revision_gets == 2 and len(arm.patches) == 1


@pytest.mark.parametrize("change", ["multi-revision", "sidecar", "metadata-secret", "metadata-inline-secret"])
def test_unsupported_configuration_refuses_without_secret_record(change, record):
    args = arguments()
    arm = Arm(args)
    if change == "multi-revision":
        arm.before["properties"]["configuration"]["activeRevisionsMode"] = "Multiple"
    elif change == "sidecar":
        arm.before["properties"]["template"]["containers"].append({"name": "sidecar"})
    else:
        entry = arm.before["properties"]["template"]["containers"][0]["env"][-1]
        if change == "metadata-secret":
            entry.pop("value")
            entry["secretRef"] = "secret"
        else:
            entry["value"] = "INLINE_SECRET_DO_NOT_PERSIST"
    with pytest.raises(release_app.ReleaseError):
        execute(args, arm)
    assert not arm.patches and not record


@pytest.mark.parametrize("status", [400, 401, 403, 409, 412, 429, 500])
def test_failed_patch_is_not_retried_and_record_survives(status, record):
    args = arguments()
    arm = Arm(args)
    arm.status = status
    with pytest.raises(release_app.ReleaseError, match=f"HTTP {status}") as error:
        execute(args, arm)
    assert "INLINE_SECRET" not in str(error.value)
    assert len(arm.patches) == 1 and record["text"]


def test_patch_timeout_does_not_retry_unknown_mutation(record):
    args = arguments()
    arm = Arm(args)
    arm.patch_timeout = True
    with pytest.raises(release_app.ReleaseError, match="do not blindly retry"):
        execute(args, arm)
    assert len(arm.patches) == 1 and record["text"]


def test_smoke_redirect_is_a_failure_not_an_unverified_target(record):
    args = arguments()
    arm = Arm(args)
    arm.smoke_status = 302
    with pytest.raises(release_app.ReleaseError, match="HTTP 302"):
        execute(args, arm)
    assert len(arm.patches) == 1 and record["text"]


@pytest.mark.parametrize("failure", ["readiness", "traffic", "superseded", "provisioning", "smoke", "smoke-timeout"])
def test_readiness_traffic_smoke_and_newer_releases_fail_without_rollback(failure, record):
    args = arguments(timeout=10)
    arm = Arm(args)
    arm.ready = failure != "readiness"
    arm.traffic = 0 if failure == "traffic" else 100
    arm.superseded = failure == "superseded"
    arm.failed = failure == "provisioning"
    arm.smoke_status = 503 if failure == "smoke" else 200
    arm.smoke_timeout = failure == "smoke-timeout"
    ticks = iter(range(100))
    with pytest.raises(release_app.ReleaseError):
        execute(args, arm, sleep=lambda _: None, clock=lambda: next(ticks))
    assert len(arm.patches) == 1 and record["text"]
    assert all(request.method in ("GET", "PATCH") for request in arm.requests)


def test_existing_rollback_is_never_overwritten(record):
    args = arguments()
    execute(args, Arm(args))
    original = record["text"]
    arm = Arm(args)
    with pytest.raises(FileExistsError):
        execute(args, arm)
    assert record["text"] == original and not arm.patches


@pytest.mark.parametrize("paths,expected", [
    (["README.md", "infra/main.bicep"], []),
    (["apps/web-tech/package-lock.json"], ["web-tech"]),
    (["apps/web-voice/src/App.tsx"], ["web-voice"]),
    (["apps/api/app/main.py"], ["api"]),
    (["apps/api/app/insurance_domain.py"], ["api", "mcp-tech-lookup", "mcp-work-order"]),
    (["data/northwind/insurance-fixtures.json"], ["api", "mcp-tech-lookup", "mcp-work-order"]),
    (["data/seed/generated/nw-fnol-demo-2001.pdf"], ["api", "mcp-tech-lookup", "mcp-work-order"]),
    (["apps/mcp-work-order/app/main.py"], ["mcp-work-order"]),
    (["scripts/bundle-harness-assets.py"], list(release_app.SERVICES)),
    (["agents/copilot-harness/instructions.prompty"], list(release_app.SERVICES)),
    ([".github/workflows/deploy-apps.yml"], list(release_app.SERVICES)),
    (["apps/web-tech-evil/anything"], []),
])
def test_changed_service_paths_cover_actual_build_dependencies(paths, expected):
    assert release_app.changed_services(paths) == expected


def test_initial_commit_or_unknown_ancestor_conservatively_selects_every_app(monkeypatch):
    assert release_app.plan(SOURCE_SHA, None) == list(release_app.SERVICES)
    monkeypatch.setattr(release_app.subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=1))
    assert release_app.plan(SOURCE_SHA, "c" * 40) == list(release_app.SERVICES)


def test_plan_uses_nul_delimited_ancestor_diff_and_does_not_execute_paths(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["capture_output"]
        return SimpleNamespace(returncode=0, stdout=b"apps/web-tech/src/a\nb.ts\0apps/api/app/main.py\0")

    monkeypatch.setattr(release_app.subprocess, "run", run)
    assert release_app.plan(SOURCE_SHA, "c" * 40) == ["api", "web-tech"]
    assert calls[-1] == ["git", "diff", "--no-renames", "--name-only", "-z", "c" * 40, SOURCE_SHA, "--"]


def test_manual_service_allowlist_and_no_unchecked_sha():
    assert release_app.plan(SOURCE_SHA, None, "web-tech") == ["web-tech"]
    for sha, service in [("main", "all"), (SOURCE_SHA, "agents"), ("$(echo injected)", "api")]:
        with pytest.raises(ValueError):
            release_app.plan(sha, None, service)


def test_business_frontend_defaults_disabled_but_enabled_configuration_must_be_complete():
    release_app.validate_frontend_environment({})
    release_app.validate_frontend_environment({"VITE_INSURANCE_BUSINESS_ENABLED": "false"})
    valid = {
        "VITE_INSURANCE_BUSINESS_ENABLED": "true",
        "VITE_INSURANCE_AUTH_TENANT_ID": release_app.TENANT,
        "VITE_INSURANCE_AUTH_CLIENT_ID": release_app.TENANT,
        "VITE_INSURANCE_AUTH_SCOPE": "api://explicit-api/business",
        "VITE_INSURANCE_RUN_ID": "approved-run",
    }
    release_app.validate_frontend_environment(valid)
    for key in valid:
        with pytest.raises(ValueError):
            release_app.validate_frontend_environment(valid | {key: ""})
    with pytest.raises(ValueError):
        release_app.validate_frontend_environment(valid | {"VITE_INSURANCE_RUN_ID": "../../other"})


def workflow():
    return yaml.load((ROOT / ".github" / "workflows" / "deploy-apps.yml").read_text(), Loader=yaml.BaseLoader)


def test_workflow_has_environment_scoped_oidc_shared_lock_and_immutable_builds():
    data = workflow()
    assert set(data["on"]) == {"workflow_run", "workflow_dispatch"}
    assert data["on"]["workflow_run"]["workflows"] == ["CI"]
    assert data["concurrency"] == {"group": "northwind-live-mutations", "cancel-in-progress": "false"}
    assert data["permissions"] == {"contents": "read", "actions": "read"}
    deploy = data["jobs"]["deploy"]
    assert deploy["environment"] == "northwind-apps"
    assert deploy["permissions"]["id-token"] == "write"
    text = json.dumps(data)
    for forbidden in ("azd up", "azd down", "azd provision", "az acr build", "az acr credential", ":latest"):
        assert forbidden not in text
    for job in data["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", "")
    assert "containerimage.digest" in text
    assert "python data/seed/generate.py --domain insurance" in text
    assert "reportlab==4.2.5" in text
    assert "retention-days" in text
    assert "NORTHWIND_DEPLOY_ENABLED" in data["jobs"]["verify"]["if"]
    assert data["jobs"]["verify"]["steps"][0]["uses"] == "actions/github-script@v9"
    artifact = deploy["steps"][-1]
    assert artifact["if"] == "always()"
    assert artifact["with"]["path"] == ".release-state/rollback-*.json"
    assert artifact["with"]["include-hidden-files"] == "true"
    assert artifact["with"]["if-no-files-found"] == "error"
    assert artifact["with"]["retention-days"] == "14"


@pytest.mark.skipif(not shutil.which("node"), reason="Node unavailable for GitHub script execution")
@pytest.mark.parametrize("case,allowed", [
    ("automatic", True), ("dispatch", True), ("pull-request-source", False), ("fork", False),
    ("wrong-branch", False), ("failed-ci", False), ("old-main", False), ("unchecked-dispatch", False),
    ("dispatch-pr-ci", False), ("dispatch-non-main", False), ("dispatch-arbitrary-sha", False),
    ("wrong-ci-workflow", False), ("wrong-repository", False), ("wrong-workflow-sha", False),
])
def test_actual_workflow_trust_gate_against_untrusted_and_unverified_sources(case, allowed):
    script = workflow()["jobs"]["verify"]["steps"][0]["with"]["script"]
    # Execute the actual gate with inert GitHub API mocks; no network or code checkout.
    harness = r"""
      const run = {workflow_id: 1, head_repository: {id: 10}, head_branch: 'main',
        event: 'push', status: 'completed', conclusion: 'success', head_sha: 'a'.repeat(40)};
      const repository = {id: 10, full_name: 'ZacharyZurloMSFT/foundry-frontier', default_branch: 'main'};
      const context = {repo: {owner: 'ZacharyZurloMSFT', repo: 'foundry-frontier'}, runId: 99,
        eventName: 'workflow_run', sha: run.head_sha, ref: 'refs/heads/main',
        payload: {workflow_run: run, inputs: {expected_sha: ''}}};
      const scenario = process.argv[1];
      let main = run.head_sha;
      let eligible = [run];
      if (scenario.startsWith('dispatch') || scenario === 'unchecked-dispatch') context.eventName = 'workflow_dispatch';
      if (scenario === 'pull-request-source' || scenario === 'dispatch-pr-ci') run.event = 'pull_request';
      if (scenario === 'fork') run.head_repository.id = 11;
      if (scenario === 'wrong-branch') run.head_branch = 'pr';
      if (scenario === 'failed-ci') run.conclusion = 'failure';
      if (scenario === 'old-main') main = 'b'.repeat(40);
      if (scenario === 'unchecked-dispatch') eligible = [];
      if (scenario === 'dispatch-non-main') context.ref = 'refs/heads/pr';
      if (scenario === 'dispatch-arbitrary-sha') context.payload.inputs.expected_sha = '$(touch injected)';
      if (scenario === 'wrong-ci-workflow') run.workflow_id = 2;
      if (scenario === 'wrong-repository') repository.full_name = 'fork/foundry-frontier';
      if (scenario === 'wrong-workflow-sha') context.sha = 'b'.repeat(40);
      const outputs = {};
      const github = {rest: {
        repos: {get: async () => ({data: repository}), getCommit: async () => ({data: {sha: main}})},
        actions: {getWorkflow: async () => ({data: {id: 1}}),
          listWorkflowRuns: async q => ({data: {workflow_runs: q.workflow_id === 1 ? eligible : []}})}
      }};
      const core = {setOutput: (k,v) => outputs[k] = v};
      const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
      new AsyncFunction('context', 'github', 'core', SCRIPT)(context, github, core)
        .then(() => console.log(JSON.stringify(outputs)))
        .catch(e => {console.log(e.message); process.exitCode = 1;});
    """.replace("SCRIPT", json.dumps(script))
    result = subprocess.run(["node", "-e", harness, case], capture_output=True, text=True, check=False)
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr
    if allowed:
        assert json.loads(result.stdout)["sha"] == SOURCE_SHA
