import copy
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.ai.projects.models import FileDatasetVersion, FolderDatasetVersion, ModelDeployment, ModelDeploymentSku

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipelines" / "insurance-learning"))
from insurance_learning import prepare
from evals.insurance_native import PREFIX, build_plan, config_template, digest, execute_sdk
from evals.insurance_review import (
    approval_template, read_json, require_training_approval, sha256, verify_receipt, write_new,
)


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pack(tmp_path):
    directory = tmp_path / "pack"
    prepare(directory)
    return directory


def approved(pack, artifacts=None):
    names = artifacts or ["evaluation-candidates.jsonl", "annotation-candidates.jsonl",
                          "native-smoke-triage.jsonl", "native-smoke-lookup.jsonl", "native-smoke-policy_wording.jsonl"]
    result = approval_template(pack, names)
    current = datetime.now(timezone.utc)
    result.update(approved=True, approver="Explicit Test User", confirmation_source="unit-test-only-no-real-consent",
                  confirmation_text="Unit-test attestation only, not actual user permission.",
                  approved_at=(current - timedelta(minutes=5)).isoformat(),
                  expires_at=(current + timedelta(minutes=5)).isoformat())
    result["evaluation_budget"].update(approved=True, acknowledge_conditional_bound_not_service_billing_cap=True)
    return result


def verified_config(pack):
    config = config_template(pack)
    for key in ("target_model", "judge_model"):
        config[key].update(model_name="gpt-4o", model_version="unit-test-version", sku="GlobalStandard",
                           input_usd_per_million="2.5", output_usd_per_million="10",
                           pricing_verified=True, pricing_reference="test-not-live-price", pricing_verified_at="2026-10-14")
    config["bounds"].update(
        verified=True, evidence="unit test only", acknowledge_no_service_hard_cap=True,
        target_input_tokens_total_per_case=1000, target_output_tokens_total_per_case=1000,
        target_non_token_usd_per_case="0", judge_input_tokens_per_call=1000,
        judge_calls_per_dimension_per_case=1, judge_non_token_usd_per_case="0",
    )
    for role, spec in config["datasets"].items():
        spec.update(dataset_id=PREFIX + role + "/versions/2", uploaded_bytes_verified=True,
                    uploaded_bytes_evidence="unit-test readback", prompt_binding_verified=True,
                    prompt_binding_evidence="unit-test runtime binding", internal_concurrency_bounded=True,
                    agent_source_sha256="a" * 64, agent_config_sha256="b" * 64)
        spec["target"].update(name="unit-test-insurance-" + role, version="2")
    return config


def test_approval_template_is_not_user_consent_or_annotation(pack):
    template = approval_template(pack)
    assert template["approved"] is False and template["approver"] == ""
    assert template["individual_human_annotations"] == 0
    with pytest.raises(ValueError, match="Explicit"):
        verify_receipt(pack, template, ["evaluation-candidates.jsonl"], "evaluate")


