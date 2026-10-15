import copy
import hashlib
import importlib.util
import json
import shutil
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "orders_native_demo_evals", Path(__file__).parents[1] / "run-orders-native-demo-evals.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

REFERENCE = {
    "order_id": "NW-10001", "customer_hint": None, "intent": "ORDER_STATUS",
    "sentiment": "calm", "urgency": "P3", "problem_summary": "Customer asks for order status.",
}


@pytest.fixture
def workspace():
    # Keep test artifacts inside the project, not an OS temporary directory.
    path = Path(__file__).resolve().parents[2] / ".pytest_cache" / ("native-demo-" + uuid.uuid4().hex)
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


@pytest.fixture
def config(workspace):
    value = module.config_template()
    value["require_dataset_id_source"] = False
    value["remaining_eval_budget"].update(actual_spend_usd="0.10", verified=True, evidence="mock actual usage")
    value["model"].update(model_version="2024-08-06", sku="GlobalStandard")
    value["model"]["pricing"].update(
        input_usd_per_million="2.50", output_usd_per_million="10.00",
        source="SYNTHETIC TEST PRICES, not verified real pricing", verified_at="2026-10-06", verified=True,
    )
    value["judge"].update(semantic=True, max_input_tokens_per_call=2000, max_output_tokens_per_call=512,
                          bounds_verified=True, bounds_evidence="mock total call bounds")
    for role, spec in value["datasets"].items():
        rows = [{
            "query": "Where is NW-10001?",
            "ground_truth": json.dumps(REFERENCE) if role == "triage" else "Order NW-10001 is in transit.",
            "response": "PRECOMPUTED RESPONSE MUST NOT BE USED",
        } for _ in range(2)]
        path = workspace / f"{role}.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        spec.update(snapshot_path=str(path), snapshot_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    execution_paused=False,
                    uploaded_data_verified=True, verification_source="mock uploaded bytes read-back",
                    agent_source_sha256="a" * 64, agent_config_sha256="b" * 64)
        spec["bounds"].update(max_input_tokens_per_row=2000,
                              max_output_tokens_per_row=4096 if role == "triage" else 1000,
                              non_model_usd_per_row="0.001", verified=True, evidence="mock total limits")
    return value


def plan(config, workspace, roles=None):
    return module.build_plan(config, workspace, roles or list(module.TARGETS))


def test_compute_profile_keeps_total_authorization_and_covers_hosting(config, workspace):
    config.update(budget_profile="includes-hosted-compute", budget_usd="7.50",
                  require_dataset_id_source=True, max_rows=100)
    config["judge"].update(semantic=False, coherence=False)
    spec = config["datasets"]["triage"]
    path = Path(spec["snapshot_path"])
    row = json.loads(path.read_text().splitlines()[0])
    path.write_text("\n".join(json.dumps(row) for _ in range(100)) + "\n")
    spec["snapshot_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec["bounds"].update(max_input_tokens_per_row=2337, max_output_tokens_per_row=4096,
                          non_model_usd_per_row="0.025625")
    result = module.build_plan(config, workspace, ["triage"])
    assert result["budget"]["planned_upper_bound_usd"] == "7.242750"
    assert result["budget"]["selected_rows_total"] == 100
    assert sum(Decimal(v) for v in result["budget"]["reservations_usd"].values()) == Decimal("10")
    assert result["budget"]["reservations_usd"]["developer_training"] == "1.50"
    assert result["jobs"]["triage"]["bounds"]["non_model_usd_per_row"] == "0.025625"


def test_native_wrapped_schema_is_verified_without_weakening_mismatch_checks():
    expected = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    native = {"data_source_config": {
        "type": "custom", "item_schema": {}, "include_sample_schema": True,
        "schema": {"item": expected, "sample": {"type": "object"}},
    }}
    normalized = module.normalized_evaluation_definition(native)
    assert normalized["data_source_config"]["item_schema"] == expected
    assert native["data_source_config"]["item_schema"] == {}
    sdk = copy.deepcopy(native)
    sdk["data_source_config"]["schema_"] = sdk["data_source_config"].pop("schema")
    assert module.normalized_evaluation_definition(sdk)["data_source_config"]["item_schema"] == expected
    native["data_source_config"]["schema"]["item"] = {"type": "number"}
    wrong = module.normalized_evaluation_definition(native)
    assert not module.includes(wrong["data_source_config"]["item_schema"], expected)


def receipt(value):
    return {"plan": value, "plan_sha256": module.digest(value), "state": "dry_run", "jobs": {}}


