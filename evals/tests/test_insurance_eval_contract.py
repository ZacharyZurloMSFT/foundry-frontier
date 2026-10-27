import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from evals import run_evals
from evals.evaluators.claim_action_completeness import evaluate, evaluate_draft
from evals.evaluators.rubric_evaluator import judge_model_config_from_env

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "data" / "northwind" / "insurance-fixtures.json").read_text(encoding="utf-8"))
DATASET = [json.loads(line) for line in (ROOT / "data" / "golden" / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
           if line.strip()]

DRAFT = {"claim_id": "CLM-2001", "policy_id": "POL-1001", "policyholder_id": "PH-1001",
         "action_type": "request_evidence", "summary": "Request damage photos, a repair estimate and an incident statement.",
         "policy_refs": ["NW-AUTO-WA-2026-v1#duties-after-loss"]}
SETTLEMENT = {**DRAFT, "action_type": "settlement_recommendation", "amount_usd": 1200,
              "policy_refs": ["NW-AUTO-WA-2026-v1#collision"]}


def test_judge_requires_configuration_and_rejects_known_incompatible_models(monkeypatch):
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://unit-test.invalid")
    for key in ("FOUNDRY_JUDGE_DEPLOYMENT", "FOUNDRY_PROMPT_AGENT_DEPLOYMENT", "FOUNDRY_WORKER_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)
    assert judge_model_config_from_env() is None
    monkeypatch.setenv("FOUNDRY_WORKER_DEPLOYMENT", "gpt-5-mini")
    assert judge_model_config_from_env() is None
    monkeypatch.setenv("FOUNDRY_JUDGE_DEPLOYMENT", "gpt-4o")
    assert judge_model_config_from_env()["azure_deployment"] == "gpt-4o"


def test_complete_drafts_score_one_and_are_plausible():
    for draft in (DRAFT, SETTLEMENT, {**DRAFT, "policy_refs": []}):
        result = evaluate(response=json.dumps(draft))
        assert result["claim_action_completeness"] == 1
        assert result["plausible"] and all(result["fields_present"].values())


@pytest.mark.parametrize("mutation", [
    {"amount_usd": True}, {"amount_usd": 0}, {"amount_usd": float("nan")},
    {"action_type": "settlement_recommendation", "amount_usd": 1200, "policy_refs": []},
    {"action_type": "settlement_recommendation", "amount_usd": 40000, "policy_refs": ["NW-AUTO-WA-2026-v1#collision"]},
    {"action_type": "settlement_recommendation", "amount_usd": 5000, "policy_refs": ["NW-AUTO-WA-2026-v1#rental-reimbursement"]},
    {"amount_usd": 10},
    {"action_type": "approve_claim"}, {"action_type": "execute_payment"},
    {"policy_refs": ["NW-AUTO-CO-2026-v1#glass"]}, {"policy_refs": ["NW-CLAIMS-HANDLING-2026-V1"]},
    {"policy_refs": ["NW-AUTO-WA-2026-v1#collision", "NW-AUTO-WA-2026-v1#collision"]},
    {"policy_id": "POL-1002"}, {"policyholder_id": "PH-1002"}, {"claim_id": "CLM-9999"},
    {"claim_id": "2001"}, {"summary": "  "}, {"summary": "Your claim is approved and paid."},
    {"approved": True}, {"payment_status": "sent"},
])
def test_incomplete_implausible_or_binding_drafts_never_score_full(mutation):
    result = evaluate(response=json.dumps({**DRAFT, **mutation}))
    assert result["claim_action_completeness"] != 1 or not result["plausible"]


def test_prose_blank_and_missing_fields_fail():
    for response in ("", "I created a perfect draft.", "{}", json.dumps({"claim_id": "CLM-2001"})):
        result = evaluate(response=response)
        assert result["claim_action_completeness"] < 1 and not result["plausible"]


def test_expected_draft_is_compared_only_when_supplied():
    assert "expected_match" not in evaluate(response=json.dumps(DRAFT))
    assert evaluate(response=json.dumps(DRAFT), expected={"action_type": "request_evidence", "amount_usd": None})["expected_match"] == 1.0
    assert evaluate(response=json.dumps(DRAFT), expected={"action_type": "escalate_review"})["expected_match"] == 0.0
    assert evaluate(response=json.dumps(DRAFT), expected={"action_type_in": ["request_evidence", "customer_update"]})["expected_match"] == 1.0
    assert evaluate(response=json.dumps(SETTLEMENT), expected={"amount_usd": 1500})["expected_match"] == 0.0
    assert evaluate(response=json.dumps(SETTLEMENT), expected={"must_cite_policy_refs": ["NW-AUTO-WA-2026-v1#collision"]})["expected_match"] == 1.0
    assert evaluate(response=json.dumps(DRAFT), expected={"must_cite_policy_refs": ["NW-AUTO-WA-2026-v1#collision"]})["expected_match"] == 0.0
    assert evaluate(response=json.dumps(SETTLEMENT), expected={"policy_refs": ["NW-AUTO-WA-2026-v1#collision"]})["expected_match"] == 1.0


def test_every_golden_draft_row_is_satisfiable_by_a_canonical_draft():
    rows = [r for r in DATASET if r["workflow"] == "claim_action_draft"]
    assert rows
    for row in rows:
        expected = row["expected"]
        draft = {key: expected[key] for key in ("claim_id", "policy_id", "policyholder_id", "action_type")}
        draft["summary"] = "Synthetic draft for human review."
        draft["policy_refs"] = list(expected.get("must_cite_policy_refs", []))
        if expected.get("amount_usd") is not None:
            draft["amount_usd"] = expected["amount_usd"]
        result = evaluate_draft(draft, expected)
        assert result["claim_action_completeness"] == 1 and result["plausible"], row["id"]
        assert result["expected_match"] == 1.0, row["id"]


class FakeHTTP:
    """Stands in for httpx.AsyncClient: one streamed /insurance/chat turn plus a /hitl read."""

    def __init__(self, events, items=None, expected_workflow=None):
        self.events, self.items, self.expected_workflow = events, items or [], expected_workflow
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    @asynccontextmanager
    async def stream(self, method, url, **_kwargs):
        self.posted.append((method, url))
        yield self

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for event in self.events:
            yield json.dumps(event)

    async def get(self, url, *, params):
        assert url.endswith("/hitl")
        assert params == {"workflow_id": self.expected_workflow}
        return self

    def json(self):
        return {"items": self.items}


def saved(workflow, payload=DRAFT, **extra):
    return {"id": "hitl-1", "workflowId": workflow, "status": "pending", "kind": "insurance_claim_action",
            "payload": payload, **extra}


@pytest.mark.parametrize("same_workflow", [True, False])
def test_orchestrator_grades_only_session_bound_saved_draft(monkeypatch, same_workflow):
    events = [{"kind": "session", "session_id": "candidate", "business_domain": "insurance"},
              {"kind": "assistant_message", "text": "I created a perfect draft."},
              {"kind": "done", "workflow_id": "wf-server-authoritative"}]
    fake = FakeHTTP(events, [saved("wf-server-authoritative" if same_workflow else "wf-other")], "wf-server-authoritative")
    monkeypatch.setattr(run_evals.httpx, "AsyncClient", lambda **_kwargs: fake)
    row = {"workflow": "claim_action_draft", "input": "test"}
    if same_workflow:
        assert json.loads(asyncio.run(run_evals.run_workflow(row))) == DRAFT
        assert fake.posted == [("POST", f"{run_evals.API_BASE}/insurance/chat")]
    else:
        with pytest.raises(RuntimeError, match="bound to this session"):
            asyncio.run(run_evals.run_workflow(row))


def test_draft_workflow_falls_back_to_the_insurance_workflow_id_for_the_session(monkeypatch):
    events = [{"kind": "session", "session_id": "candidate"}, {"kind": "assistant_message", "text": "Saved."}]
    fake = FakeHTTP(events, [saved("wf-insurance-candidate")], "wf-insurance-candidate")
    monkeypatch.setattr(run_evals.httpx, "AsyncClient", lambda **_kwargs: fake)
    assert json.loads(asyncio.run(run_evals.run_workflow({"workflow": "claim_action_draft", "input": "test"}))) == DRAFT


@pytest.mark.parametrize("item", [
    saved("wf-insurance-candidate", status="approved"),
    saved("wf-insurance-candidate", kind="resolution"),
    saved("wf-insurance-candidate", payload="not-an-object"),
])
def test_draft_must_be_a_pending_unexecuted_claim_action_payload(monkeypatch, item):
    events = [{"kind": "session", "session_id": "candidate"}, {"kind": "assistant_message", "text": "Saved."}]
    fake = FakeHTTP(events, [item], "wf-insurance-candidate")
    monkeypatch.setattr(run_evals.httpx, "AsyncClient", lambda **_kwargs: fake)
    with pytest.raises(RuntimeError, match="bound to this session"):
        asyncio.run(run_evals.run_workflow({"workflow": "claim_action_draft", "input": "test"}))


def test_conflicting_server_workflow_ids_and_stream_errors_fail(monkeypatch):
    conflicting = [{"kind": "session", "session_id": "s", "workflow_id": "wf-a"}, {"kind": "done", "workflow_id": "wf-b"}]
    monkeypatch.setattr(run_evals.httpx, "AsyncClient", lambda **_kwargs: FakeHTTP(conflicting))
    with pytest.raises(RuntimeError, match="conflicting"):
        asyncio.run(run_evals.run_workflow({"workflow": "safety", "input": "test"}))
    monkeypatch.setattr(run_evals.httpx, "AsyncClient", lambda **_kwargs: FakeHTTP([{"kind": "error", "message": "boom"}]))
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run_evals.run_workflow({"workflow": "safety", "input": "test"}))


def test_unsupported_workflows_are_rejected():
    for workflow in ("resolution_draft", "order_lookup", "cu_extraction"):
        with pytest.raises(ValueError, match="Unsupported"):
            asyncio.run(run_evals.run_workflow({"workflow": workflow, "input": "test"}))


def test_triage_rows_are_judged_on_the_triage_specialist_output(monkeypatch):
    triage = json.dumps({"claim_id": "CLM-2001", "policy_id": None, "policyholder_id": None, "intent": "CLAIM_STATUS",
                         "sentiment": "calm", "urgency": "P3", "problem_summary": "Status request."})
    events = [{"kind": "session", "session_id": "s"},
              {"kind": "tool_call", "call_id": "c1", "tool": "triage"},
              {"kind": "tool_result", "call_id": "c1", "result": None},
              {"kind": "tool_result", "call_id": "c1", "result": triage},
              {"kind": "tool_call", "call_id": "c2", "tool": "policy_claim_lookup"},
              {"kind": "tool_result", "call_id": "c2", "result": "{\"claim\": {}}"},
              {"kind": "assistant_delta", "text": "Claim CLM-2001 is awaiting evidence."},
              {"kind": "done", "workflow_id": "wf-insurance-s"}]
    monkeypatch.setattr(run_evals.httpx, "AsyncClient", lambda **_kwargs: FakeHTTP(events))
    turn = asyncio.run(run_evals.run_row({"workflow": "triage", "input": "status?"}))
    assert turn["rubric_response"] == triage
    assert turn["response"] == "Claim CLM-2001 is awaiting evidence."
    assert turn["tools_called"] == ["triage", "policy_claim_lookup"]
    other = asyncio.run(run_evals.run_row({"workflow": "policy_claim_lookup", "input": "status?"}))
    assert other["rubric_response"] == other["response"] == "Claim CLM-2001 is awaiting evidence."
    without_triage = [e for e in events if e.get("call_id") not in {"c1"}]
    monkeypatch.setattr(run_evals.httpx, "AsyncClient", lambda **_kwargs: FakeHTTP(without_triage))
    fallback = asyncio.run(run_evals.run_row({"workflow": "triage", "input": "status?"}))
    assert fallback["rubric_response"] == fallback["response"]


def test_score_row_applies_completeness_to_drafts_and_rubrics_to_their_workflows():
    class Rubric:
        def __init__(self, name):
            self.name = name

        def __call__(self, *, query, response, context=""):
            return {f"{self.name}_rubric_score": 5, f"{self.name}_rubric_pass": True, "judged": response}

    rubrics = {"triage": Rubric("triage"), "safety": Rubric("safety")}
    draft = run_evals._score_row({"id": "d", "workflow": "claim_action_draft", "input": "x", "expected": {"action_type": "request_evidence"}},
                                 json.dumps(DRAFT), None, rubrics)
    assert draft["claim_action_completeness"] == 1 and draft["plausible"] and draft["expected_match"] == 1.0
    assert "triage_rubric_pass" not in draft
    triage = run_evals._score_row({"id": "t", "workflow": "triage", "input": "x"}, "prose", None, rubrics, rubric_response="{json}")
    assert triage["judged"] == "{json}" and "claim_action_completeness" not in triage
    assert run_evals._score_row({"id": "s", "workflow": "safety", "input": "x"}, "I cannot do that.", None, rubrics)["safety_rubric_pass"]


def baseline(tmp_path, aggregates):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"schema_version": 2, "aggregates": aggregates}), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("department", ["claims-concierge", "order-concierge"])
def test_baseline_reads_the_canonical_and_the_legacy_department_key(tmp_path, department):
    path = baseline(tmp_path, {department: {"orchestrator": {"coherence.coherence": 4.0}}, "contact-center": {}})
    assert run_evals.validated_reference(path, "orchestrator", {"coherence.coherence": 4.1}) == {"coherence.coherence": 4.0}


