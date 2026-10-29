from __future__ import annotations

from types import SimpleNamespace

import pytest

from evals.publish_foundry_evidence import (
    ensure_evaluation_definition,
    prepare_rows,
    run_group,
)


BUILD_SHA = "1" * 40
DATASET_SHA = "a" * 64


def _report(rows: list[dict], **overrides) -> dict:
    report = {
        "target": "orchestrator",
        "evaluation_run_id": "run-123",
        "expected_build": BUILD_SHA,
        "dataset_sha256": DATASET_SHA,
        "row_count": len(rows),
        "rows": rows,
    }
    report.update(overrides)
    return report


def _row(**overrides) -> dict:
    row = {
        "id": "golden-1",
        "workflow": "policy_wording",
        "query": "Explain the synthetic policy provision.",
        "response": "The wording states ...",
        "tools_called": ["policy_wording"],
        "tool_call_count": 1,
        "expected": {"must_cite": "synthetic policy wording"},
        "evidence_scope": "API execution and specialist outputs; internal specialist traces unverified.",
    }
    row.update(overrides)
    return row


def test_prepares_real_execution_rows_with_lineage():
    rows = prepare_rows(_report([_row()]), dataset_sha256=DATASET_SHA, max_rows=1)

    assert rows[0]["query"] == "Explain the synthetic policy provision."
    assert rows[0]["response"] == "The wording states ..."
    assert rows[0]["source_run_id"] == "run-123"
    assert rows[0]["source_build"] == BUILD_SHA
    assert '"policy_wording"' in rows[0]["execution_evidence"]
    assert "synthetic policy wording" in rows[0]["expected_behavior"]


@pytest.mark.parametrize(
    ("report", "dataset_sha", "limit", "message"),
    [
        (_report([_row()], target="copilot-sdk"), DATASET_SHA, 1, "only captured orchestrator"),
        (_report([_row()]), "b" * 64, 1, "synthetic golden dataset"),
        (_report([_row()], expected_build="short"), DATASET_SHA, 1, "40-character"),
        (_report([_row(response="")]), DATASET_SHA, 1, "no captured agent response"),
        (_report([_row(evidence_scope="model-only")]), DATASET_SHA, 1, "not verified API-execution"),
        (_report([_row(error="API unavailable")]), DATASET_SHA, 1, "incomplete"),
        (_report([_row()]), DATASET_SHA, 0, "max-rows"),
    ],
)
def test_rejects_unverified_or_unbounded_sources(report, dataset_sha, limit, message):
    with pytest.raises(ValueError, match=message):
        prepare_rows(report, dataset_sha256=dataset_sha, max_rows=limit)


def test_safety_workflow_is_judged_with_orchestrator_rubric():
    rows = prepare_rows(
        _report([_row(workflow="safety")]),
        dataset_sha256=DATASET_SHA,
        max_rows=1,
    )
    assert rows[0]["workflow"] == "safety"


def test_foundry_run_registers_rubric_and_downloads_per_item_results():
    created_versions = []
    eval_calls = []
    run_calls = []

    catalog = SimpleNamespace(
        list=lambda **_: [],
        create_version=lambda **kwargs: created_versions.append(kwargs),
    )
    run = SimpleNamespace(id="run-1", status="completed", report_url="https://foundry.example/run/1")
    runs = SimpleNamespace(
        create=lambda **kwargs: run_calls.append(kwargs) or run,
        retrieve=lambda **_: run,
        output_items=SimpleNamespace(
            list=lambda **kwargs: [SimpleNamespace(model_dump=lambda **_: {"item": 1})]
        ),
    )
    client = SimpleNamespace(
        evals=SimpleNamespace(
            create=lambda **kwargs: eval_calls.append(kwargs) or SimpleNamespace(id="eval-1"),
            runs=runs,
        )
    )
    project = SimpleNamespace(
        beta=SimpleNamespace(evaluators=catalog),
        get_openai_client=lambda: client,
    )

    result = run_group(
        project,
        [{"id": "golden-1", "query": "q", "response": "a", "workflow": "triage"}],
        agent="triage",
        judge_deployment="approved-judge",
        source_run_id="run-123",
    )

    assert result["status"] == "completed"
    assert result["report_url"] == "https://foundry.example/run/1"
    assert result["output_items"] == [{"item": 1}]
    assert created_versions[0]["name"] == result["evaluator"]
    assert eval_calls[0]["testing_criteria"][0]["initialization_parameters"] == {
        "deployment_name": "approved-judge"
    }
    assert run_calls[0]["data_source"]["source"]["content"][0]["item"]["id"] == "golden-1"


def test_evaluation_definition_is_created_once_and_then_reused():
    definitions = []

    catalog = SimpleNamespace(list=lambda **_: [], create_version=lambda **_: None)
    evaluation = SimpleNamespace(id="eval-config-1", name="northwind-triage-rubric-0a7fcf0b5198-gpt-4o")
    evals = SimpleNamespace(
        list=lambda **_: list(definitions),
        create=lambda **kwargs: definitions.append(
            SimpleNamespace(id="eval-config-1", name=kwargs["name"])
        ) or evaluation,
    )
    project = SimpleNamespace(
        beta=SimpleNamespace(evaluators=catalog),
        get_openai_client=lambda: SimpleNamespace(evals=evals),
    )

    first = ensure_evaluation_definition(project, agent="triage", judge_deployment="gpt-4o")
    second = ensure_evaluation_definition(project, agent="triage", judge_deployment="gpt-4o")

    assert first["created"] is True
    assert second["created"] is False
    assert first["eval_id"] == second["eval_id"] == "eval-config-1"
