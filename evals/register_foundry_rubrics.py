"""Register the Northwind claims rubric evaluators in a Foundry project.

This creates or reuses rubric evaluator versions only; it does not run evaluations
or invoke the deployed insurance API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from evals.foundry_rubrics import AGENT_RUBRICS  # noqa: E402
from evals.publish_foundry_evidence import ensure_evaluator  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-endpoint",
        default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("AZURE_AI_PROJECT_ENDPOINT"),
        required=False,
        help="Foundry project endpoint; defaults to FOUNDRY_PROJECT_ENDPOINT.",
    )
    args = parser.parse_args()
    if not args.project_endpoint:
        parser.error("Pass --project-endpoint or configure FOUNDRY_PROJECT_ENDPOINT.")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=args.project_endpoint, credential=credential)
    try:
        registered = [
            {"agent": agent, "evaluator": ensure_evaluator(project_client, agent)}
            for agent in AGENT_RUBRICS
        ]
    finally:
        project_client.close()
        credential.close()

    print(json.dumps({"project_endpoint": args.project_endpoint, "evaluators": registered}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