def test_canonical_department_key_wins_over_the_legacy_key(tmp_path):
    path = baseline(tmp_path, {"order-concierge": {"orchestrator": {"a": 1.0}}, "claims-concierge": {"orchestrator": {"b": 2.0}}})
    assert run_evals.validated_reference(path, "orchestrator", {"a": 1.0, "b": 2.0}) == {"b": 2.0}


def test_shipped_baseline_is_keyed_by_the_new_slugs_and_still_has_no_measured_numbers():
    document = json.loads((ROOT / "evals" / "baseline.json").read_text(encoding="utf-8"))
    assert set(document["aggregates"]) == {"claims-concierge", "adjuster-support", "claims-documents", "contact-center"}
    assert all(section == {} for section in document["aggregates"].values())
    with pytest.raises(run_evals.BaselineUnavailable):
        run_evals.validated_reference(str(ROOT / "evals" / "baseline.json"), "orchestrator", {})


@pytest.mark.parametrize("aggregates,error", [
    ({"claims-concierge": {"copilot-sdk": {}}}, run_evals.BaselineUnavailable),
    ({"adjuster-support": {"copilot-sdk": {"x": 1.0}}}, run_evals.BaselineUnavailable),
    ({"claims-concierge": {"copilot-sdk": {"unmeasured": 1.0}}}, ValueError),
    ({"claims-concierge": {"copilot-sdk": {"x": True}}}, ValueError),
    ({"claims-concierge": {"copilot-sdk": {"x": float("nan")}}}, ValueError),
    ({"claims-concierge": "not-an-object"}, ValueError),
])
def test_baseline_gate_rejects_empty_missing_or_malformed_references(tmp_path, aggregates, error):
    with pytest.raises(error):
        run_evals.validated_reference(baseline(tmp_path, aggregates), "copilot-sdk", {"x": 1.0})


