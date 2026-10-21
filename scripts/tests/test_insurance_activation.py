import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from apps.api.app.insurance_config import (
    INSURANCE_AGENT_NAMES, configuration_hash, insurance_routing, validate_fleet_receipt,
)
from test_release_app import Arm, SOURCE_SHA, arguments, execute, release_app, workflow
from test_release_app import record as rollback_record

record = rollback_record

SPEC = importlib.util.spec_from_file_location(
    "verify_insurance_fleet", Path(__file__).parents[1] / "verify-insurance-fleet.py",
)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def test_workflow_insurance_mutations_are_explicit_api_only_and_evidence_gated():
    config = workflow()
    inputs = config["on"]["workflow_dispatch"]["inputs"]
    assert inputs["insurance_mode"]["default"] == "preserve"
    assert inputs["insurance_mode"]["options"] == ["preserve", "configure", "disable", "activate"]
    deploy = config["jobs"]["deploy"]
    scripts = "\n".join(step.get("run", "") for step in deploy["steps"])
    assert 'test "$SERVICES_JSON" = \'["api"]\'' in scripts
    assert 'test "$GITHUB_EVENT_NAME" = workflow_dispatch' in scripts
    assert '--insurance-mode "$INSURANCE_RELEASE_MODE"' in scripts
    assert "--verification-receipt .release-state/insurance-verification.json" in scripts
    assert '--verification-sha256 "$INSURANCE_VERIFICATION_SHA256"' in scripts
    artifact = next(step for step in deploy["steps"] if step.get("name") == "Preserve the exact verified activation evidence")
    assert artifact["if"].startswith("success()")
    assert artifact["with"]["include-hidden-files"] == "true"
    assert deploy["steps"][-1]["if"] == "always()"


def fleet_receipt():
    return {
        "state": "verified", "source_commit": SOURCE_SHA,
        "routing_sha256": configuration_hash(insurance_routing()),
        "synthetic": True, "draft_execution_status": "not_executed",
        "workflow_id": "wf-insurance-verification-unit", "hitl_id": "unit-review",
        "agents": {
            role: {"name": name, "version": "2", "runtime_verified": True,
                   **{key: "b" * 64 for key in verifier.HASH_KEYS}}
            for role, name in INSURANCE_AGENT_NAMES.items()
        },
    }


@pytest.mark.parametrize("change", ["source", "routing", "missing-role", "runtime", "executed", "hash"])
def test_activation_rejects_partial_stale_or_unverified_evidence(change):
    receipt = fleet_receipt()
    if change == "source":
        receipt["source_commit"] = "c" * 40
    elif change == "routing":
        receipt["routing_sha256"] = "d" * 64
    elif change == "missing-role":
        del receipt["agents"]["triage"]
    elif change == "runtime":
        receipt["agents"]["triage"]["runtime_verified"] = False
    elif change == "executed":
        receipt["draft_execution_status"] = "executed"
    else:
        receipt["agents"]["triage"]["prompt_sha256"] = "not-a-digest"
    with pytest.raises(ValueError):
        validate_fleet_receipt(receipt, SOURCE_SHA)


def configure_arm(args):
    arm = Arm(args)
    settings = arm.before["properties"]["template"]["containers"][0]["env"]
    for entry in settings:
        if entry["name"] == "FOUNDRY_PROJECT_ENDPOINT":
            entry["value"] = insurance_routing()["INSURANCE_PROJECT_ENDPOINT"].replace(
                "nw-insurance-claims", "northwind-orders",
            )
        if entry["name"] == "FOUNDRY_PROJECT_ENDPOINTS":
            entry["value"] = insurance_routing()["FOUNDRY_PROJECT_ENDPOINTS"]
    return arm


def test_scoped_insurance_configuration_removes_aliases_but_preserves_unrelated_controls_and_secrets(record):
    args = arguments(insurance_mode="configure")
    arm = configure_arm(args)
    result = execute(args, arm)
    assert result["state"] == "ready"
    env = {item["name"]: item for item in arm.patches[0]["properties"]["template"]["containers"][0]["env"]}
    assert "FOUNDRY_PROJECT_ENDPOINT" not in env and "ORDER_PROJECT_NAME" not in env
    assert {name: env[name]["value"] for name in insurance_routing()} == insurance_routing()
    assert env["NORTHWIND_INSURANCE_AGENTS_READY"]["value"] == "false"
    assert env["COPILOT_HARNESS_ENABLED"]["value"] == "true"
    assert env["NORTHWIND_BUSINESS_API_ENABLED"]["value"] == "false"
    assert env["TOKEN"]["secretRef"] == "secret"
    assert env["INLINE_TOKEN"]["value"] == "INLINE_SECRET_DO_NOT_PERSIST"
    rollback = json.loads(record["text"])
    assert rollback["restore_readiness"] is False
    assert "INLINE_SECRET_DO_NOT_PERSIST" not in record["text"]


