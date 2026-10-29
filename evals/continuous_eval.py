"""Sample saved harness contracts without replaying side-effectful agent actions.

Reads `AI-conversation` custom events from the last 24h, downsamples to 5%,
reconstructs `(user_input, assistant_response, workflow)` triples, and pipes
them through the same evaluators used at PR time. Copilot SDK harness runs are
re-read from the insurance API (`API_URI`, GET /harness/runs/{id}) and graded
with the harness contract; a claim-action draft that a human already decided is
accepted. This is contract-only evidence, not a model-quality or Foundry publication
claim. Unsupported trace shapes and no-data windows are incomplete, not passing.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.evidence import contract_lineage, report  # noqa: E402

SAMPLE_RATE = 0.05
LOOKBACK_HOURS = 24


def _query() -> str:
    return f"""
union customEvents, traces
| where timestamp > ago({LOOKBACK_HOURS}h)
| where name == 'AI-conversation' or message == 'AI-conversation'
| project timestamp,
          workflow = tostring(customDimensions.workflow),
          user_input = tostring(customDimensions.user_input),
          response = tostring(customDimensions.response),
          harness = tostring(customDimensions.harness),
          department = tostring(customDimensions.department),
          session_id = tostring(customDimensions.session_id)
| where isnotempty(response) or harness == 'copilot-sdk'
"""


async def score_samples(sampled: list[dict]) -> dict:
    import httpx
    from evals.harness_eval import grade

    results = []
    for row in sampled:
        run_id = row.get("session_id")
        if row.get("harness") != "copilot-sdk" or not run_id:
            results.append({"id": run_id, "workflow": row.get("workflow", "unknown"),
                            "error": "Unsupported trace evidence; model-quality scoring is not configured."})
            continue
        try:
            base = os.environ["API_URI"].rstrip("/")
            async with httpx.AsyncClient(timeout=30) as http:
                snapshot = await http.get(f"{base}/harness/runs/{run_id}")
                snapshot.raise_for_status()
            saved = snapshot.json()
            if not isinstance(saved, dict) or saved.get("run_id") != run_id:
                raise ValueError("Saved harness evidence belongs to a different run.")
            results.append({"id": run_id, "workflow": "harness", "harness": "copilot-sdk",
                            "department": row.get("department"),
                            **grade(saved, allow_decided=True)})
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            results.append({"id": run_id, "workflow": "harness", "error": str(exc)})
    payload = report(results, target="copilot-sdk")
    payload.update(
        evidence_scope="harness_contract_only",
        **contract_lineage(),
        publication_status="not_published",
        regression_status="unavailable",
        judge_status="not_run",
        evaluation_status=("incomplete" if not results or any(r.get("error") for r in results)
                           else "failed" if any(r.get("harness_contract_pass") != 1 for r in results)
                           else "evaluated"),
    )
    return payload


async def main() -> int:
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    cred = DefaultAzureCredential()
    client = LogsQueryClient(cred)
    resp = client.query_resource(os.environ["APPINSIGHTS_RESOURCE_ID"], _query(), timespan=None)
    if resp.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError(f"App Insights query failed: {resp}")
    table = resp.tables[0]
    rows = [dict(zip(table.columns, r)) for r in table.rows]

    rng = random.Random(0)
    sampled = [r for r in rows if rng.random() < SAMPLE_RATE]
    print(f"Sampled {len(sampled)}/{len(rows)} traces")

    payload = await score_samples(sampled)
    payload["eligible_trace_count"] = len(rows)
    Path("continuous-eval-results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(payload['rows'])} rows: {payload['evaluation_status']}.")
    return {"evaluated": 0, "failed": 1, "incomplete": 2}[payload["evaluation_status"]]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