def fake_clients(value):
    definitions = {}
    runs = {}
    project = SimpleNamespace(
        deployments=SimpleNamespace(get=Mock(return_value={
            "modelName": value["model"]["model_name"], "modelVersion": value["model"]["model_version"],
            "sku": {"name": value["model"]["sku"]},
        })),
        datasets=SimpleNamespace(get=Mock(side_effect=lambda name, version: {
            "id": module.DATA_PREFIX + name + "/versions/" + version, "type": "uri_file",
        })),
        agents=SimpleNamespace(get_version=Mock(side_effect=lambda name, version: {
            "name": name, "version": version, "status": "active",
            "definition": {"kind": "hosted", "environment_variables": {
                "ORDER_AGENT_ROLE": next(r for r, j in value["jobs"].items() if j["target"]["name"] == name),
                "MODEL_DEPLOYMENT_NAME": "gpt-4o",
            }},
            "metadata": {"source_sha256": "a" * 64, "config_sha256": "b" * 64},
        })),
    )
    project.beta = SimpleNamespace(evaluators=SimpleNamespace(
        create_version=Mock(return_value={"version": "1"}),
        get_version=Mock(return_value={"version": "1", "definition": module.grader_definition()["definition"]}),
        list_versions=Mock(return_value=[{"version": "1", "definition": module.grader_definition()["definition"]}]),
    ))

    def create_eval(**kwargs):
        result = {**kwargs, "id": f"eval-{len(definitions) + 1}"}
        definitions[result["id"]] = result
        return result

    def create_run(eval_id, **kwargs):
        result = {**kwargs, "eval_id": eval_id, "id": f"run-{len(runs) + 1}", "status": "queued",
                  "report_url": "https://ai.azure.com/mock-report"}
        runs[result["id"]] = result
        return result

    def retrieve_run(eval_id, run_id):
        result = runs[run_id]
        role = result["metadata"]["demo_role"]
        return {**result, "status": "completed", "result_counts": {
            "total": value["jobs"][role]["selected_rows"], "errored": 0,
        }}

    def output_items(eval_id, run_id, **kwargs):
        role = runs[run_id]["metadata"]["demo_role"]
        return {"data": [{
            "id": f"{run_id}-item-{n}", "eval_id": eval_id, "run_id": run_id,
            "sample": {"output": [{"role": "assistant", "content": json.dumps(REFERENCE)}]},
            "results": [{"name": c["name"], "score": 0.8}
                        for c in value["jobs"][role]["create"]["testing_criteria"]],
        } for n in range(value["jobs"][role]["selected_rows"])], "has_more": False}

    run_client = SimpleNamespace(
        create=Mock(side_effect=create_run), retrieve=Mock(side_effect=retrieve_run),
        list=Mock(side_effect=lambda eval_id, **kwargs: {
            "data": [r for r in runs.values() if r["eval_id"] == eval_id], "has_more": False,
        }),
        output_items=SimpleNamespace(list=Mock(side_effect=output_items)),
    )
    client = SimpleNamespace(evals=SimpleNamespace(
        create=Mock(side_effect=create_eval), runs=run_client,
        retrieve=Mock(side_effect=lambda eval_id: definitions[eval_id]),
        list=Mock(side_effect=lambda **kwargs: {"data": list(definitions.values()), "has_more": False}),
    ))
    return project, client, definitions, runs


def test_native_templates_use_actual_output_and_existing_datasets(config, workspace):
    value = plan(config, workspace)
    assert value["apply_blockers"] == []
    assert value["dataset_authorization"] == "demo-only"
    assert value["individually_human_reviewed"] is False
    assert value["budget"]["selected_rows_total"] == 6
    for role, job in value["jobs"].items():
        assert job["create"]["data_source_config"]["include_sample_schema"] is True
        assert job["run"]["data_source"]["source"] == {"type": "file_id", "id": job["dataset_id"]}
        assert job["run"]["data_source"]["type"] == "azure_ai_target_completions"
        assert job["target"] == {"type": "azure_ai_agent", "name": module.TARGETS[role][0],
                                  "version": module.TARGETS[role][1]}
        message = job["run"]["data_source"]["input_messages"]["template"][0]
        assert message == {"type": "message", "role": "user",
                           "content": {"type": "input_text", "text": "{{item.query}}"}}
        criteria = job["create"]["testing_criteria"]
        assert criteria[0]["data_mapping"]["response"] == "{{sample.output_text}}"
        scorer = next(c for c in criteria if c["type"] == "score_model")
        assert scorer["sampling_params"]["max_completions_tokens"] == 512
        assert scorer["model"] == "gpt-4o"
        assert "{{sample.output_text}}" in scorer["input"][1]["content"]
    assert "not individually human-reviewed" in value["metric_note"]


