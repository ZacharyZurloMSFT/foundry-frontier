"""Deploy the triage rubric evaluation to Foundry.

End-to-end:
  1. Read triage-* rows from the golden dataset.
  2. Call Azure OpenAI with the triage agent's system prompt to generate
     structured JSON responses (mirrors what the deployed prompt agent does).
  3. Write {query, response} JSONL to .foundry/datasets/triage-eval-input.jsonl.
  4. Run azure.ai.evaluation.evaluate() with our RubricEvaluator +
     CoherenceEvaluator, pushing results to the Foundry project.
  5. Print the studio URL so the run appears in the portal Evaluations tab.

Usage:
    cd evals
    python push_to_foundry.py               # 3 rows (smoke)
    python push_to_foundry.py --limit 12    # more rows
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals"))
from evaluators import load_agent_rubric  # noqa: E402


def _azd_env() -> dict[str, str]:
    """Load azd env vars (script may run without an activated azd shell)."""
    import subprocess

    out = subprocess.check_output(["azd", "env", "get-values"], cwd=REPO, text=True, shell=True)
    env: dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"')
    return env


def load_triage_system_prompt() -> str:
    y = yaml.safe_load((REPO / "agents" / "triage.yaml").read_text(encoding="utf-8"))
    return y["instructions"]


def generate_responses(rows: list[dict], client: AzureOpenAI, deployment: str) -> list[dict]:
    system = load_triage_system_prompt()
    out: list[dict] = []
    for row in rows:
        r = client.chat.completions.create(
            model=deployment,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": row["input"]},
            ],
        )
        response_text = r.choices[0].message.content or ""
        out.append({"id": row["id"], "query": row["input"], "response": response_text})
        print(f"  [{row['id']}] -> {response_text[:120]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3, help="Max triage rows to score (default 3 for smoke)")
    ap.add_argument("--eval-name", default="triage-rubric-smoke")
    args = ap.parse_args()

    env = _azd_env()
    endpoint = env["FOUNDRY_ENDPOINT"].rstrip("/")
    project_endpoint = env["FOUNDRY_PROJECT_ENDPOINT"]
    triage_deployment = env.get("FOUNDRY_PROMPT_AGENT_DEPLOYMENT", "gpt-4o")
    judge_deployment = (
        env.get("FOUNDRY_JUDGE_DEPLOYMENT")
        or env.get("FOUNDRY_PROMPT_AGENT_DEPLOYMENT")
        or env.get("FOUNDRY_WORKER_DEPLOYMENT", "gpt-4o")
    )
    api_version = env.get("FOUNDRY_API_VERSION", "2024-10-21")

    print(f"Foundry project : {project_endpoint}")
    print(f"Triage model    : {triage_deployment}")
    print(f"Judge model     : {judge_deployment}")

    cred = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(cred, "https://cognitiveservices.azure.com/.default")
    aoai = AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version,
    )

    # 1. Pick triage rows.
    dataset_path = REPO / "data" / "golden" / "dataset.jsonl"
    rows = [json.loads(l) for l in dataset_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    triage_rows = [r for r in rows if r["workflow"] == "triage"][: args.limit]
    print(f"Loaded {len(triage_rows)} triage rows")

    # 2. Generate responses via AOAI.
    print("Generating triage responses via AOAI...")
    scored_rows = generate_responses(triage_rows, aoai, triage_deployment)

    # 3. Write eval input JSONL.
    out_dir = REPO / ".foundry" / "datasets"
    out_dir.mkdir(parents=True, exist_ok=True)
    input_path = out_dir / "triage-eval-input.jsonl"
    input_path.write_text("\n".join(json.dumps(r) for r in scored_rows) + "\n", encoding="utf-8")
    print(f"Wrote {input_path} ({input_path.stat().st_size} bytes)")

    # 4. Configure judge (env vars are what the .prompty file reads).
    os.environ["FOUNDRY_ENDPOINT"] = endpoint
    os.environ["FOUNDRY_JUDGE_DEPLOYMENT"] = judge_deployment
    os.environ["FOUNDRY_API_VERSION"] = api_version

    judge_cfg = {
        "azure_endpoint": endpoint,
        "azure_deployment": judge_deployment,
        "api_version": api_version,
    }
    triage_rubric = load_agent_rubric("triage", judge_cfg)

    from azure.ai.evaluation import CoherenceEvaluator, evaluate

    coherence = CoherenceEvaluator(
        model_config={
            "azure_endpoint": endpoint,
            "azure_deployment": judge_deployment,
            "api_version": api_version,
        }
    )

    results_dir = REPO / ".foundry" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"Running evaluate() and uploading to {project_endpoint}...")
    result = evaluate(
        data=str(input_path),
        evaluators={
            "triage_rubric": triage_rubric,
            "coherence": coherence,
        },
        evaluation_name=args.eval_name,
        azure_ai_project=project_endpoint,
        output_path=str(REPO / ".foundry" / "results" / "triage-rubric-smoke.json"),
        tags={"agent": "triage", "tier": "smoke", "source": "push_to_foundry"},
    )
    print("\n=== EVALUATION COMPLETE ===")
    metrics = result.get("metrics", {})
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    studio = result.get("studio_url")
    if studio:
        print(f"\nStudio URL:\n  {studio}")
    else:
        print("\n(No studio_url returned — check the Foundry portal Evaluations tab.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
