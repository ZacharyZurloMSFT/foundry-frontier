"""Sample 5% of production traces from App Insights and re-score them.

Reads `AI-conversation` custom events from the last 24h, downsamples to 5%,
reconstructs `(user_input, assistant_response, workflow)` triples, and pipes
them through the same evaluators used at PR time. Results are written to
Application Insights as a custom metric `northwind.continuous_eval` so the
Foundry Observability dashboard can chart trends.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.monitor.query import LogsQueryClient, LogsQueryStatus  # noqa: E402
from run_evals import _score_row  # noqa: E402

APP_INSIGHTS_RESOURCE_ID = os.environ["APPINSIGHTS_RESOURCE_ID"]
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


async def main() -> None:
    cred = DefaultAzureCredential()
    client = LogsQueryClient(cred)
    resp = client.query_resource(APP_INSIGHTS_RESOURCE_ID, _query(), timespan=None)
    if resp.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError(f"App Insights query failed: {resp}")
    table = resp.tables[0]
    rows = [dict(zip(table.columns, r)) for r in table.rows]

    random.seed(0)
    sampled = [r for r in rows if random.random() < SAMPLE_RATE]
    print(f"Sampled {len(sampled)}/{len(rows)} traces")

    results = []
    for r in sampled:
        if r.get("harness") == "copilot-sdk":
            import httpx
            from harness_eval import grade
            base = os.environ["API_URI"].rstrip("/")
            async with httpx.AsyncClient(timeout=30) as http:
                snapshot = await http.get(f"{base}/harness/runs/{r['session_id']}")
                snapshot.raise_for_status()
            results.append({"id": r["session_id"], "harness": "copilot-sdk",
                            "department": r.get("department"), "regression_status": "unavailable",
                            **grade(snapshot.json(), allow_decided=True)})
            continue
        pseudo = {"id": r["session_id"], "workflow": r["workflow"], "input": r["user_input"]}
        results.append(_score_row(pseudo, r["response"], foundry_evals=None))

    Path("continuous-eval-results.json").write_text(json.dumps(results, indent=2))
    print(f"Wrote {len(results)} scored rows.")
    if any(r.get("harness_contract_pass") == 0.0 for r in results):
        raise RuntimeError("Continuous harness contract regression detected.")


if __name__ == "__main__":
    asyncio.run(main())
