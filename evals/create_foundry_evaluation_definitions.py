"""Create or reuse named Foundry evaluations without starting paid runs."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from evals.foundry_rubrics import AGENT_RUBRICS  # noqa: E402
from evals.publish_foundry_evidence import ensure_evaluation_definition  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-endpoint",
        default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("AZURE_AI_PROJECT_ENDPOINT"),
        help="Foundry project endpoint; defaults to FOUNDRY_PROJECT_ENDPOINT.",
    )
    parser.add_argument(
        "--judge-deployment",
        default=os.environ.get("FOUNDRY_JUDGE_DEPLOYMENT"),
        help="Existing chat-completions deployment in the Foundry project.",
    )
    args = parser.parse_args()
    if not args.project_endpoint:
        parser.error("Pass --project-endpoint or configure FOUNDRY_PROJECT_ENDPOINT.")
    if not args.judge_deployment:
        parser.error("Pass --judge-deployment or configure FOUNDRY_JUDGE_DEPLOYMENT.")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=args.project_endpoint, credential=credential)
    try:
        evaluations = [
            ensure_evaluation_definition(
                project_client,
                agent=agent,
                judge_deployment=args.judge_deployment,
            )
            for agent in AGENT_RUBRICS
        ]
    finally:
        project_client.close()
        credential.close()

    print(json.dumps({
        "project_endpoint": args.project_endpoint,
        "judge_deployment": args.judge_deployment,
        "evaluation_definitions": evaluations,
        "runs_started": 0,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