def test_initial_template_has_five_dollar_triad_and_paused_lookups():
    value = module.config_template()
    assert value["budget_usd"] == "5.00"
    assert value["max_rows"] == 100
    assert value["require_dataset_id_source"] is True
    assert value["judge"]["semantic"] is False
    assert value["judge"]["coherence"] is False
    assert value["datasets"]["triage"]["execution_paused"] is False
    assert value["datasets"]["orderlookup"]["execution_paused"] is True
    assert value["datasets"]["policylookup"]["execution_paused"] is True
    assert sum(Decimal(v) for v in module.RESERVATIONS.values()) == Decimal("10")


def test_cli_defaults_to_only_triage(config, workspace, capsys):
    path = workspace / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    output = workspace / "receipt.json"
    module.main(["--config", str(path), "--receipt", str(output)])
    journal = json.loads(output.read_text())
    assert list(journal["plan"]["jobs"]) == ["triage"]
    assert journal["plan"]["paused_roles"] == ["orderlookup", "policylookup"]
    assert json.loads(capsys.readouterr().out)["state"] == "dry_run"


def test_one_hundred_triage_rows_reserve_worst_case_output_not_average(config, workspace):
    config["judge"].update(semantic=False, coherence=False)
    config["require_dataset_id_source"] = True
    spec = config["datasets"]["triage"]
    row = {"query": "Where is NW-10001?", "ground_truth": json.dumps(REFERENCE)}
    path = Path(spec["snapshot_path"])
    path.write_text("\n".join(json.dumps(row) for _ in range(100)) + "\n", encoding="utf-8")
    spec["snapshot_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec["bounds"].update(max_input_tokens_per_row=1800, max_output_tokens_per_row=4096,
                          non_model_usd_per_row="0")
    value = plan(config, workspace, ["triage"])
    assert value["budget"]["planned_upper_bound_usd"] == "4.546000"
    assert value["budget"]["selected_rows_total"] == 100
    assert value["budget"]["reservations_usd"] == module.RESERVATIONS
    assert value["jobs"]["triage"]["run"]["data_source"]["source"]["type"] == "file_id"
    criteria = value["jobs"]["triage"]["create"]["testing_criteria"]
    assert {c["name"] for c in criteria} == {"diagnostic-token-f1"}
    assert value["custom_grader"] is None
    config["budget_usd"] = "3.00"
    with pytest.raises(ValueError, match="full existing dataset"):
        plan(config, workspace, ["triage"])
    config["budget_usd"] = "5.00"
    spec["bounds"]["max_input_tokens_per_row"] = None
    automatic = plan(config, workspace, ["triage"])
    bound = automatic["jobs"]["triage"]["bounds"]
    assert bound["max_input_tokens_per_row"] == \
        bound["prompt_utf8_bytes"] + bound["max_query_utf8_bytes"] + 1024
    assert automatic["budget"]["selected_rows_total"] == 100
    assert Decimal(automatic["budget"]["planned_upper_bound_usd"]) <= Decimal("5")
    assert automatic["custom_grader"] is None


def test_triage_underestimated_output_bound_is_rejected(config, workspace):
    config["datasets"]["triage"]["bounds"]["max_output_tokens_per_row"] = 1000
    with pytest.raises(ValueError, match="max_tokens=4096"):
        plan(config, workspace, ["triage"])


def test_lookup_pause_and_actual_spend_evidence_block_all_cloud_calls(config, workspace):
    config["datasets"]["orderlookup"]["execution_paused"] = True
    config["remaining_eval_budget"]["verified"] = False
    value = plan(config, workspace, ["orderlookup"])
    assert any("execution is paused" in b for b in value["apply_blockers"])
    assert any("verified actual baseline spend" in b for b in value["apply_blockers"])
    project, client, _, _ = fake_clients(value)
    with pytest.raises(ValueError, match="Apply blocked"):
        module.execute(project, client, receipt(value), lambda: None)
    project.deployments.get.assert_not_called()
    client.evals.runs.create.assert_not_called()