def test_live_activation_requires_intact_evidence_and_a_fresh_serving_check(tmp_path, record):
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(fleet_receipt()), encoding="utf-8")
    args = arguments(
        insurance_mode="activate", verification_receipt=path,
        verification_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    arm = configure_arm(args)
    with pytest.raises(release_app.ReleaseError, match="fresh independent"):
        execute(args, arm)
    assert arm.patches == []


def test_activation_never_accepts_a_modified_receipt(tmp_path):
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(fleet_receipt()), encoding="utf-8")
    args = arguments(insurance_mode="activate", verification_receipt=path, verification_sha256="0" * 64)
    with pytest.raises(ValueError, match="SHA-256"):
        release_app.validate_release(args)


def test_successful_activation_requires_the_serving_check_before_the_patch(tmp_path, record):
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(fleet_receipt()), encoding="utf-8")
    args = arguments(
        insurance_mode="activate", verification_receipt=path,
        verification_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    arm = configure_arm(args)
    checks = []
    def verify():
        assert not arm.patches
        checks.append("current serving versions independently checked")
    assert execute(args, arm, pre_activation=verify)["state"] == "ready"
    assert len(checks) == 1
    env = {item["name"]: item.get("value") for item in arm.patches[0]["properties"]["template"]["containers"][0]["env"]}
    assert env["NORTHWIND_INSURANCE_AGENTS_READY"] == "true"


def test_runtime_verification_checks_actual_definition_and_observed_metadata():
    environment = {"NORTHWIND_BUSINESS_DOMAIN": "insurance", "AGENT_ROLE": "triage"}
    receipt = {
        "role": "triage", "agent": INSURANCE_AGENT_NAMES["triage"], "version": "2",
        "source_commit": SOURCE_SHA, "model": "gpt-4o",
        **{key: "b" * 64 for key in verifier.HASH_KEYS},
        "config_sha256": configuration_hash(environment),
    }
    version = {"version": "2", "status": "active", "definition": {
        "environment_variables": environment, "code_configuration": {"content_hash": receipt["source_sha256"]},
    }}
    detail = {"agent_endpoint": {"version_selector": {"version_selection_rules": [
        {"agent_version": "2", "traffic_percentage": 100, "type": "FixedRatio"},
    ]}}}
    response = {"id": "resp-unit", "status": "completed", "output": [], "metadata": {
        **{key: receipt[key] for key in verifier.HASH_KEYS if key != "source_sha256"},
        "source_commit": SOURCE_SHA, "source_clean": "true", "business_domain": "insurance",
        "agent_role": "triage", "model_deployment": "gpt-4o",
    }}
    assert verifier.verify_runtime(receipt, version, detail, response)["runtime_verified"]
    response["metadata"]["prompt_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="runtime source/config/prompt"):
        verifier.verify_runtime(receipt, version, detail, response)
    response["metadata"]["prompt_sha256"] = receipt["prompt_sha256"]
    version["definition"]["environment_variables"]["AGENT_ROLE"] = "orchestrator"
    with pytest.raises(ValueError, match="configuration drifted"):
        verifier.verify_runtime(receipt, version, detail, response)


def test_draft_verification_requires_tool_result_not_a_prose_success_claim():
    workflow = "wf-insurance-verification-unit"
    value = {"status": "pending_approval", "workflow_id": workflow, "hitl_id": "unit-review",
             "synthetic": True, "execution_status": "not_executed", "draft": {"action_type": "request_evidence"}}
    response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}]}
    with pytest.raises(ValueError, match="tool evidence"):
        verifier.pending_draft(response, workflow)
    response["output"] = [
        {"type": "function_call", "name": "draft_claim_action", "call_id": "unit"},
        {"type": "function_call_output", "call_id": "unit", "output": json.dumps(value)},
    ]
    assert verifier.pending_draft(response, workflow)["hitl_id"] == "unit-review"
    with pytest.raises(ValueError, match="tool evidence"):
        verifier.pending_draft(response, "wf-different")


def test_existing_knowledge_association_is_read_not_republished_or_trusted_by_name():
    endpoint = "https://unit.search.windows.net"
    connection = {
        "type": "RemoteTool",
        "target": endpoint + "/knowledgebases/northwind-insurance-kb/mcp?api-version=2026-05-01-preview",
        "credentials": {"type": "CustomKeys", "api-key": "unit-credential-never-persist"},
    }
    calls, wrong_index = [], False
    project = SimpleNamespace(connections=SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(as_dict=lambda: connection)))
    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.headers["api-key"] == "unit-credential-never-persist"
        if "/knowledgebases/" in request.url.path:
            body = {"knowledgeSources": [{"name": "northwind-insurance-knowledge-ks"}]}
        elif "/knowledgesources/" in request.url.path:
            body = {"kind": "searchIndex", "searchIndexParameters": {
                "searchIndexName": "northwind-knowledge" if wrong_index else "northwind-insurance-knowledge",
            }}
        else:
            body = {"name": "northwind-insurance-knowledge"}
        return httpx.Response(200, json=body)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        verifier.verify_knowledge(project, endpoint, http)
        wrong_index = True
        with pytest.raises(ValueError, match="unexpected source or index"):
            verifier.verify_knowledge(project, endpoint, http)
    assert len(calls) == 6