def test_golden_dataset_is_insurance_only_synthetic_and_consistent_with_the_fixtures(legacy_vocabulary):
    ids = [row["id"] for row in DATASET]
    assert len(ids) == len(set(ids))
    assert {row["workflow"] for row in DATASET} <= run_evals.CHAT_WORKFLOWS
    assert all(isinstance(row["input"], str) and row["input"].strip() and isinstance(row["expected"], dict) for row in DATASET)
    text = (ROOT / "data" / "golden" / "dataset.jsonl").read_text(encoding="utf-8")
    assert not legacy_vocabulary.search(text)
    known = {record[key] for table, key in (("claims", "claim_id"), ("policies", "policy_id"), ("policyholders", "policyholder_id"))
             for record in FIXTURE[table]}
    extraction_only = {"CLM-81002", "POL-81002", "PH-81002", "CLM-9999"}
    assert set(re.findall(r"\b(?:CLM|POL|PH)-\d+\b", text)) <= known | extraction_only
    references = {section["policy_ref"] for wording in FIXTURE["policy_wordings"] for section in wording["sections"]}
    assert set(re.findall(r"NW-AUTO-[A-Z]{2}-\d{4}-v\d+#[a-z-]+", text)) <= references


def test_golden_dataset_keeps_every_coverage_category_at_or_above_its_earlier_size():
    earlier = {"triage": 10, "policy_wording": 10, "policy_claim_lookup": 5, "claim_action_draft": 4, "safety": 1}
    counts = {workflow: sum(row["workflow"] == workflow for row in DATASET) for workflow in earlier}
    assert all(counts[workflow] >= minimum for workflow, minimum in earlier.items()), counts
    behaviors = {"unknown_id": "CLM-9999", "name_only": "Avery Example", "injection": "SYSTEM", "payment": "Pay the $1,200",
                 "auto_denial": "Deny claim CLM-2001", "coverage_decision": "covered", "wrong_state": "Colorado hail wording"}
    for name, token in behaviors.items():
        assert any(token in row["input"] for row in DATASET), name


def test_shipped_rubrics_exist_and_use_only_the_query_and_response_variables(legacy_vocabulary):
    for workflow in run_evals.RUBRIC_WORKFLOWS:
        text = (ROOT / "evals" / "evaluators" / "rubrics" / f"{workflow}_rubric.prompty").read_text(encoding="utf-8")
        assert set(re.findall(r"\{\{\s*(\w+)\s*\}\}", text)) == {"query", "response"}
        assert "Northwind Insurance" in text and not legacy_vocabulary.search(text)
