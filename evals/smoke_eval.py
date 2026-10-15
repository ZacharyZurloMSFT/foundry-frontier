"""Tier-1 smoke eval — runs against the free GitHub Models endpoint.

Zero Azure credentials, zero cost, ~10s wall-time. The point is to catch
obvious prompt breakage (blank output, malformed JSON from triage, missing
required fields) before we spend Foundry quota on the Tier-2 golden set.

The endpoint contract: POST to https://models.github.ai/inference/chat/completions
with the GITHUB_TOKEN as bearer, OpenAI-compatible request body. Set
GH_MODELS_MODEL to override the default model.

Usage:
    python evals/smoke_eval.py --output smoke-results.json
    python evals/smoke_eval.py --self-check     # exit 0 iff env is wired
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "agents"

GH_MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"
DEFAULT_MODEL = os.environ.get("GH_MODELS_MODEL", "openai/gpt-4o-mini")

# 5 hand-picked micro-cases. Kept intentionally tiny — the goal is a signal
# in <60s, not a real quality measurement. That's what the Tier-2 golden
# set (run_evals.py) is for.
CASES: list[dict] = [
    {
        "workflow": "triage",
        "user":
            "Where's my order NW-40021? It was supposed to arrive Tuesday.",
        "must_be_valid_json": True,
        "must_contain_keys": ["order_id", "intent", "sentiment", "urgency",
                              "problem_summary"],
    },
    {
        "workflow": "triage",
        "user": "I want to return the cast-iron skillet from order NW-40022 — "
                "it arrived cracked.",
        "must_be_valid_json": True,
        "must_contain_keys": ["order_id", "intent"],
    },
    {
        "workflow": "manual_retrieval",
        "user": "What is Northwind's return window on kitchen items?",
        "must_be_valid_json": False,
        "must_mention_any": ["30", "kitchen", "return", "days"],
    },
    {
        "workflow": "work_order_draft",
        "user": "Order NW-40021 shipping SLA breached at day 8 for a gold-tier "
                "customer. Draft a resolution.",
        "must_be_valid_json": True,
        "must_contain_keys": ["order_id", "customer_id", "resolution_type",
                              "amount_usd", "urgency"],
    },
    {
        "workflow": "tech_lookup",
        "user": "Find order NW-40021.",
        "must_be_valid_json": False,
        "must_mention_any": ["NW-40021", "order", "customer"],
    },
]


def load_agent_instructions(workflow: str) -> str:
    """Read agents/<workflow>.yaml and return the `instructions:` block."""
    path = AGENTS_DIR / f"{workflow}.yaml"
    if not path.exists():
        return f"You are the Northwind {workflow} agent."
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return str(data.get("instructions") or "").strip()


def _call_gh_models(system: str, user: str, model: str, token: str) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 800,
    }
    r = httpx.post(
        GH_MODELS_ENDPOINT,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json=body, timeout=45.0,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


def _grade(case: dict, output: str) -> tuple[bool, str]:
    if not output.strip():
        return False, "empty output"
    if case.get("must_be_valid_json"):
        # Strip common markdown fence noise before parsing.
        candidate = output.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as e:
            return False, f"not valid JSON: {e.msg}"
        for k in case.get("must_contain_keys", []):
            if k not in parsed:
                return False, f"missing key: {k}"
        return True, "ok"
    for token in case.get("must_mention_any", []):
        if token.lower() in output.lower():
            return True, f"mentioned '{token}'"
    return False, f"no expected tokens ({case.get('must_mention_any')}) found"


def run(output_path: str, model: str, token: str) -> dict:
    started = time.perf_counter()
    results = []
    passed = 0
    for case in CASES:
        system = load_agent_instructions(case["workflow"])
        try:
            out = _call_gh_models(system, case["user"], model, token)
            ok, reason = _grade(case, out)
        except Exception as e:  # noqa: BLE001
            ok, reason, out = False, f"call failed: {e}", ""
        passed += 1 if ok else 0
        results.append({
            "workflow": case["workflow"], "pass": ok, "reason": reason,
            "output_preview": out[:280],
        })
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    report = {
        "model": model, "total": len(CASES), "passed": passed,
        "elapsed_ms": elapsed_ms, "cases": results,
    }
    Path(output_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="smoke-results.json")
    ap.add_argument("--self-check", action="store_true",
                    help="Just check that the env is wired; don't call the API.")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if args.self_check:
        if not token:
            print("GITHUB_TOKEN missing", file=sys.stderr)
            return 2
        for c in CASES:
            _ = load_agent_instructions(c["workflow"])
        print("self-check ok")
        return 0
    if not token:
        print("GITHUB_TOKEN missing — required for GitHub Models", file=sys.stderr)
        return 2

    report = run(args.output, DEFAULT_MODEL, token)
    print(json.dumps({"passed": report["passed"], "total": report["total"],
                      "elapsed_ms": report["elapsed_ms"]}, indent=2))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