def test_lookup_batch_uses_only_remaining_five_dollar_reservation(config, workspace):
    config["remaining_eval_budget"]["actual_spend_usd"] = "4.96"
    value = plan(config, workspace, ["orderlookup"])
    assert value["budget"]["eval_reservation_usd"] == "0.04"
    assert value["jobs"]["orderlookup"]["selected_rows"] == 1
    assert Decimal(value["budget"]["baseline_total_upper_bound_usd"]) <= Decimal("5")
    config["remaining_eval_budget"]["actual_spend_usd"] = "5.00"
    with pytest.raises(ValueError, match="No remaining"):
        plan(config, workspace, ["orderlookup"])


def test_bom_normalization_does_not_bypass_uploaded_byte_hash(config, workspace):
    spec = config["datasets"]["triage"]
    original = plan(config, workspace, ["triage"])
    path = Path(spec["snapshot_path"])
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    with pytest.raises(ValueError, match="snapshot hash"):
        plan(config, workspace, ["triage"])
    # After the owner explicitly supplies the verified downloaded byte hash,
    # BOM-insensitive parsing preserves the logical evaluation rows.
    spec["snapshot_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    normalized = plan(config, workspace, ["triage"])
    assert normalized["jobs"]["triage"]["selected_rows_sha256"] == \
        original["jobs"]["triage"]["selected_rows_sha256"]
    assert normalized["jobs"]["triage"]["snapshot_sha256"] != original["jobs"]["triage"]["snapshot_sha256"]


@pytest.mark.parametrize("change", [
    lambda c: c.update(tenant="wrong-tenant"),
    lambda c: c.update(project_endpoint="https://example.com/projects/northwind-orders"),
    lambda c: c.update(dataset_authorization="human-reviewed"),
    lambda c: c.update(budget_usd="5.01"),
    lambda c: c.update(max_rows=101),
    lambda c: c.update(max_rows=True),
    lambda c: c["model"].update(deployment="auto-deploy-new-judge"),
    lambda c: c["model"]["pricing"].update(input_usd_per_million="NaN"),
    lambda c: c["datasets"].update(manager={}),
    lambda c: c["datasets"]["triage"].update(
        dataset_id=module.DATA_PREFIX + "06-resolution-evaluation/versions/1"),
    lambda c: c["datasets"]["triage"].update(snapshot_sha256="wrong"),
])
def test_rejects_unapproved_scope_and_invalid_budget(config, workspace, change):
    change(config)
    with pytest.raises(ValueError):
        plan(config, workspace)


def test_unverified_demo_authorization_is_not_forged_review(config, workspace):
    config["model"]["pricing"]["verified"] = False
    config["datasets"]["triage"]["bounds"]["verified"] = False
    value = plan(config, workspace)
    assert len(value["apply_blockers"]) == 2
    project, client, _, _ = fake_clients(value)
    with pytest.raises(ValueError, match="Apply blocked"):
        module.execute(project, client, receipt(value), lambda: None)
    project.deployments.get.assert_not_called()
    client.evals.create.assert_not_called()


def test_global_budget_subset_preview_keeps_lineage_but_cannot_copy_or_launch(config, workspace):
    config["budget_usd"] = "0.12"
    value = plan(config, workspace)
    assert Decimal(value["budget"]["planned_upper_bound_usd"]) <= Decimal("0.12")
    assert value["budget"]["selected_rows_total"] == 3
    assert value["budget"]["launchable"] is False
    assert "file_content" not in json.dumps(value)
    assert "PRECOMPUTED RESPONSE" not in json.dumps(value)
    for job in value["jobs"].values():
        assert job["run"] is None
        assert job["launchable"] is False
        assert job["dataset_id"].endswith("/versions/1")
        assert job["source_mode"] == "blocked-subset-no-data-copies"
        assert job["dataset_id_support"] == "not-tested-blocked-subset"


def test_dataset_id_probe_does_not_silently_fall_back_to_inline(config, workspace):
    config.update(require_dataset_id_source=True, max_rows=1)
    with pytest.raises(ValueError, match="inline sampling cannot verify"):
        plan(config, workspace, ["triage"])
    config["max_rows"] = 2
    value = plan(config, workspace, ["triage"])
    assert value["jobs"]["triage"]["run"]["data_source"]["source"]["type"] == "file_id"
    assert value["jobs"]["triage"]["dataset_id_support"] == "unverified-until-native-readback"


def test_row_cap_is_total_not_per_role(config, workspace):
    config["max_rows"] = 4
    value = plan(config, workspace)
    assert value["budget"]["selected_rows_total"] == 4
    assert [j["selected_rows"] for j in value["jobs"].values()] == [2, 1, 1]


def test_judge_retry_and_coherence_costs_are_reserved(config, workspace):
    baseline = plan(config, workspace)
    config["judge"].update(coherence=True, max_calls_per_evaluator_per_row=2)
    increased = plan(config, workspace)
    assert Decimal(increased["budget"]["planned_upper_bound_usd"]) > \
        Decimal(baseline["budget"]["planned_upper_bound_usd"])
    criterion = increased["jobs"]["triage"]["create"]["testing_criteria"][-1]
    assert criterion["evaluator_name"] == "builtin.coherence"
    assert criterion["data_mapping"]["response"] == "{{sample.output_text}}"


def test_budget_that_cannot_cover_all_roles_fails_before_jobs(config, workspace):
    config["budget_usd"] = "0.001"
    with pytest.raises(ValueError, match="one row"):
        plan(config, workspace)


def test_code_grader_measures_json_labels_not_f1():
    scope = {}
    exec(module.TRIAGE_CODE, scope)
    grade = scope["grade"]
    expected = json.dumps(REFERENCE)
    actual = {**REFERENCE, "problem_summary": "Different correct wording."}
    assert grade({}, {"sample": {"output_text": json.dumps(actual, indent=2)}, "ground_truth": expected}) == 1
    assert grade({}, {"response": json.dumps(actual), "ground_truth": expected}) == 1
    assert grade({}, {"ground_truth": expected}) == 0
    for changed in ({**actual, "urgency": "P1"}, {**actual, "order_id": "NW-00000"},
                    {**actual, "intent": "OTHER"}, {**actual, "extra": True},
                    {**actual, "customer_hint": False}, {**actual, "problem_summary": ""}):
        assert grade({}, {"sample": {"output_text": json.dumps(changed)}, "ground_truth": expected}) == 0
    assert grade({}, {"sample": {"output_text": "prose " + expected}, "ground_truth": expected}) == 0
    assert grade({}, {"response": expected[:-1] + ', "urgency": "P3"}', "ground_truth": expected}) == 0
    # The actual nested target output takes precedence over a misleading mapped field.
    assert grade({}, {"sample": {"output_text": "{}"}, "response": expected, "ground_truth": expected}) == 0


def test_offline_dry_run_is_persistent_and_never_imports_azure(config, workspace, monkeypatch):
    config_path = workspace / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    receipt_path = workspace / "receipt.json"
    import builtins
    original = builtins.__import__

    def deny_cloud(name, *args, **kwargs):
        if name.startswith(("azure", "openai")):
            raise AssertionError("Dry run tried to import a cloud SDK")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_cloud)
    first = module.run(config_path, receipt_path, list(module.TARGETS))
    assert first["state"] == "dry_run"
    assert first["inference_started"] is False
    assert first["jobs"] == {}
    assert module.run(config_path, receipt_path, list(module.TARGETS)) == first
    assert json.loads(receipt_path.read_text()) == first
    config["max_rows"] = 4
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        module.run(config_path, receipt_path, list(module.TARGETS))


