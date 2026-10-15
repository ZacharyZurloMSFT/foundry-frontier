import copy
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evals import insurance_native
from evals.insurance_review import (
    EXECUTION_REVIEW_FINDINGS, EXECUTION_REVIEW_TESTS, approval_template, execution_review_test_command,
    lineage_record, read_json, sha256, verify_code_lineage,
)

APPROVED_PACK = ROOT / "pipelines" / "orders-learning" / "insurance-upload-pack" / "insurance-2026-10-14-v2"


def current_sources(manifest):
    return {name: sha256(ROOT / name) for name in manifest["source_code_sha256"]}


def result_properties(manifest_hash, executed, tests, recorded_at, pytest_args):
    return {
        "insurance_prepared_manifest_sha256": manifest_hash,
        "insurance_executed_source_code_sha256": json.dumps(executed, sort_keys=True),
        "insurance_test_sources_sha256": json.dumps(tests, sort_keys=True),
        "insurance_test_evidence_recorded_at": recorded_at,
        "insurance_test_pytest_args": json.dumps(pytest_args),
    }


def write_unit_result(case):
    properties = result_properties(
        sha256(case.pack / "manifest.json"), current_sources(read_json(case.pack / "manifest.json")),
        case.receipt["test_evidence"]["test_sources_sha256"],
        (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        case.receipt["test_evidence"]["command"][3:])
    report = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(report, "testsuite", name="pytest", tests="3", failures="0", errors="0", skipped="0")
    props = ElementTree.SubElement(suite, "properties")
    for name, value in properties.items():
        ElementTree.SubElement(props, "property", name=name, value=value)
    for name in EXECUTION_REVIEW_TESTS:
        ElementTree.SubElement(suite, "testcase", classname=str(Path(name).with_suffix("")).replace("\\", "."),
                              name="unit_test_fixture_only_not_actual_execution_evidence")
    case.result.write_bytes(ElementTree.tostring(report, encoding="utf-8"))
    case.receipt["test_evidence"]["result_sha256"] = sha256(case.result)


def pin_unit_receipt(case):
    case.review.write_text(json.dumps(case.receipt), encoding="utf-8")
    return {
        "execution_code_review": case.review, "execution_code_review_sha256": sha256(case.review),
        "approval_receipt_path": case.approval,
    }


@pytest.fixture
def reviewed(tmp_path):
    pack = tmp_path / "pack"
    shutil.copytree(APPROVED_PACK, pack)
    approval = approval_template(pack, [
        "evaluation-candidates.jsonl", "native-smoke-triage.jsonl",
        "native-smoke-lookup.jsonl", "native-smoke-policy_wording.jsonl",
    ])
    current = datetime.now(timezone.utc)
    approval.update(
        approved=True, approver="Unit Test Operator", confirmation_source="unit-test-only-no-real-user-consent",
        confirmation_text="Unit-test fixture, not actual authorization.",
        approved_at=(current - timedelta(minutes=5)).isoformat(),
        expires_at=(current + timedelta(minutes=15)).isoformat())
    approval_path = tmp_path / "unit-test-user-receipt.json"
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    manifest = read_json(pack / "manifest.json")
    executed = current_sources(manifest)
    result = tmp_path / "unit-test-results.xml"
    result_relative = str(result.relative_to(ROOT))
    receipt = {
        "schema_version": 1, "kind": "insurance_reviewed_execution_code",
        "receipt_version": "unit-test-execution-code-v1", "actor_type": "assistant",
        "reviewer_session_id": "unit-test-only-not-actual-parent-review",
        "reviewed_at": (current - timedelta(minutes=1)).isoformat(),
        "expires_at": (current + timedelta(minutes=10)).isoformat(),
        "scope": "evaluation_only_no_training", "individual_human_annotations": 0,
        "manifest_sha256": sha256(pack / "manifest.json"),
        "artifact_approval_receipt_sha256": sha256(approval_path),
        "changes": {
            name: {"prepared_sha256": before, "executed_sha256": executed[name],
                   "finding_ids": sorted(EXECUTION_REVIEW_FINDINGS[name])}
            for name, before in manifest["source_code_sha256"].items() if before != executed[name]
        },
        "test_evidence": {
            "command": execution_review_test_command(result_relative), "result_path": result_relative,
            "result_sha256": "",
            "test_sources_sha256": {name: sha256(ROOT / name) for name in EXECUTION_REVIEW_TESTS},
        },
    }
    case = SimpleNamespace(pack=pack, approval=approval_path, result=result,
                           review=tmp_path / "unit-test-execution-code-v1.json", receipt=receipt)
    write_unit_result(case)
    pin_unit_receipt(case)
    return case


def test_execution_review_evidence_records_current_frozen_source_snapshot(record_testsuite_property, request):
    assert not any(request.config.getoption(option, default=False)
                   for option in ("keyword", "markexpr", "deselect", "collectonly", "lf"))
    manifest = read_json(APPROVED_PACK / "manifest.json")
    for name, value in result_properties(
        sha256(APPROVED_PACK / "manifest.json"), current_sources(manifest),
        {path: sha256(ROOT / path) for path in EXECUTION_REVIEW_TESTS},
        datetime.now(timezone.utc).isoformat(), sys.argv[1:],
    ).items():
        record_testsuite_property(name, value)


def test_independently_pinned_overlay_preserves_prepared_and_executed_maps(reviewed):
    original = read_json(reviewed.pack / "manifest.json")
    result = verify_code_lineage(reviewed.pack, ROOT, **pin_unit_receipt(reviewed))
    assert result["source_code_sha256"] == result["prepared_source_code_sha256"] == original["source_code_sha256"]
    assert result["executed_source_code_sha256"] == current_sources(original)
    assert set(reviewed.receipt["changes"]) == set(EXECUTION_REVIEW_FINDINGS)
    assert result["execution_code_review_receipt_sha256"] == sha256(reviewed.review)
    assert result["execution_code_review_receipt"]["actor_type"] == "assistant"
    assert read_json(reviewed.pack / "manifest.json") == original


def test_without_overlay_still_blocks_reviewed_source_drift(reviewed):
    with pytest.raises(ValueError, match="source changed"):
        verify_code_lineage(reviewed.pack, ROOT)


@pytest.mark.parametrize("mutation", [
    "missing_pin", "missing_file", "wrong_pin", "missing_approval", "wrong_approval_bytes", "unknown_field",
    "wrong_manifest", "human_actor", "human_annotation", "training_scope", "boolean_version", "unversioned",
    "expired", "future", "no_timezone", "long_attestation", "missing_transition", "extra_transition",
    "unauthorized_path", "aliased_path", "wrong_prepared", "wrong_executed", "unknown_transition_field",
    "missing_finding", "duplicate_finding", "unknown_finding", "unknown_evidence_field", "missing_test",
    "extra_test", "changed_test", "filtered_tests", "wrong_result_hash", "evidence_outside_project",
])
def test_overlay_rejects_unreviewed_or_misbound_receipt(reviewed, mutation):
    receipt = reviewed.receipt
    transition = next(iter(receipt["changes"].values()))
    if mutation == "wrong_approval_bytes":
        reviewed.approval.write_text(reviewed.approval.read_text() + "\n", encoding="utf-8")
    elif mutation == "unknown_field":
        receipt["approved"] = True
    elif mutation == "wrong_manifest":
        receipt["manifest_sha256"] = "0" * 64
    elif mutation == "human_actor":
        receipt["actor_type"] = "human_user"
    elif mutation == "human_annotation":
        receipt["individual_human_annotations"] = 24
    elif mutation == "training_scope":
        receipt["scope"] = "training"
    elif mutation == "boolean_version":
        receipt["schema_version"] = True
    elif mutation == "unversioned":
        receipt["receipt_version"] = "latest"
    elif mutation == "expired":
        receipt["expires_at"] = "2020-01-01T00:00:00Z"
    elif mutation == "future":
        receipt["reviewed_at"] = "2100-01-01T00:00:00Z"
    elif mutation == "no_timezone":
        receipt["reviewed_at"] = "2026-10-14T12:00:00"
    elif mutation == "long_attestation":
        receipt["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    elif mutation == "missing_transition":
        receipt["changes"].pop(next(iter(receipt["changes"])))
    elif mutation == "extra_transition":
        receipt["changes"][r"scripts\review-insurance-datasets.py"] = copy.deepcopy(transition)
    elif mutation in ("unauthorized_path", "aliased_path"):
        name = next(iter(receipt["changes"]))
        receipt["changes"][r"evals\insurance.py" if mutation == "unauthorized_path" else ".\\" + name] = receipt["changes"].pop(name)
    elif mutation == "wrong_prepared":
        transition["prepared_sha256"] = "0" * 64
    elif mutation == "wrong_executed":
        transition["executed_sha256"] = "0" * 64
    elif mutation == "unknown_transition_field":
        transition["approved"] = True
    elif mutation == "missing_finding":
        transition["finding_ids"].pop()
    elif mutation == "duplicate_finding":
        transition["finding_ids"].append(transition["finding_ids"][0])
    elif mutation == "unknown_finding":
        transition["finding_ids"].append("INS-ARBITRARY-SNAPSHOT")
    elif mutation == "unknown_evidence_field":
        receipt["test_evidence"]["passed"] = True
    elif mutation == "missing_test":
        receipt["test_evidence"]["test_sources_sha256"].pop(EXECUTION_REVIEW_TESTS[0])
    elif mutation == "extra_test":
        receipt["test_evidence"]["test_sources_sha256"][r"evals\insurance.py"] = "0" * 64
    elif mutation == "changed_test":
        receipt["test_evidence"]["test_sources_sha256"][EXECUTION_REVIEW_TESTS[0]] = "0" * 64
    elif mutation == "filtered_tests":
        receipt["test_evidence"]["command"].extend(["-k", "easy"])
    elif mutation == "wrong_result_hash":
        receipt["test_evidence"]["result_sha256"] = "0" * 64
    elif mutation == "evidence_outside_project":
        receipt["test_evidence"]["result_path"] = r"..\outside.xml"
    options = pin_unit_receipt(reviewed)
    if mutation == "missing_pin":
        options.pop("execution_code_review_sha256")
    elif mutation == "missing_file":
        options.pop("execution_code_review")
    elif mutation == "wrong_pin":
        options["execution_code_review_sha256"] = "0" * 64
    elif mutation == "missing_approval":
        options.pop("approval_receipt_path")
    with pytest.raises(ValueError):
        verify_code_lineage(reviewed.pack, ROOT, **options)


@pytest.mark.parametrize("mutation", ["failed", "skipped", "errors", "missing_module", "duplicate_case", "wrong_counts",
                                     "missing_properties", "wrong_source", "wrong_tests", "wrong_manifest", "stale",
                                     "future", "wrong_command", "duplicate_property", "unsafe_xml", "malformed_xml"])
def test_overlay_rejects_incomplete_stale_or_wrong_source_test_results(reviewed, mutation):
    report = ElementTree.fromstring(reviewed.result.read_bytes())
    suite = report[0]
    props = suite.find("properties")
    if mutation in ("failed", "skipped", "errors"):
        suite.set({"failed": "failures", "skipped": "skipped", "errors": "errors"}[mutation], "1")
    elif mutation == "missing_module":
        suite.remove(suite.findall("testcase")[0])
        suite.set("tests", "2")
    elif mutation == "duplicate_case":
        suite.append(copy.deepcopy(suite.findall("testcase")[0]))
        suite.set("tests", "4")
    elif mutation == "wrong_counts":
        suite.set("tests", "100000")
    elif mutation == "missing_properties":
        suite.remove(props)
    elif mutation == "duplicate_property":
        props.append(copy.deepcopy(props[0]))
    elif mutation in ("wrong_source", "wrong_tests", "wrong_manifest", "stale", "future", "wrong_command"):
        key, value = {
            "wrong_source": ("insurance_executed_source_code_sha256", "{}"),
            "wrong_tests": ("insurance_test_sources_sha256", "{}"),
            "wrong_manifest": ("insurance_prepared_manifest_sha256", "0" * 64),
            "stale": ("insurance_test_evidence_recorded_at", "2020-01-01T00:00:00Z"),
            "future": ("insurance_test_evidence_recorded_at", "2100-01-01T00:00:00Z"),
            "wrong_command": ("insurance_test_pytest_args", '["-k", "easy"]'),
        }[mutation]
        next(prop for prop in props if prop.get("name") == key).set("value", value)
    result_bytes = ElementTree.tostring(report, encoding="utf-8")
    if mutation == "unsafe_xml":
        result_bytes = b"<!DOCTYPE testsuites [<!ENTITY example 'unsafe'>]>" + result_bytes
    elif mutation == "malformed_xml":
        result_bytes = b"<invalid"
    reviewed.result.write_bytes(result_bytes)
    reviewed.receipt["test_evidence"]["result_sha256"] = sha256(reviewed.result)
    with pytest.raises(ValueError):
        verify_code_lineage(reviewed.pack, ROOT, **pin_unit_receipt(reviewed))


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_receipt_json_keys_never_pass_even_with_correct_external_pin(reviewed, nested):
    options = pin_unit_receipt(reviewed)
    text = reviewed.review.read_text()
    if nested:
        text = text.replace('"finding_ids":', '"finding_ids": [], "finding_ids":', 1)
    else:
        text = text[:-1] + ', "scope": "evaluation_only_no_training"}'
    reviewed.review.write_text(text, encoding="utf-8")
    options["execution_code_review_sha256"] = sha256(reviewed.review)
    with pytest.raises(ValueError, match="Duplicate"):
        verify_code_lineage(reviewed.pack, ROOT, **options)


@pytest.mark.parametrize("mutation", ["not_approved", "assistant", "training", "expired"])
def test_code_review_cannot_supply_missing_user_artifact_authorization(reviewed, mutation):
    approval = read_json(reviewed.approval)
    if mutation == "not_approved":
        approval["approved"] = False
    elif mutation == "assistant":
        approval["actor_type"] = "assistant"
    elif mutation == "training":
        approval["scope"] = "training"
    else:
        approval["expires_at"] = "2020-01-01T00:00:00Z"
    reviewed.approval.write_text(json.dumps(approval), encoding="utf-8")
    reviewed.receipt["artifact_approval_receipt_sha256"] = sha256(reviewed.approval)
    with pytest.raises(ValueError):
        verify_code_lineage(reviewed.pack, ROOT, **pin_unit_receipt(reviewed))


def test_execution_review_receipt_must_stay_outside_the_immutable_pack(reviewed):
    reviewed.review = reviewed.pack / "not-allowed-review-v1.json"
    with pytest.raises(ValueError, match="outside"):
        verify_code_lineage(reviewed.pack, ROOT, **pin_unit_receipt(reviewed))


@pytest.mark.parametrize("mutation", ["artifact", "prompt", "fixture", "unapproved_source"])
def test_overlay_cannot_authorize_other_frozen_inputs(reviewed, mutation):
    manifest = read_json(reviewed.pack / "manifest.json")
    if mutation == "artifact":
        artifact = reviewed.pack / "evaluation-candidates.jsonl"
        artifact.write_bytes(artifact.read_bytes() + b"\n")
    elif mutation == "prompt":
        manifest["prompt_sources_sha256"][next(iter(manifest["prompt_sources_sha256"]))] = "0" * 64
    elif mutation == "fixture":
        manifest["fixture_sha256"] = "0" * 64
    else:
        name = r"evals\insurance.py"
        manifest["source_code_sha256"][name] = "0" * 64
        reviewed.receipt["changes"][name] = {
            "prepared_sha256": "0" * 64, "executed_sha256": sha256(ROOT / name),
            "finding_ids": ["INS-LINEAGE-001"],
        }
    (reviewed.pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    reviewed.receipt["manifest_sha256"] = sha256(reviewed.pack / "manifest.json")
    approval = read_json(reviewed.approval)
    approval["manifest_sha256"] = reviewed.receipt["manifest_sha256"]
    reviewed.approval.write_text(json.dumps(approval), encoding="utf-8")
    reviewed.receipt["artifact_approval_receipt_sha256"] = sha256(reviewed.approval)
    with pytest.raises(ValueError):
        verify_code_lineage(reviewed.pack, ROOT, **pin_unit_receipt(reviewed))


def test_native_plan_threads_overlay_without_verifying_budget_or_replacing_prepared_map(reviewed):
    config = insurance_native.config_template(reviewed.pack)
    plan = insurance_native.build_plan(reviewed.pack, config, **pin_unit_receipt(reviewed))
    assert plan["prepared_source_code_sha256"] == plan["source_code_sha256"]
    assert plan["executed_source_code_sha256"] != plan["prepared_source_code_sha256"]
    assert plan["execution_code_review_receipt_sha256"] == sha256(reviewed.review)
    assert plan["apply_blockers"] and plan["bounds"]["verified"] is False
    assert not plan["target_model"]["pricing_verified"] and not plan["judge_model"]["pricing_verified"]
    assert not plan["training_approved"] and not plan["release_approved"]
    wrong_approval = read_json(reviewed.approval)
    wrong_approval["approver"] = "Different Operator"
    with pytest.raises(ValueError, match="Supplied approval differs"):
        insurance_native.build_plan(reviewed.pack, config, wrong_approval, **pin_unit_receipt(reviewed))


def test_native_execute_stays_blocked_before_any_credential_or_cloud_access(reviewed, monkeypatch, tmp_path):
    import azure.ai.projects
    import azure.identity
    def forbidden(*_args, **_kwargs):
        raise AssertionError("No credential or cloud construction is authorized by a code review")
    monkeypatch.setattr(azure.ai.projects, "AIProjectClient", forbidden)
    monkeypatch.setattr(azure.identity, "AzureDeveloperCliCredential", forbidden)
    options = pin_unit_receipt(reviewed)
    options.pop("approval_receipt_path")
    output = tmp_path / "never-submitted.json"
    with pytest.raises(ValueError, match="verification|Verified"):
        insurance_native.execute(reviewed.pack, insurance_native.config_template(reviewed.pack),
                                 reviewed.approval, output, sha256(reviewed.approval), **options)
    assert not output.exists()


@pytest.mark.parametrize("native", [False, True])
def test_cli_threads_review_and_records_both_full_maps_without_execution(reviewed, tmp_path, native):
    spec = importlib.util.spec_from_file_location("insurance_overlay_cli", ROOT / "scripts" / "run-insurance-quality-gates.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    output = tmp_path / "dry-plan.json"
    options = pin_unit_receipt(reviewed)
    argv = [
        "--pack", str(reviewed.pack), "--approval-receipt", str(reviewed.approval),
        "--execution-code-review", str(reviewed.review),
        "--execution-code-review-sha256", options["execution_code_review_sha256"], "--output", str(output),
    ]
    if native:
        config_path = tmp_path / "unverified-native-config.json"
        config_path.write_text(json.dumps(insurance_native.config_template(reviewed.pack)), encoding="utf-8")
        argv.extend(["--config", str(config_path)])
    assert script.main(argv) == 0
    plan = read_json(output)
    assert plan["prepared_source_code_sha256"] == plan["source_code_sha256"]
    assert plan["executed_source_code_sha256"] != plan["prepared_source_code_sha256"]
    assert plan["execution_code_review_receipt_sha256"] == sha256(reviewed.review)
    assert not plan["release_approved"] and not plan["training_approved"]


def test_offline_cli_run_receipt_records_both_maps_without_pretending_missing_cases_pass(reviewed, tmp_path):
    spec = importlib.util.spec_from_file_location("insurance_overlay_offline_cli", ROOT / "scripts" / "run-insurance-quality-gates.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    observations = tmp_path / "no-model-results.jsonl"
    observations.write_text("", encoding="utf-8")
    output = tmp_path / "offline-missing-cases.json"
    assert script.main([
        "--pack", str(reviewed.pack), "--approval-receipt", str(reviewed.approval),
        "--execution-code-review", str(reviewed.review),
        "--execution-code-review-sha256", pin_unit_receipt(reviewed)["execution_code_review_sha256"],
        "--observations", str(observations), "--execute", "--confirm", sha256(reviewed.approval),
        "--output", str(output),
    ]) == 1
    result = read_json(output)
    assert not result["contract_passed"] and result["metrics"] is None
    assert result["prepared_source_code_sha256"] == result["source_code_sha256"]
    assert result["executed_source_code_sha256"] != result["prepared_source_code_sha256"]
    assert result["execution_code_review_receipt_sha256"] == sha256(reviewed.review)


def test_native_sdk_run_receipt_keeps_the_complete_reviewed_source_lineage(reviewed):
    verified = verify_code_lineage(reviewed.pack, ROOT, **pin_unit_receipt(reviewed))
    plan = {"apply_blockers": [], **lineage_record(verified),
            "target_model": {"deployment": "unit-test-no-provider"}, "judge_model": {}}
    receipt = {"jobs": {}, "plan_sha256": insurance_native.digest(plan)}
    def no_provider(_deployment):
        raise RuntimeError("Unit test stops before any provider/network operation")
    project = SimpleNamespace(deployments=SimpleNamespace(get=no_provider))
    with pytest.raises(RuntimeError, match="Unit test stops"):
        insurance_native.execute_sdk(project, None, plan, receipt, lambda: None)
    assert lineage_record(receipt) == lineage_record(verified)


def test_native_sdk_rechecks_source_attestation_expiry_before_cloud_access(reviewed):
    verified = verify_code_lineage(reviewed.pack, ROOT, **pin_unit_receipt(reviewed))
    plan = {"apply_blockers": [], **lineage_record(verified)}
    plan["execution_code_review_receipt"]["expires_at"] = "2020-01-01T00:00:00Z"
    receipt = {"jobs": {}, "plan_sha256": insurance_native.digest(plan)}
    with pytest.raises(ValueError, match="expired"):
        insurance_native.execute_sdk(None, None, plan, receipt, lambda: None)
