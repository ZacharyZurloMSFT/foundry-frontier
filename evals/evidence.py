"""Offline evidence helpers shared by golden and sampled evaluations."""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contract_lineage() -> dict:
    root = Path(__file__).resolve().parents[1]
    paths = [
        "evals/harness_eval.py", "evals/evidence.py",
        "evals/evaluators/claim_action_completeness.py",
        "evals/insurance.py", "evals/insurance_gates.py", "evals/insurance_review.py",
        "data/northwind/insurance-fixtures.json",
    ]
    sources = {path: file_hash(root.joinpath(*path.split("/"))) for path in paths}
    return {
        "evaluator_version": hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
        "source_sha256": sources,
    }


def aggregate(rows: list[dict]) -> tuple[dict[str, float], dict[str, int]]:
    """Average each metric over its measured population, never unrelated rows."""
    measured: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, dict) and key != "fields_present":
                for metric, score in value.items():
                    if isinstance(score, (int, float)):
                        if math.isfinite(score):
                            row[f"{key}.{metric}"] = score
                        else:
                            row["error"] = f"{key}.{metric} was not a finite score."
        for key, value in list(row.items()):
            if key in {"id", "response_len", "latency_ms", "tool_call_count"}:
                continue
            if isinstance(value, (int, float)):
                if math.isfinite(value):
                    measured[key].append(float(value))
                else:
                    row["error"] = f"{key} was not a finite score."
    return (
        {key: round(sum(values) / len(values), 4) for key, values in measured.items()},
        {key: len(values) for key, values in measured.items()},
    )


def report(rows: list[dict], *, target: str) -> dict[str, Any]:
    aggregates, counts = aggregate(rows)
    by_workflow = {}
    for workflow in sorted({str(row.get("workflow", "unknown")) for row in rows}):
        selected = [row for row in rows if str(row.get("workflow", "unknown")) == workflow]
        values, samples = aggregate(selected)
        by_workflow[workflow] = {"aggregates": values, "metric_counts": samples, "row_count": len(selected)}
    latencies = sorted(row["latency_ms"] for row in rows
                       if isinstance(row.get("latency_ms"), (int, float))
                       and math.isfinite(row["latency_ms"]) and row["latency_ms"] >= 0)
    return {
        "schema_version": 1,
        "evaluation_run_id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "rows": rows,
        "row_count": len(rows),
        "aggregates": aggregates,
        "metric_counts": counts,
        "by_workflow": by_workflow,
        "performance": {
            "latency_samples": len(latencies),
            "latency_p50_ms": latencies[math.ceil(0.5 * len(latencies)) - 1] if latencies else None,
            "latency_p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
            "percentile_method": "nearest_rank",
            "token_usage": None,
            "cost_usd": None,
            "note": "Target elapsed time only; unmeasured usage/cost is unavailable, not zero.",
        },
    }