def test_native_success_requires_completed_readback_and_exports_actual_outputs(config, workspace):
    value = plan(config, workspace)
    journal = receipt(value)
    project, client, _, _ = fake_clients(value)
    snapshots = []
    module.execute(project, client, journal, lambda: snapshots.append(copy.deepcopy(journal)))
    assert journal["state"] == "completed"
    assert "grader" not in journal
    project.beta.evaluators.create_version.assert_not_called()
    assert client.evals.runs.create.call_count == 3
    assert all(j["readback_verified"] for j in journal["jobs"].values())
    assert all(j["native_dataset_id_support_verified"] for j in journal["jobs"].values())
    assert any(j.get("state") == "submitted" and not j["readback_verified"]
               for saved in snapshots for j in saved["jobs"].values())
    assert all(j["output_items"][0]["sample"]["output"][0]["content"] == json.dumps(REFERENCE)
               for j in journal["jobs"].values())
    criteria = client.evals.create.call_args_list[0].kwargs["testing_criteria"]
    assert not any(c.get("evaluator_name") == module.GRADER_NAME for c in criteria)


@pytest.mark.parametrize("mismatch", ["version", "role", "model", "source", "dataset", "sku"])
def test_preflight_read_only_mismatches_block_all_posts(config, workspace, mismatch):
    value = plan(config, workspace)
    project, client, _, _ = fake_clients(value)
    agent = project.agents.get_version.side_effect("northwind-triage", "2")
    if mismatch == "dataset":
        project.datasets.get.side_effect = None
        project.datasets.get.return_value = {"id": "different", "type": "uri_file"}
    elif mismatch == "sku":
        project.deployments.get.return_value["sku"]["name"] = "WrongSKU"
    else:
        if mismatch == "version":
            agent["version"] = "3"
        elif mismatch == "role":
            agent["definition"]["environment_variables"]["ORDER_AGENT_ROLE"] = "manager"
        elif mismatch == "model":
            agent["definition"]["environment_variables"]["MODEL_DEPLOYMENT_NAME"] = "new-model"
        else:
            agent["metadata"]["source_sha256"] = "c" * 64
        project.agents.get_version.side_effect = None
        project.agents.get_version.return_value = agent
    with pytest.raises(ValueError):
        module.execute(project, client, receipt(value), lambda: None)
    project.beta.evaluators.create_version.assert_not_called()
    client.evals.create.assert_not_called()
    client.evals.runs.create.assert_not_called()


