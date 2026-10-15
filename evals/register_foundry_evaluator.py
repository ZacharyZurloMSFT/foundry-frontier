"""Register + run a Foundry evaluator for one or all Northwind agents.

For each selected agent:
  1. Register a prompt-based evaluator version in the Evaluator catalog
     via project_client.beta.evaluators.create_version() using the rubric
     from evals/foundry_rubrics.py.
  2. Generate {query, response} pairs by calling AOAI with the agent's
     real system prompt (from agents/*.yaml). Tool-calling agents (tech_lookup,
     manual_retrieval, work_order_draft, orchestrator) are graded on the
     shape/content of their conversational output, not tool-call fidelity.
  3. Create an eval definition referencing the catalog evaluator by name
     (testing_criteria type: azure_ai_evaluator) and run it.
  4. Poll to terminal and print the portal report URL.

Usage:
    python evals/register_foundry_evaluator.py --agent triage --limit 3
    python evals/register_foundry_evaluator.py --all --limit 3
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import EvaluatorCategory, EvaluatorDefinitionType
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals"))
from eval_inputs import ORCHESTRATOR_INLINE, TECH_LOOKUP_INLINE  # noqa: E402
from foundry_rubrics import AGENT_RUBRICS  # noqa: E402


def azd_env() -> dict[str, str]:
    out = subprocess.check_output(["azd", "env", "get-values"], cwd=REPO, text=True, shell=True)
    env: dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"')
    return env


def load_agent_system_prompt(agent_yaml: str) -> str:
    path = REPO / "agents" / agent_yaml
    y = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "instructions" in y:
        return y["instructions"]
    # Hosted orchestrator agent.yaml has no "instructions" — use a synthesized manager prompt
    # derived from the agent description so the smoke eval still runs against something
    # representative of the orchestrator's role.
    return (
        "You are the Northwind Order Concierge orchestrator. Coordinate 4 sub-agents "
        "(triage, order lookup, policy lookup, resolution drafting) to handle "
        "the customer request. Do not re-ask for details already provided. Chain the "
        "right steps: identify intent, ground policy questions in the knowledge base, "
        "look up the order when the customer references one, and draft a resolution "
        "(awaiting human approval) when a refund / reship / cancel / credit action is "
        "implied. Return a coherent final response that stitches the sub-steps "
        "together and ends with a clear next action."
    )


def rows_for_agent(agent: str, limit: int) -> list[dict]:
    """Return inputs [{id, input}, ...] for a given agent, capped at limit."""
    if agent == "tech_lookup":
        return TECH_LOOKUP_INLINE[:limit]
    if agent == "orchestrator":
        return ORCHESTRATOR_INLINE[:limit]
    dataset_path = REPO / "data" / "golden" / "dataset.jsonl"
    rows = [json.loads(l) for l in dataset_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if r["workflow"] == agent][:limit]


def generate_responses(rows: list[dict], system_prompt: str, aoai: AzureOpenAI, deployment: str) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        r = aoai.chat.completions.create(
            model=deployment,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": row["input"]},
            ],
        )
        text = r.choices[0].message.content or ""
        out.append({"id": row["id"], "query": row["input"], "response": text})
        print(f"  [{row['id']}] -> {text[:100].replace(chr(10),' ')}")
    return out


def register_evaluator(project_client: AIProjectClient, agent: str) -> str:
    spec = AGENT_RUBRICS[agent]
    name = f"{agent}_rubric"
    body = {
        "name": name,
        "categories": [EvaluatorCategory.QUALITY],
        "display_name": spec["display_name"],
        "description": spec["description"],
        "definition": {
            "type": EvaluatorDefinitionType.PROMPT,
            "prompt_text": spec["prompt_text"],
            "init_parameters": {
                "type": "object",
                "properties": {
                    "deployment_name": {"type": "string"},
                    "threshold": {"type": "number"},
                },
                "required": ["deployment_name", "threshold"],
            },
            "data_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "response": {"type": "string"},
                },
                "required": ["query", "response"],
            },
            "metrics": {
                name: {
                    "type": "ordinal",
                    "desirable_direction": "increase",
                    "min_value": 1,
                    "max_value": 5,
                }
            },
        },
    }
    try:
        result = project_client.beta.evaluators.create_version(name=name, evaluator_version=body)
    except HttpResponseError as e:
        print(f"  create_version failed ({e.status_code}): {getattr(e,'message','')[:600]}")
        raise
    version = getattr(result, "version", None) or getattr(result, "id", "?")
    print(f"  Registered evaluator: name={name} version={version}")
    return name


def run_eval(
    project_client: AIProjectClient,
    evaluator_name: str,
    scored: list[dict],
    judge_deployment: str,
) -> tuple[str, str, str]:
    client = project_client.get_openai_client()
    eval_obj = client.evals.create(
        name=f"{evaluator_name}-eval",
        data_source_config={
            "type": "custom",
            "item_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "query": {"type": "string"},
                    "response": {"type": "string"},
                },
                "required": ["query", "response"],
            },
            "include_sample_schema": False,
        },
        testing_criteria=[
            {
                "type": "azure_ai_evaluator",
                "name": evaluator_name,
                "evaluator_name": evaluator_name,
                "data_mapping": {
                    "query": "{{item.query}}",
                    "response": "{{item.response}}",
                },
                "initialization_parameters": {
                    "deployment_name": judge_deployment,
                    "threshold": 4,
                },
            }
        ],
    )
    run = client.evals.runs.create(
        eval_id=eval_obj.id,
        name=f"{evaluator_name}-run-{int(time.time())}",
        data_source={
            "type": "jsonl",
            "source": {
                "type": "file_content",
                "content": [{"item": row} for row in scored],
            },
        },
    )
    print(f"  Eval id: {eval_obj.id}  run id: {run.id}  polling...", end="", flush=True)
    for _ in range(120):
        run = client.evals.runs.retrieve(eval_id=eval_obj.id, run_id=run.id)
        if run.status in {"completed", "failed", "canceled"}:
            break
        print(".", end="", flush=True)
        time.sleep(3)
    print(f" {run.status}  counts={getattr(run,'result_counts',None)}")
    return eval_obj.id, run.id, getattr(run, "report_url", "") or ""


def process_agent(
    agent: str,
    project_client: AIProjectClient,
    aoai: AzureOpenAI,
    generator_deployment: str,
    judge_deployment: str,
    limit: int,
) -> dict:
    print(f"\n=== {agent} ===")
    rows = rows_for_agent(agent, limit)
    print(f"  Rows: {len(rows)}")
    if not rows:
        return {"agent": agent, "status": "skipped_no_rows"}
    system_prompt = load_agent_system_prompt(AGENT_RUBRICS[agent]["agent_yaml"])
    print("  Generating responses via AOAI...")
    scored = generate_responses(rows, system_prompt, aoai, generator_deployment)
    print("  Registering catalog evaluator...")
    evaluator_name = register_evaluator(project_client, agent)
    print("  Running eval...")
    eval_id, run_id, report_url = run_eval(project_client, evaluator_name, scored, judge_deployment)
    return {
        "agent": agent,
        "evaluator": evaluator_name,
        "eval_id": eval_id,
        "run_id": run_id,
        "report_url": report_url,
    }


def cmd_list(project_client: AIProjectClient) -> int:
    """Print all custom evaluators registered in the project catalog."""
    print("Custom evaluators in Foundry Evaluator catalog:")
    try:
        items = list(project_client.beta.evaluators.list(type="custom"))
    except HttpResponseError as e:
        print(f"  list failed ({e.status_code}): {getattr(e,'message','')[:400]}")
        return 1
    if not items:
        print("  (none)")
        return 0
    for e in items:
        print(
            f"  - name={e.name!r:35s} display={getattr(e,'display_name','?')!r:30s} "
            f"latest_version={getattr(e,'latest_version','?')}"
        )
    print(f"Total: {len(items)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=list(AGENT_RUBRICS.keys()), help="Single agent to process")
    ap.add_argument("--all", action="store_true", help="Process all agents")
    ap.add_argument("--list", action="store_true", help="Just list registered custom evaluators and exit")
    ap.add_argument("--limit", type=int, default=3, help="Max rows per agent (default 3)")
    args = ap.parse_args()

    if not args.agent and not args.all and not args.list:
        ap.error("pass --agent <name>, --all, or --list")

    env = azd_env()
    endpoint = env["FOUNDRY_ENDPOINT"].rstrip("/")
    project_endpoint = env["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    generator_deployment = env.get("FOUNDRY_PROMPT_AGENT_DEPLOYMENT", "gpt-4o")
    judge_deployment = (
        env.get("FOUNDRY_JUDGE_DEPLOYMENT")
        or env.get("FOUNDRY_PROMPT_AGENT_DEPLOYMENT")
        or "gpt-4o"
    )
    print(f"Project endpoint : {project_endpoint}")
    if not args.list:
        print(f"Generator model  : {generator_deployment}")
        print(f"Judge model      : {judge_deployment}")
        print(f"Targets          : {list(AGENT_RUBRICS.keys()) if args.all else [args.agent]}")

    cred = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=project_endpoint, credential=cred)

    if args.list:
        return cmd_list(project_client)

    targets = list(AGENT_RUBRICS.keys()) if args.all else [args.agent]
    token_provider = get_bearer_token_provider(cred, "https://cognitiveservices.azure.com/.default")
    aoai = AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2024-10-21",
    )

    summary = []
    for agent in targets:
        try:
            summary.append(process_agent(agent, project_client, aoai, generator_deployment, judge_deployment, args.limit))
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {e}")
            summary.append({"agent": agent, "status": "error", "error": str(e)[:400]})

    print("\n=== SUMMARY ===")
    for row in summary:
        print(json.dumps(row, indent=2))
    return 0 if all("error" not in r and r.get("status") != "error" for r in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
