from types import SimpleNamespace
from datetime import timedelta
import json

import httpx
import pytest
from azure.core.exceptions import HttpResponseError
from azure.monitor.query import LogsQueryStatus

from app import traces


def test_unavailable_trace_is_explicit_and_never_synthetic(monkeypatch):
    monkeypatch.setattr(traces, "_try_query", lambda *_: None)
    result = traces.get_trace("insurance-session")
    assert result["status"] == "unavailable"
    assert result["spans"] == []
    assert result["totals"]["cost_usd"] == 0.0
    assert "No synthetic trace or cost estimate was substituted" in result["note"]


@pytest.mark.parametrize("result", [
    {"spans": [{"name": "actual-span"}], "totals": {"cost_usd": 0.1}},
    {"spans": [], "note": "No ingested spans"},
    {"spans": [], "note": "App Insights query failed"},
])
def test_actual_trace_result_is_not_replaced_with_stub(monkeypatch, result):
    monkeypatch.setattr(traces, "_try_query", lambda *_: result)
    assert traces.get_trace("session") is result


@pytest.mark.parametrize("properties_as_json", [False, True])
def test_workspace_query_correlates_real_spans_and_preserves_metrics(monkeypatch, properties_as_json):
    calls = []
    properties = {
        "gen_ai.agent.name": "northwind-insurance-orchestrator",
        "gen_ai.tool.name": "policy_lookup",
        "gen_ai.request.model": "gpt-5.4",
        "gen_ai.usage.input_tokens": "120",
        "gen_ai.usage.output_tokens": 20,
        "cost_usd": 0.001,
    }
    table = SimpleNamespace(
        columns=["timestamp", "itemType", "name", "duration", "operation_Id",
                 "operation_ParentId", "id", "customDimensions"],
        rows=[["2026-10-14T21:30:00Z", "AppDependencies", "actual-policy-lookup", 12.5,
               "actual-operation", "actual-parent", "actual-span",
               json.dumps(properties) if properties_as_json else properties]],
    )

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def query_workspace(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(status=LogsQueryStatus.SUCCESS, tables=[table])

    monkeypatch.setenv("APPINSIGHTS_WORKSPACE_ID", "workspace-id")
    monkeypatch.setattr(traces, "LogsQueryClient", Client)
    monkeypatch.setattr(traces, "get_credential", lambda: "existing-managed-identity")
    result = traces.get_trace("harness-actual", 60)
    query = calls[1]["query"]
    assert "AppDependencies, AppRequests, AppTraces, AppEvents" in query
    assert 'tostring(Properties.session_id) == "harness-actual"' in query
    assert '"wf-harness-actual"' in query
    assert "OperationId in (correlated)" in query
    assert calls[1]["timespan"] == timedelta(minutes=60)
    assert calls[1]["server_timeout"] == 20
    assert result["status"] == "ready" and result["source"] == "azure_monitor"
    assert result["spans"][0]["trace_id"] == "actual-operation"
    assert result["spans"][0]["agent"] == "northwind-insurance-orchestrator"
    assert result["totals"]["prompt_tokens"] == 120
    assert result["totals"]["completion_tokens"] == 20
    assert result["totals"]["cost_usd"] == 0.001


@pytest.mark.parametrize("failure", ["permission", "partial"])
def test_query_failure_is_explicit_without_synthetic_spans(monkeypatch, failure):
    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def query_workspace(self, **kwargs):
            if failure == "permission":
                raise HttpResponseError(message="Denied")
            return SimpleNamespace(status=LogsQueryStatus.PARTIAL)

    monkeypatch.setenv("APPINSIGHTS_WORKSPACE_ID", "workspace-id")
    monkeypatch.setattr(traces, "LogsQueryClient", Client)
    monkeypatch.setattr(traces, "get_credential", lambda: "existing-managed-identity")
    result = traces.get_trace("insurance-session")
    assert result["status"] == "unavailable"
    assert result["spans"] == []
    assert "stub" not in result["source"]


@pytest.mark.parametrize("session_id", ['session" | take 500', "a.b", "a" * 129])
def test_invalid_session_never_reaches_query(monkeypatch, session_id):
    def query(*_):
        pytest.fail("Invalid session reached the workspace query.")

    monkeypatch.setattr(traces, "_try_query", query)
    with pytest.raises(ValueError, match="Trace session IDs"):
        traces.get_trace(session_id)


@pytest.mark.asyncio
async def test_trace_route_rejects_query_syntax_and_preserves_window_clamp(monkeypatch):
    from app import main
    calls = []
    monkeypatch.setattr(main, "get_trace", lambda sid, minutes: calls.append((sid, minutes)) or {"spans": []})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/traces/harness-safe?minutes=999")
        assert response.status_code == 200
        assert calls == [("harness-safe", 60)]
        response = await client.get("/traces/unsafe%22")
        assert response.status_code == 422
        assert len(calls) == 1