@pytest.mark.parametrize("mutation", ["assistant", "wrong_hash", "wrong_artifact", "training", "human_labels", "no_confirmation", "expired"])
def test_user_receipt_is_hash_bound_scope_bound_and_not_assistant_approval(pack, mutation):
    receipt = approved(pack)
    if mutation == "assistant":
        receipt["approver"] = "Copilot Assistant"
    elif mutation == "wrong_hash":
        receipt["manifest_sha256"] = "0" * 64
    elif mutation == "wrong_artifact":
        receipt["artifacts_sha256"]["evaluation-candidates.jsonl"] = "0" * 64
    elif mutation == "training":
        receipt["scope"] = "training"
    elif mutation == "human_labels":
        receipt["individual_human_annotations"] = 24
    elif mutation == "no_confirmation":
        receipt["confirmation_text"] = ""
    else:
        receipt["expires_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        verify_receipt(pack, receipt, ["evaluation-candidates.jsonl"], "evaluate")


def test_insurance_training_fails_closed_even_with_evaluation_consent(pack):
    with pytest.raises(ValueError, match="separate reviewed training release"):
        require_training_approval(read_json(pack / "manifest.json"), approved(pack))
    require_training_approval({"business_domain": "other"})


def test_dry_native_plan_needs_no_credentials_and_no_real_approval(pack, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "do-not-use")
    config = config_template(pack)
    plan = build_plan(pack, config)
    assert plan["selected_samples"] == 8 and plan["apply_blockers"]
    assert plan["conditional_cost_upper_bound_usd"] is None
    assert all(j["run"]["data_source"]["source"]["type"] == "file_id" for j in plan["jobs"].values())
    script = load_script("run-insurance-quality-gates.py")
    assert script.main(["--pack", str(pack), "--print-config-template"]) == 0
    assert script.main(["--pack", str(pack)]) == 0


def test_native_plan_binds_identity_cost_full_artifacts_and_all_dimensions(pack):
    config = verified_config(pack)
    plan = build_plan(pack, config, approved(pack))
    assert plan["apply_blockers"] == []
    assert plan["conditional_token_upper_bound"] == 76480
    assert float(plan["conditional_cost_upper_bound_usd"]) < 1
    assert all(len(job["create"]["testing_criteria"]) == 5 for job in plan["jobs"].values())
    assert "claim_action_draft" not in plan["jobs"]


@pytest.mark.parametrize("mutation", ["bool_limit", "huge_tokens", "cost", "unknown_role", "changed_remote_bytes",
                                     "partial_dataset", "unverified_bounds", "no_budget"])
def test_native_preflight_cannot_weaken_limits_or_source_authorization(pack, mutation):
    config = verified_config(pack)
    approval = approved(pack)
    if mutation == "bool_limit":
        config["max_samples"] = True
    elif mutation == "huge_tokens":
        config["bounds"]["target_input_tokens_total_per_case"] = 1000000
    elif mutation == "cost":
        config["target_model"]["output_usd_per_million"] = "100000"
    elif mutation == "unknown_role":
        config["datasets"]["orchestrator"] = copy.deepcopy(config["datasets"]["lookup"])
    elif mutation == "changed_remote_bytes":
        config["datasets"]["lookup"]["uploaded_sha256"] = "0" * 64
    elif mutation == "partial_dataset":
        config["datasets"]["lookup"]["artifact"] = "evaluation-candidates.jsonl"
    elif mutation == "unverified_bounds":
        config["bounds"]["verified"] = False
    else:
        approval["evaluation_budget"]["approved"] = False
    try:
        plan = build_plan(pack, config, approval)
        assert plan["apply_blockers"]
    except ValueError:
        pass


def test_review_script_only_writes_unapproved_template_and_refuses_overwrite(pack, tmp_path):
    script = load_script("review-insurance-datasets.py")
    output = tmp_path / "pending-user-approval.json"
    argv = ["--pack", str(pack), "--approval-template", str(output)]
    assert script.main(argv) == 0
    assert read_json(output)["approved"] is False
    assert script.main(argv) == 2


def test_offline_cli_without_consent_does_not_score_or_write_model_results(pack, tmp_path):
    script = load_script("run-insurance-quality-gates.py")
    path = tmp_path / "not-created.json"
    assert script.main(["--pack", str(pack), "--execute", "--output", str(path)]) == 2
    assert not path.exists()


def test_native_ambiguous_post_never_replayed(pack):
    config = verified_config(pack)
    config["datasets"] = {"triage": config["datasets"]["triage"]}
    plan = build_plan(pack, config, approved(pack))
    receipt = {"jobs": {}, "plan_sha256": digest(plan)}
    saved = []
    class Project:
        class deployments:
            @staticmethod
            def get(_):
                return {"model_name": "gpt-4o", "model_version": "unit-test-version", "sku": {"name": "GlobalStandard"}}
        class agents:
            @staticmethod
            def get_version(name, version):
                return {"version": version, "status": "active", "metadata": {
                    "source_sha256": "a" * 64, "config_sha256": "b" * 64},
                    "definition": {"model": "gpt-4o"}}
        class datasets:
            @staticmethod
            def get(name, version):
                return {"id": PREFIX + name + "/versions/" + version, "type": "uri_file", "is_single_file": True}
    class Client:
        class evals:
            @staticmethod
            def create(**_kwargs):
                raise TimeoutError("ambiguous server creation")
    with pytest.raises(TimeoutError):
        execute_sdk(Project(), Client(), plan, receipt, lambda: saved.append(copy.deepcopy(receipt)))
    assert saved[-1]["jobs"]["triage"]["state"] == "eval_submission_started"
    with pytest.raises(ValueError, match="reconciled"):
        execute_sdk(Project(), Client(), plan, receipt, lambda: None)


@pytest.mark.parametrize("mutation", [None, "missing_case", "wrong_gold", "no_output", "grader_error",
                                     "missing_response_id", "wrong_model_name", "wrong_model_version",
                                     "wrong_sku", "wrong_dataset_kind", "wrong_dataset_id"])
def test_native_sdk_success_retains_actual_results_and_incomplete_cases_never_pass(pack, mutation):
    config = verified_config(pack)
    config["datasets"] = {"triage": config["datasets"]["triage"]}
    plan = build_plan(pack, config, approved(pack))
    receipt = {"jobs": {}, "plan_sha256": digest(plan)}
    definition, requested_run = {}, {}
    from evals.insurance_review import rows
    sources = rows(pack / "native-smoke-triage.jsonl")
    def create_eval(**kwargs):
        definition.update(kwargs)
        return {"id": "actual-eval-id"}
    def retrieve_eval(**kwargs):
        return {"id": kwargs["eval_id"], **definition}
    def create_run(**kwargs):
        requested_run.update(kwargs)
        return {"id": "actual-run-id"}
    def retrieve_run(**_kwargs):
        return {"id": "actual-run-id", "eval_id": "actual-eval-id", "status": "completed",
                "data_source": requested_run["data_source"], "metadata": requested_run["metadata"]}
    def output_items(**_kwargs):
        data = [{
            "id": f"actual-item-{index}", "eval_id": "actual-eval-id", "run_id": "actual-run-id",
            "datasource_item": copy.deepcopy(source), "status": "pass",
            "sample": {"model": "gpt-4o", "response_id": f"actual-response-{index}",
                       "output": [{"role": "assistant", "content": '{"claim_id":null}'}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            "results": [{"name": criterion["name"], "score": 4, "passed": False,
                         "reason": "Actual mocked judge reason, not a live evaluation"}
                        for criterion in plan["jobs"]["triage"]["create"]["testing_criteria"]],
        } for index, source in enumerate(sources)]
        if mutation == "missing_case":
            data.pop()
        elif mutation == "wrong_gold":
            data[0]["datasource_item"]["ground_truth"] = "{}"
        elif mutation == "no_output":
            data[0]["sample"]["output"] = []
        elif mutation == "grader_error":
            data[0]["results"][0]["error"] = "mock error"
        elif mutation == "missing_response_id":
            data[0]["sample"].pop("response_id")
        return {"data": data, "has_more": False}
    project = SimpleNamespace(
        deployments=SimpleNamespace(get=lambda _: ModelDeployment(
            name="gpt-4o", model_name="different-model" if mutation == "wrong_model_name" else "gpt-4o",
            model_version="different-version" if mutation == "wrong_model_version" else "unit-test-version",
            model_publisher="OpenAI", capabilities={},
            sku=ModelDeploymentSku(name="Standard" if mutation == "wrong_sku" else "GlobalStandard"))),
        agents=SimpleNamespace(get_version=lambda _name, version: {
            "version": version, "status": "active",
            "metadata": {"source_sha256": "a" * 64, "config_sha256": "b" * 64},
            "definition": {"model": "gpt-4o"}}),
        datasets=SimpleNamespace(get=lambda name, version: (
            FolderDatasetVersion if mutation == "wrong_dataset_kind" else FileDatasetVersion)(
                id=PREFIX + name + "/versions/" + ("wrong-version" if mutation == "wrong_dataset_id" else version),
                name=name, version=version, data_uri="https://unit-test.invalid/smoke.jsonl")),
    )
    client = SimpleNamespace(evals=SimpleNamespace(
        create=create_eval, retrieve=retrieve_eval, runs=SimpleNamespace(
            create=create_run, retrieve=retrieve_run, output_items=SimpleNamespace(list=output_items))))
    if mutation not in (None, "missing_response_id"):
        with pytest.raises(ValueError):
            execute_sdk(project, client, plan, receipt, lambda: None)
        assert not receipt.get("native_run_completed")
        if mutation in ("wrong_model_name", "wrong_model_version", "wrong_sku", "wrong_dataset_kind", "wrong_dataset_id"):
            assert not definition and not requested_run and not receipt["jobs"]
    else:
        execute_sdk(project, client, plan, receipt, lambda: None)
        assert receipt["native_run_completed"] and not receipt["release_approved"]
        assert not receipt["native_judge_smoke_passed"]
        actual = receipt["jobs"]["triage"]["cases"][0]
        assert actual["response"] == '{"claim_id":null}'
        assert actual["response_id"] == (None if mutation else "actual-response-0")
        assert bool(receipt["missing_provenance_cases"]) == (mutation == "missing_response_id")
        assert receipt["preflight"]["models"]["target_model"]["model_name"] == "gpt-4o"
        assert receipt["preflight"]["models"]["target_model"]["model_version"] == "unit-test-version"
        assert receipt["preflight"]["targets"]["triage"]["dataset_kind"] == "uri_file"


@pytest.mark.parametrize("environment,reaches_submission", [
    ({"NORTHWIND_BUSINESS_DOMAIN": "insurance"}, True),
    ({}, True),
    ({"NORTHWIND_BUSINESS_DOMAIN": "other"}, False),
])
def test_hosted_agent_preflight_rejects_only_an_explicit_non_insurance_domain(pack, environment, reaches_submission):
    config = verified_config(pack)
    config["datasets"] = {"triage": config["datasets"]["triage"]}
    plan = build_plan(pack, config, approved(pack))
    receipt = {"jobs": {}, "plan_sha256": digest(plan)}
    submitted = []

    def create_eval(**_kwargs):
        submitted.append(True)
        raise TimeoutError("preflight passed; stop before any native work")

    project = SimpleNamespace(
        deployments=SimpleNamespace(get=lambda _: ModelDeployment(
            name="gpt-4o", model_name="gpt-4o", model_version="unit-test-version", model_publisher="OpenAI",
            capabilities={}, sku=ModelDeploymentSku(name="GlobalStandard"))),
        agents=SimpleNamespace(get_version=lambda _name, version: {
            "version": version, "status": "active",
            "metadata": {"source_sha256": "a" * 64, "config_sha256": "b" * 64},
            "definition": {"kind": "hosted", "model": "gpt-4o", "environment_variables": environment}}),
        datasets=SimpleNamespace(get=lambda name, version: FileDatasetVersion(
            id=PREFIX + name + "/versions/" + version, name=name, version=version,
            data_uri="https://unit-test.invalid/smoke.jsonl")),
    )
    client = SimpleNamespace(evals=SimpleNamespace(create=create_eval))
    with pytest.raises(TimeoutError if reaches_submission else ValueError):
        execute_sdk(project, client, plan, receipt, lambda: None)
    assert bool(submitted) is reaches_submission


def test_real_sdk_serialization_matches_documented_fields_without_invented_dataset_flags():
    deployment = ModelDeployment(
        name="gpt-4o", model_name="gpt-4o", model_version="unit-test-version",
        model_publisher="OpenAI", capabilities={}, sku=ModelDeploymentSku(name="GlobalStandard"))
    assert deployment.model_name == deployment.as_dict()["modelName"]
    assert deployment.model_version == deployment.as_dict()["modelVersion"]
    assert "model_name" not in deployment.as_dict() and "model_version" not in deployment.as_dict()
    dataset = FileDatasetVersion(name="test", version="2", data_uri="https://unit-test.invalid/data.jsonl")
    assert dataset.as_dict()["type"] == "uri_file"
    assert "is_single_file" not in dataset.as_dict() and "isSingleFile" not in dataset.as_dict()


def test_approved_insurance_v2_manifest_and_artifact_bytes_are_unchanged():
    pack = ROOT / "pipelines" / "insurance-learning" / "insurance-upload-pack" / "insurance-2026-10-14-v2"
    assert sha256(pack / "manifest.json") == "a528a43c451ef3d2ec5357978b0fefd8a2f3fb3d9d10e26c275f99da6b6f618e"
    manifest = read_json(pack / "manifest.json")
    assert all(sha256(pack / name) == expected for name, expected in manifest["files_sha256"].items())


def test_code_lineage_changed_after_pack_requires_new_approval(pack):
    manifest_path = pack / "manifest.json"
    manifest = read_json(manifest_path)
    key = next(iter(manifest["source_code_sha256"]))
    manifest["source_code_sha256"][key] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="source changed"):
        build_plan(pack, verified_config(pack))
