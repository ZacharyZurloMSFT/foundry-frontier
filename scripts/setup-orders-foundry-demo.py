"""Configure native Foundry review/evaluation artifacts, without training or inference."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import yaml
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential

REVIEW_AGENT = "northwind-triage-review"
RULE_ID = "northwind-orders-triage-human-review"
EVALUATION_NAME = "northwind-orders-triage-precomputed-f1"


def agent_model(model: str) -> str:
    """Model string for the review agent: unchanged by default, `<connection>/<alias>` when
    AI_GATEWAY_MODEL_CONNECTION routes prompt agents through the AI Gateway (same rule and
    helpers as scripts/create-prompt-agents.py)."""
    if not (os.environ.get("AI_GATEWAY_MODEL_CONNECTION", "").strip()
            or os.environ.get("AI_GATEWAY_REQUIRED", "").strip()):
        return model
    path = Path(__file__).resolve().with_name("create-prompt-agents.py")
    spec = importlib.util.spec_from_file_location("create_prompt_agents", path)
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    return helper.resolve_agent_model(model, helper.gateway_models_from_env())


def evaluation_definition() -> dict:
    return {
        "name": EVALUATION_NAME,
        "data_source_config": {
            "type": "custom",
            "item_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "response": {"type": "string"},
                    "ground_truth": {"type": "string"},
                },
                "required": ["query", "response", "ground_truth"],
            },
        },
        "testing_criteria": [{
            "type": "azure_ai_evaluator", "name": "response-f1",
            "evaluator_name": "builtin.f1_score",
            "data_mapping": {"response": "{{item.response}}",
                             "ground_truth": "{{item.ground_truth}}"},
        }],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--template-id", required=True, help="Existing native human-evaluation template ID")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.project_endpoint.endswith("/projects/northwind-orders"):
        raise ValueError("Use the explicit northwind-orders project endpoint")
    if "/projects/northwind-orders/humanevaluationtemplates/" not in args.template_id:
        raise ValueError("Human template must belong to northwind-orders")
    root = Path(__file__).resolve().parents[1]
    prompt = yaml.safe_load((root / "agents" / "triage.yaml").read_text(encoding="utf-8"))
    prompt_hash = hashlib.sha256(prompt["instructions"].encode()).hexdigest()
    try:
        review_model = agent_model(args.model)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from None
    report = {
        "review_agent": REVIEW_AGENT,
        "review_agent_kind": "prompt",
        "purpose": "Native Human Evaluation companion; not the hosted production triage agent",
        "template_id": args.template_id,
        "evaluation": evaluation_definition(),
        "metric_note": "Token F1 is diagnostic only; structured triage promotion gates remain separate.",
        "inference_started": False, "training_started": False,
    }
    if review_model != args.model:
        report["model"] = review_model
    if not args.apply:
        print(json.dumps(report, indent=2))
        return
    credential = AzureCliCredential(tenant_id=args.tenant, process_timeout=120)
    try:
        with AIProjectClient(endpoint=args.project_endpoint, credential=credential, allow_preview=True) as project:
            existing = None
            try:
                for version in project.agents.list_versions(REVIEW_AGENT):
                    if ((version.metadata or {}).get("instructions_sha256") == prompt_hash
                            and getattr(version.definition, "model", None) == review_model):
                        existing = version
                        break
            except ResourceNotFoundError:
                pass
            if existing is None:
                existing = project.agents.create_version(
                    REVIEW_AGENT,
                    definition=PromptAgentDefinition(model=review_model, instructions=prompt["instructions"]),
                    description="Review-only companion for native Human Evaluation; hosted triage remains separate.",
                    metadata={"instructions_sha256": prompt_hash, "purpose": "human-review-only"},
                )
            report["review_agent_version"] = existing.version
            rule = project.evaluation_rules.create_or_update(RULE_ID, {
                "id": RULE_ID, "displayName": "Northwind Orders triage human review",
                "description": "Review-only companion; human feedback does not authorise model training.",
                "eventType": "manual", "enabled": True,
                "action": {"type": "humanEvaluation", "templateId": args.template_id},
                "filter": {"agentName": REVIEW_AGENT},
            })
            report["rule"] = rule.as_dict()
            with project.get_openai_client() as client:
                matches = [item for item in client.evals.list()
                           if item.name == EVALUATION_NAME]
                if len(matches) > 1:
                    raise ValueError("Multiple native evaluation definitions have the same name; review explicitly")
                evaluation = matches[0] if matches else client.evals.create(**evaluation_definition())
                report["evaluation_id"] = evaluation.id
            print(json.dumps(report, indent=2))
    finally:
        credential.close()


if __name__ == "__main__":
    main()