@pytest.mark.parametrize("registration", [
    {"type": "uri_folder"},
    {"kind": "URI_FILE", "isSingleFile": False, "containerdataURI": "https://unused.example/SECRET-SAS"},
    {"properties": {"kind": "URI_FILE", "isSingleFile": False,
                    "containerdataURI": "https://unused.example/SECRET-SAS"}},
])
def test_legacy_uri_folder_is_recognized_without_logging_credentials(config, workspace, registration):
    value = plan(config, workspace, ["triage"])
    project, client, _, _ = fake_clients(value)
    project.datasets.get.side_effect = lambda name, version: {
        "id": module.DATA_PREFIX + name + "/versions/" + version, **registration,
        "blobReference": {"blobUri": "SECRET-SAS", "credential": {"sasToken": "SECRET-TOKEN"}},
        "blobReferenceForConsumption": {"blobUri": "SECRET-SAS", "credential": "SECRET-TOKEN"},
    }
    checked = module.preflight(project, value, time.monotonic() + 10)
    entry = checked["datasets_and_targets"]["triage"]
    assert entry["legacy_folder_registration"] is True
    assert entry["dataset_id_support"] == "unverified-until-native-readback"
    assert "SECRET" not in json.dumps(checked)
    client.evals.runs.create.assert_not_called()
    journal = receipt(value)
    module.execute(project, client, journal, lambda: None)
    assert journal["jobs"]["triage"]["native_dataset_id_support_verified"] is True
    assert journal["preflight"]["datasets_and_targets"]["triage"]["dataset_id_support"] == \
        "verified-by-completed-native-run"
    assert "SECRET" not in json.dumps(journal)


def test_subset_copy_is_blocked_before_project_reads_and_job_writes(config, workspace):
    config["max_rows"] = 3
    value = plan(config, workspace)
    project, client, _, _ = fake_clients(value)
    journal = receipt(value)
    with pytest.raises(ValueError, match="copies are prohibited"):
        module.execute(project, client, journal, lambda: None)
    assert journal["state"] == "dry_run"
    project.deployments.get.assert_not_called()
    client.evals.runs.create.assert_not_called()
    # Clearing a diagnostic blocker cannot enable an inline/local-data path.
    value["apply_blockers"] = []
    with pytest.raises(ValueError, match="Dataset copies are prohibited"):
        module.execute(project, client, journal, lambda: None)
    project.deployments.get.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "canceled", "cancelled"])
def test_failed_run_does_not_submit_remaining_jobs_or_claim_completed(config, workspace, status):
    value = plan(config, workspace)
    project, client, _, runs = fake_clients(value)
    client.evals.runs.retrieve.side_effect = lambda eval_id, run_id: {**runs[run_id], "status": status}
    journal = receipt(value)
    module.execute(project, client, journal, lambda: None)
    assert journal["state"] == "incomplete"
    assert journal["jobs"]["triage"]["state"] == status
    assert client.evals.runs.create.call_count == 1


def test_incomplete_results_are_not_complete(config, workspace):
    value = plan(config, workspace)
    project, client, _, _ = fake_clients(value)
    client.evals.runs.output_items.list.side_effect = None
    client.evals.runs.output_items.list.return_value = {"data": [], "has_more": False}
    journal = receipt(value)
    module.execute(project, client, journal, lambda: None)
    assert journal["state"] == "incomplete"
    assert journal["jobs"]["triage"]["state"] == "results_incomplete"
    assert journal["jobs"]["triage"]["readback_verified"] is False
    assert client.evals.runs.create.call_count == 1


