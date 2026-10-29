import asyncio

import httpx
import pytest

from evals import continuous_eval, drift_detector


def historical(value=1.0, **overrides):
    return {"target": "copilot-sdk", "evidence_scope": "harness_contract_only",
            "evaluator_version": "harness-contract-v1", "evaluation_status": "evaluated",
            "aggregates": {"harness_contract_pass": value}, **overrides}


def test_drift_uses_comparable_measured_reports_and_only_deterioration():
    history = [historical()] * 3
    assert drift_detector.detect_drift(historical(.5), history, 2)[0][0] == "harness_contract_pass"
    assert drift_detector.detect_drift(historical(1), [historical(.5)] * 3, 2) == []
    assert drift_detector.detect_drift(historical(), history, 2) == []
    assert drift_detector.detect_drift(historical(.5, evaluation_status="failed"), history, 2)


@pytest.mark.parametrize("latest,history", [
    (historical(evaluation_status="incomplete"), [historical()] * 3),
    (historical(), [historical()] * 2),
    (historical(), [historical(evaluator_version="other")] * 3),
    (historical(aggregates={}), [historical()] * 3),
])
def test_missing_or_incomparable_drift_evidence_never_passes(latest, history):
    with pytest.raises(ValueError):
        drift_detector.detect_drift(latest, history, 2)


def test_no_samples_and_unsupported_traces_are_incomplete():
    assert asyncio.run(continuous_eval.score_samples([]))["evaluation_status"] == "incomplete"
    result = asyncio.run(continuous_eval.score_samples([{"session_id": "s1", "workflow": "triage"}]))
    assert result["evaluation_status"] == "incomplete"
    assert "not configured" in result["rows"][0]["error"]


def test_sampling_grades_saved_contract_without_replaying_agent(monkeypatch):
    monkeypatch.setenv("API_URI", "https://example.invalid")
    saved = {"run_id": "h1", "status": "succeeded", "output": "Synthetic claim status.",
             "events": [{"kind": "tool_start", "tool": "policy_claim_lookup"}]}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=saved)

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    result = asyncio.run(continuous_eval.score_samples([{"session_id": "h1", "harness": "copilot-sdk"}]))
    assert result["evaluation_status"] == "evaluated"
    assert result["evidence_scope"] == "harness_contract_only"
    assert result["aggregates"]["harness_contract_pass"] == 1
    assert result["publication_status"] == "not_published"
    assert len(requests) == 1 and requests[0].method == "GET"
    saved["run_id"] = "other"
    assert asyncio.run(continuous_eval.score_samples([{"session_id": "h1", "harness": "copilot-sdk"}]))["evaluation_status"] == "incomplete"


def test_drift_does_not_replace_a_missing_latest_report_with_old_evidence(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/insurance")

    def github(path, token, **params):
        if path.endswith("/runs"):
            return {"workflow_runs": [{"id": number} for number in range(4)]}
        return {"artifacts": []}

    monkeypatch.setattr(drift_detector, "_gh", github)
    assert drift_detector.main() == 2


def test_drift_deduplicates_an_existing_open_issue(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/insurance")
    marker = "<!-- eval-drift:copilot-sdk:harness_contract_only:harness_contract_pass -->"

    def github(path, token, **params):
        if path.endswith("/runs"):
            return {"workflow_runs": [{"id": number} for number in range(4)]}
        if path.endswith("/issues"):
            return [{"body": marker}]
        return {"artifacts": [{"id": int(path.split("/")[-2]), "name": "continuous-eval-results"}]}

    monkeypatch.setattr(drift_detector, "_gh", github)
    monkeypatch.setattr(drift_detector, "_download_artifact",
                        lambda repo, artifact, token: historical(0 if artifact == 0 else 1))

    def no_post(*args, **kwargs):
        raise AssertionError("Existing drift issue must not be duplicated")

    monkeypatch.setattr(drift_detector.httpx, "post", no_post)
    assert drift_detector.main() == 0
