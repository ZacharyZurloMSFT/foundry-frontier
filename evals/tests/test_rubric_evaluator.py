"""Smoke test for RubricEvaluator without hitting a real judge model.

Monkeypatches `prompty.execute` to return canned judge outputs so the class
contract (score/reason/pass keys, aggregation, error handling) is verified
in CI without Foundry credentials.

Run:
    cd evals
    python -m pytest tests/test_rubric_evaluator.py -v
    # or: python tests/test_rubric_evaluator.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import evaluators.rubric_evaluator as rubric_evaluator  # noqa: E402
from evaluators.rubric_evaluator import RubricEvaluator, load_agent_rubric  # noqa: E402


CANNED: dict[str, object] = {}


def _fake_execute(_path, inputs, configuration):  # noqa: ARG001
    key = inputs.get("response", "")
    return CANNED.get(key, {"score": 3, "reason": "default"})


def _make_ev() -> RubricEvaluator:
    return load_agent_rubric(
        "triage",
        model_config={
            "azure_endpoint": "https://example",
            "azure_deployment": "gpt-4o",
            "api_version": "2024-10-21",
        },
    )


def test_good_response_scores_5_and_passes(monkeypatch):
    monkeypatch.setattr(rubric_evaluator.prompty, "execute", _fake_execute)
    good = ('{"claim_id":"CLM-2001","policy_id":null,"policyholder_id":null,"intent":"CLAIM_STATUS",'
            '"sentiment":"calm","urgency":"P3","problem_summary":"Status request for claim CLM-2001."}')
    CANNED[good] = {"score": 5, "reason": "perfect", "identifiers_correct": True, "intent_correct": True, "json_valid": True}
    ev = _make_ev()
    out = ev(query="What is the status of claim CLM-2001?", response=good)
    assert out["triage_rubric_score"] == 5
    assert out["triage_rubric_pass"] is True
    assert "perfect" in out["triage_rubric_reason"]
    assert out["triage_rubric_raw"]["identifiers_correct"] is True


def test_bad_response_scores_low_and_fails(monkeypatch):
    monkeypatch.setattr(rubric_evaluator.prompty, "execute", _fake_execute)
    bad = "The claim is moving along. Priority P1."
    CANNED[bad] = {"score": 2, "reason": "not JSON and mis-prioritized", "intent_correct": False, "json_valid": False}
    ev = _make_ev()
    out = ev(query="Just checking in on claim CLM-2002, no rush at all", response=bad)
    assert out["triage_rubric_score"] == 2
    assert out["triage_rubric_pass"] is False


def test_unparseable_judge_output_returns_error(monkeypatch):
    monkeypatch.setattr(rubric_evaluator.prompty, "execute", lambda *_a, **_k: "sorry I refuse")
    ev = _make_ev()
    out = ev(query="q", response="r")
    assert out["triage_rubric_score"] is None
    assert out["triage_rubric_pass"] is False
    assert out["error"] == "unparseable_judge_output"


def test_judge_exception_is_swallowed(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("network down")
    monkeypatch.setattr(rubric_evaluator.prompty, "execute", _boom)
    ev = _make_ev()
    out = ev(query="q", response="r")
    assert out["triage_rubric_score"] is None
    assert out["triage_rubric_pass"] is False
    assert "network down" in out["error"]


def test_aggregate_results():
    ev = _make_ev()
    rows = [
        {"triage_rubric_score": 5, "triage_rubric_pass": True},
        {"triage_rubric_score": 3, "triage_rubric_pass": False},
        {"triage_rubric_score": 4, "triage_rubric_pass": True},
        {"triage_rubric_score": None, "triage_rubric_pass": False, "error": "judge_error"},
    ]
    agg = ev._aggregate_results(rows)
    assert agg["triage_rubric.mean_score"] == 4.0
    assert agg["triage_rubric.pass_rate"] == 0.5
    assert agg["triage_rubric.n_scored"] == 3
    assert agg["triage_rubric.n_errors"] == 1


def test_every_shipped_rubric_resolves_by_workflow_name(monkeypatch):
    import pytest

    pytest.importorskip("prompty.azure")
    monkeypatch.setattr(rubric_evaluator.prompty, "execute", lambda *_a, **_k: {"score": 4, "reason": "ok"})
    config = {"azure_endpoint": "https://example", "azure_deployment": "gpt-4o", "api_version": "2024-10-21"}
    for workflow in ("triage", "safety"):
        out = load_agent_rubric(workflow, model_config=config)(query="q", response="r")
        assert out[f"{workflow}_rubric_score"] == 4 and out[f"{workflow}_rubric_pass"] is True


if __name__ == "__main__":
    # Minimal standalone runner so this works without pytest installed.
    class _MP:
        def __init__(self):
            self._orig = []

        def setattr(self, target, name, value):
            self._orig.append((target, name, getattr(target, name)))
            setattr(target, name, value)

        def undo(self):
            for target, name, value in reversed(self._orig):
                setattr(target, name, value)
            self._orig.clear()

    tests = [
        test_good_response_scores_5_and_passes,
        test_bad_response_scores_low_and_fails,
        test_unparseable_judge_output_returns_error,
        test_judge_exception_is_swallowed,
    ]
    failed = 0
    for t in tests:
        mp = _MP()
        try:
            t(mp)
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        finally:
            mp.undo()
    try:
        test_aggregate_results()
        print("PASS  test_aggregate_results")
    except AssertionError as e:
        failed += 1
        print(f"FAIL  test_aggregate_results: {e}")
    raise SystemExit(1 if failed else 0)