def test_missing_requested_grader_results_are_not_complete(config, workspace):
    value = plan(config, workspace)
    project, client, _, _ = fake_clients(value)
    original = client.evals.runs.output_items.list.side_effect

    def missing_grader(**kwargs):
        page = original(**kwargs)
        page["data"][0]["results"].pop()
        return page

    client.evals.runs.output_items.list.side_effect = missing_grader
    journal = receipt(value)
    module.execute(project, client, journal, lambda: None)
    assert journal["jobs"]["triage"]["state"] == "results_incomplete"
    assert journal["state"] == "incomplete"


def test_timeout_is_bounded_and_submitted_is_not_completed(config, workspace, monkeypatch):
    value = plan(config, workspace)
    project, client, _, runs = fake_clients(value)
    client.evals.runs.retrieve.side_effect = lambda eval_id, run_id: runs[run_id]
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += seconds

    def clock():
        return elapsed[0]
    original_read = module.read_call
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock, sleep=sleep))
    monkeypatch.setattr(module, "read_call", lambda function, **kwargs:
                        original_read(function, clock=clock, sleep=sleep, **kwargs))
    journal = receipt(value)
    module.execute(project, client, journal, lambda: None, wait_seconds=2, poll_seconds=1)
    assert elapsed[0] == 2
    assert journal["state"] == "incomplete"
    assert journal["jobs"]["triage"]["state"] == "wait_timeout"
    assert journal["jobs"]["triage"]["readback_verified"] is False
    assert client.evals.runs.create.call_count == 1


def test_uncertain_run_post_is_reconciled_without_duplicate_submission(config, workspace):
    value = plan(config, workspace, ["triage"])
    project, client, _, runs = fake_clients(value)
    original = client.evals.runs.create.side_effect

    def lost_response(**kwargs):
        original(**kwargs)
        raise ConnectionError("Response lost after accepted POST")

    client.evals.runs.create.side_effect = lost_response
    journal = receipt(value)
    saved = []
    with pytest.raises(ConnectionError):
        module.execute(project, client, journal, lambda: saved.append(copy.deepcopy(journal)))
    assert journal["jobs"]["triage"]["run_submission_started"] is True
    assert "run_id" not in journal["jobs"]["triage"]
    module.execute(project, client, journal, lambda: saved.append(copy.deepcopy(journal)))
    assert journal["state"] == "completed"
    assert len(runs) == 1
    assert client.evals.runs.create.call_count == 1
    assert all(s.get("inference_started") is True for s in saved
               if s["jobs"].get("triage", {}).get("run_submission_started"))


def test_uncertain_run_without_match_fails_closed(config, workspace):
    value = plan(config, workspace, ["triage"])
    project, client, definitions, _ = fake_clients(value)
    journal = receipt(value)
    identity = {"demo_plan_sha256": journal["plan_sha256"], "demo_role": "triage",
                "dataset_authorization": "demo-only"}
    definition = copy.deepcopy(value["jobs"]["triage"]["create"])
    for criterion in definition["testing_criteria"]:
        if criterion.get("evaluator_name") == module.GRADER_NAME:
            criterion["evaluator_version"] = "1"
    definitions["eval-existing"] = {**definition, "id": "eval-existing", "metadata": identity}
    journal["jobs"]["triage"] = {"eval_id": "eval-existing", "run_submission_started": True}
    with pytest.raises(RuntimeError, match="Uncertain prior POST"):
        module.execute(project, client, journal, lambda: None)
    client.evals.runs.create.assert_not_called()


def test_uncertain_eval_creation_is_reconciled_read_only(config, workspace):
    value = plan(config, workspace, ["triage"])
    project, client, _, _ = fake_clients(value)
    original = client.evals.create.side_effect

    def lost_response(**kwargs):
        original(**kwargs)
        raise ConnectionError("Accepted eval POST without client response")

    client.evals.create.side_effect = lost_response
    journal = receipt(value)
    with pytest.raises(ConnectionError):
        module.execute(project, client, journal, lambda: None)
    module.execute(project, client, journal, lambda: None)
    assert journal["state"] == "completed"
    assert client.evals.create.call_count == 1


