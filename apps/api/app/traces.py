"""In-app trace inspector — powers the trace panel + cost HUD + agent-handoff
mermaid diagram in web-portal. Queries the App Insights workspace directly (via
the same UAMI the container app runs under) so the demo never has to leave
the browser to look at spans.

Every response includes the build stamp (`build_info()`) so the UI's footer
can render "session running <git_sha>@<env>" — the human-readable side of B6.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from typing import Any

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ServiceRequestError, ServiceResponseError
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

from .azure_clients import get_credential
from .observability import build_info

logger = logging.getLogger(__name__)
SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


def _empty(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable", "source": "azure_monitor",
        "build": build_info(), "spans": [], "totals": {"latency_ms": 0,
        "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
        "tool_calls": 0, "agent_hops": 0}, "handoff_mermaid": "",
        "note": reason,
    }


def _try_query(session_id: str, minutes: int) -> dict[str, Any] | None:
    """Query workspace tables and correlate child spans by operation ID."""
    workspace_id = os.environ.get("APPINSIGHTS_WORKSPACE_ID") or os.environ.get(
        "LOG_ANALYTICS_WORKSPACE_ID")
    if not workspace_id:
        return None
    kql = (
        "let telemetry = union isfuzzy=true AppDependencies, AppRequests, AppTraces, AppEvents; "
        "let correlated = telemetry "
        f'| where tostring(Properties.session_id) == "{session_id}" '
        f'or tostring(Properties.workflow_id) in ("wf-{session_id}", "wf-insurance-{session_id}") '
        "| where isnotempty(OperationId) | distinct OperationId; "
        "telemetry "
        f'| where tostring(Properties.session_id) == "{session_id}" '
        f'or tostring(Properties.workflow_id) in ("wf-{session_id}", "wf-insurance-{session_id}") '
        "or OperationId in (correlated) "
        '| project timestamp=TimeGenerated, itemType=Type, '
        'name=coalesce(column_ifexists("Name", ""), column_ifexists("Message", "")), '
        'duration=todouble(column_ifexists("DurationMs", 0.0)), '
        'operation_Id=OperationId, operation_ParentId=ParentId, '
        'id=column_ifexists("Id", ""), customDimensions=Properties '
        "| order by timestamp asc | take 500"
    )
    try:
        with LogsQueryClient(
            credential=get_credential(), retry_total=0, connection_timeout=10, read_timeout=35,
        ) as client:
            resp = client.query_workspace(
                workspace_id=workspace_id, query=kql,
                timespan=timedelta(minutes=minutes), server_timeout=20,
            )
    except (ClientAuthenticationError, HttpResponseError, ServiceRequestError, ServiceResponseError):
        logger.exception("Live telemetry query failed for session %s", session_id)
        return _empty("Live telemetry query failed. Check workspace read access and connectivity; no stub was substituted.")
    if resp.status != LogsQueryStatus.SUCCESS:
        logger.warning("Live telemetry query returned a partial result for session %s", session_id)
        return _empty("Live telemetry query was incomplete; partial spans were not presented as a complete trace.")

    spans: list[dict[str, Any]] = []
    for table in getattr(resp, "tables", None) or []:
        cols = [c.name if hasattr(c, "name") else c for c in table.columns]
        for row in table.rows:
            r = dict(zip(cols, row))
            cd = r.get("customDimensions") or {}
            if isinstance(cd, str):
                cd = json.loads(cd)
            if not isinstance(cd, dict):
                raise ValueError("Telemetry properties must be an object.")
            spans.append({
                "ts": str(r.get("timestamp") or ""),
                "kind": str(r.get("itemType") or ""),
                "name": str(r.get("name") or ""),
                "duration_ms": float(r.get("duration") or 0.0),
                "trace_id": str(r.get("operation_Id") or ""),
                "parent_span_id": str(r.get("operation_ParentId") or ""),
                "span_id": str(r.get("id") or ""),
                "agent":  cd.get("agent_name") or cd.get("agent") or cd.get("gen_ai.agent.name") or "",
                "tool":   cd.get("tool_name") or cd.get("tool") or cd.get("gen_ai.tool.name") or "",
                "model":  cd.get("model") or cd.get("gen_ai.request.model") or cd.get("gen_ai.response.model") or "",
                "prompt_tokens":     _num(cd.get("prompt_tokens") or cd.get("gen_ai.usage.input_tokens", 0)),
                "completion_tokens": _num(cd.get("completion_tokens") or cd.get("gen_ai.usage.output_tokens", 0)),
                "cost_usd":          _num(cd.get("cost_usd", 0)),
                "cost_reported":     "cost_usd" in cd,
                "git_sha":           cd.get("git_sha", ""),
                "gateway":           cd.get("gateway.route", ""),
                "gateway_model":     cd.get("gateway.model_alias", ""),
                "prompt_sha":        {k.split(".", 1)[1]: v for k, v in cd.items()
                                      if isinstance(k, str) and k.startswith("prompt_sha.")},
            })
    return _finalize(session_id, spans)


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _finalize(session_id: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "latency_ms": int(sum(s["duration_ms"] for s in spans)),
        "cost_usd": round(sum(s["cost_usd"] for s in spans), 6),
        "prompt_tokens": int(sum(s["prompt_tokens"] for s in spans)),
        "completion_tokens": int(sum(s["completion_tokens"] for s in spans)),
        "tool_calls": sum(1 for s in spans if s["tool"]),
        "agent_hops": len({s["agent"] for s in spans if s["agent"]}),
    }
    return {
        "status": "ready" if spans else "no_data", "source": "azure_monitor",
        "session_id": session_id, "build": build_info(),
        "spans": spans, "totals": totals,
        "handoff_mermaid": _mermaid(spans),
        "note": (
            "Token and cost totals include only reported span metrics, not a complete Azure billing total."
            if spans else "No matching ingested spans in this window. Telemetry may still be arriving."
        ),
    }


def _mermaid(spans: list[dict[str, Any]]) -> str:
    """Emit a sequence diagram of unique agent→(agent|tool) transitions.
    Reads pleasantly in demos even when the trace has ~30 spans."""
    if not spans:
        return "sequenceDiagram\n    participant User\n    User->>Orchestrator: (no trace yet)"
    lines = ["sequenceDiagram", "    autonumber", "    participant User"]
    actors: dict[str, str] = {}

    def actor(name: str, kind: str) -> str:
        key = f"{kind}:{name}"
        if key not in actors:
            alias = f"A{len(actors)}"
            actors[key] = alias
            lines.append(f"    participant {alias} as {name}")
        return actors[key]

    prev = "User"
    for s in spans:
        if s["agent"]:
            cur = actor(s["agent"], "agent")
            lines.append(f"    {prev}->>{cur}: {s['name'] or 'invoke'}")
            prev = cur
        if s["tool"]:
            t = actor(s["tool"], "tool")
            lines.append(f"    {prev}->>{t}: tool {s['tool']}")
            lines.append(f"    {t}-->>{prev}: result")
    return "\n".join(lines)


def get_trace(session_id: str, minutes: int = 15) -> dict[str, Any]:
    if not re.fullmatch(SESSION_ID_PATTERN, session_id):
        raise ValueError("Trace session IDs must contain only letters, digits, underscores or hyphens.")
    result = _try_query(session_id, minutes)
    if result is not None:
        return result
    return _empty(
        "Live insurance telemetry is unavailable: configure the workspace, "
        "query SDK and read access. No synthetic trace or cost estimate was substituted."
    )