def test_atomic_receipt_and_exclusive_lock_preserve_prior_artifacts(workspace):
    path = workspace / "receipt.json"
    module.atomic_json(path, {"state": "dry_run"})
    module.atomic_json(path, {"state": "submitted", "run_id": "run-1"})
    assert json.loads(path.read_text())["run_id"] == "run-1"
    with module.receipt_lock(path):
        with pytest.raises(FileExistsError):
            with module.receipt_lock(path):
                pytest.fail("Concurrent apply acquired the receipt lock")
    pending = path.with_name(path.name + ".pending")
    pending.write_text("unreconciled prior crash")
    with pytest.raises(FileExistsError):
        module.atomic_json(path, {"state": "should-not-overwrite"})
    assert pending.read_text() == "unreconciled prior crash"
    assert json.loads(path.read_text())["run_id"] == "run-1"


def test_bounded_rate_limit_waits_retry_reads_only():
    class RateLimit(Exception):
        status_code = 429
        response = SimpleNamespace(headers={"retry-after": "3"})

    elapsed = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    call = Mock(side_effect=[RateLimit(), RateLimit(), {"status": "completed"}])
    assert module.read_call(call, deadline=10, clock=lambda: elapsed[0], sleep=sleep)["status"] == "completed"
    assert sleeps == [3, 3]
    call = Mock(side_effect=RateLimit())
    with pytest.raises(TimeoutError):
        module.read_call(call, deadline=elapsed[0] + 2, clock=lambda: elapsed[0], sleep=sleep)


def test_pagination_readback_is_bounded_and_explicit():
    method = Mock(side_effect=[
        {"data": [{"id": "a"}], "has_more": True},
        {"data": [{"id": "b"}], "has_more": False},
    ])
    assert module.pages(method, deadline=time.monotonic() + 10) == [{"id": "a"}, {"id": "b"}]
    assert method.call_args_list[1].kwargs["after"] == "a"
    with pytest.raises(ValueError, match="bounded"):
        module.pages(Mock(return_value={"data": [{"id": "a"}, {"id": "b"}], "has_more": False}),
                     deadline=time.monotonic() + 10, maximum=1)


def test_sdk_serializes_native_contract_without_cloud_requests():
    from azure.ai.projects.models import CodeBasedEvaluatorDefinition, EvaluatorVersion
    from openai.types.eval_create_params import TestingCriterionScoreModel
    from openai.types.graders.score_model_grader_param import SamplingParams

    definition = module.grader_definition()
    sdk_definition = CodeBasedEvaluatorDefinition(**{
        k: v for k, v in definition["definition"].items() if k != "type"
    })
    assert sdk_definition.as_dict()["code_text"] == module.TRIAGE_CODE
    sdk_version = EvaluatorVersion(definition)
    assert sdk_version.as_dict()["definition"]["metrics"]["result"]["type"] == "continuous"
    assert "pass_threshold" in TestingCriterionScoreModel.__annotations__
    assert "max_completions_tokens" in SamplingParams.__annotations__


def test_apply_uses_exact_cli_tenant_zero_write_retries_and_existing_project(config, workspace, monkeypatch):
    config_path = workspace / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    value = plan(config, workspace)
    project, client, _, _ = fake_clients(value)
    credential_cm = Mock()
    credential_cm.__enter__ = Mock(return_value="CLI-CREDENTIAL")
    credential_cm.__exit__ = Mock(return_value=False)
    project_cm = Mock()
    project_cm.__enter__ = Mock(return_value=project)
    project_cm.__exit__ = Mock(return_value=False)
    client_cm = Mock()
    client_cm.__enter__ = Mock(return_value=client)
    client_cm.__exit__ = Mock(return_value=False)
    project.get_openai_client = Mock(return_value=client_cm)
    credential_factory = Mock(return_value=credential_cm)
    project_factory = Mock(return_value=project_cm)
    monkeypatch.setitem(sys.modules, "azure.identity", SimpleNamespace(AzureCliCredential=credential_factory))
    monkeypatch.setitem(sys.modules, "azure.ai.projects", SimpleNamespace(AIProjectClient=project_factory))
    result = module.run(config_path, workspace / "receipt.json", list(module.TARGETS), apply=True)
    assert result["state"] == "completed"
    credential_factory.assert_called_once_with(tenant_id=module.TENANT, process_timeout=60)
    assert project_factory.call_args.kwargs["endpoint"] == module.ENDPOINT
    assert project_factory.call_args.kwargs["retry_total"] == 0
    project.get_openai_client.assert_called_once_with(max_retries=0, timeout=30)
    second = module.run(config_path, workspace / "receipt.json", list(module.TARGETS), apply=True)
    assert second == result
    assert credential_factory.call_count == 1
    assert client.evals.runs.create.call_count == 3
