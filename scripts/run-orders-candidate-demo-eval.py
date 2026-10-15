"""One guarded native model-target evaluation of the approved Orders custom model."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import yaml
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

native = importlib.import_module("run-orders-native-demo-evals")
MODEL = "gpt-4.1-mini-2025-04-14.ft-20ae810198eb426e9d04d0530cc1812e-northwind-orders-triage-demo-v1"
DEPLOYMENT = "northwind-orders-triage-ft-v1"
DATASET = native.DATA_PREFIX + "03-triage-evaluation/versions/1"
SNAPSHOT = "cd5bee219a357bd91972fe4d366bce955dde3e99ff13e3a3ec9a0717bc914245"
NAME = "northwind-orders-DEMO-triage-ft-model-native"
ARM = ("https://management.azure.com/subscriptions/137c905b-7734-479e-acc5-ab8222462936"
       "/resourceGroups/rg-northwind-dev/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind")


def build_plan(files: Path):
    deployment = json.loads((files / "orders-candidate-deployment.json").read_text(encoding="utf-8"))
    if deployment["state"] != "Succeeded" or deployment["verified_training"]["fine_tuned_model"] != MODEL:
        raise ValueError("Exact approved custom model deployment must have succeeded")
    if deployment["budget"]["candidate_reservation_usd"] != "2.50":
        raise ValueError("Compute-inclusive candidate budget has not been reconciled")
    raw = (files / "verified-native-triage-evaluation.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != SNAPSHOT:
        raise ValueError("Previously downloaded native dataset snapshot changed")
    rows = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    if len(rows) != 100 or len({row["example_id"] for row in rows}) != 100:
        raise ValueError("Require all 100 distinct existing held-out examples")
    prompt = yaml.safe_load((Path(__file__).parents[1] / "agents" / "triage.yaml").read_text(encoding="utf-8"))["instructions"]
    input_bound = len(prompt.encode("utf-8")) + max(len(row["query"].encode("utf-8")) for row in rows) + 1024
    output_cap = 512
    upper = Decimal(100) * (Decimal(input_bound) * Decimal("0.40") + output_cap * Decimal("1.60")) / 1_000_000
    if upper > Decimal("0.18"):
        raise ValueError("Full model-target evaluation does not fit its token reservation")
    definition = copy.deepcopy(json.loads((files / "orders-native-baseline-eval.json").read_text(encoding="utf-8"))["plan"]["jobs"]["triage"]["create"])
    definition["name"] = NAME
    if [c["evaluator_name"] for c in definition["testing_criteria"]] != ["builtin.f1_score"]:
        raise ValueError("Only non-model diagnostic F1 is allowed; no extra judge inference")
    target = {"type": "azure_ai_model", "model": DEPLOYMENT,
              "sampling_params": {"max_completion_tokens": output_cap, "temperature": 0}}
    job = {
        "target": target, "dataset_id": DATASET, "dataset_name": "03-triage-evaluation",
        "snapshot_sha256": SNAPSHOT, "uploaded_rows": 100, "selected_rows": 100,
        "bounds": {"max_input_tokens_per_row": input_bound, "max_output_tokens_per_row": output_cap,
                   "non_model_usd_per_row": "0", "non_model_note": "No hosted calls in model-target run; service overhead is reserved separately, not presumed free."},
        "create": definition, "source_mode": "existing-dataset-id",
        "run": {"name": NAME, "data_source": {
            "type": "azure_ai_target_completions", "source": {"type": "file_id", "id": DATASET},
            "target": target, "input_messages": {"type": "template", "template": [
                {"type": "message", "role": "system", "content": {"type": "input_text", "text": prompt}},
                {"type": "message", "role": "user", "content": {"type": "input_text", "text": "{{item.query}}"}},
            ]},
        }},
    }
    return {
        "model": {"deployment": DEPLOYMENT, "model_name": MODEL, "model_version": "1", "sku": "DeveloperTier",
                  "pricing": {"input_usd_per_million": "0.40", "output_usd_per_million": "1.60"}},
        "jobs": {"triage": job}, "token_upper_bound_usd": str(upper),
        "budget": deployment["budget"], "dataset_authorization": "synthetic-demo-only",
        "comparison_caveat": "GPT4o hosted baseline versus GPT4.1mini fine-tuned model target changes base model, output cap and serving surface. Differences cannot be attributed solely to fine-tuning. One hosted probe is separate, not a hosted100-row benchmark.",
    }


def live_preflight(project, credential, plan):
    with httpx.Client(timeout=90) as http:
        response = http.get(ARM + "/deployments/" + DEPLOYMENT,
                            params={"api-version": "2025-07-01-preview"},
                            headers={"Authorization": "Bearer " + credential.get_token("https://management.azure.com/.default").token})
        response.raise_for_status()
        d = response.json()
        if (d["properties"]["provisioningState"] != "Succeeded"
                or d["properties"]["model"]["name"] != MODEL
                or d["sku"] != {"name": "DeveloperTier", "capacity": 50}):
            raise ValueError("Candidate model/SKU/state changed")
        for meter, price in (("gpt-4.1-mini-dev-ft inpt glbl Tokens", 0.0004),
                             ("gpt-4.1-mini-dev-ft opt glbl Tokens", 0.0016)):
            response = http.get("https://prices.azure.com/api/retail/prices",
                                params={"$filter": f"armRegionName eq 'eastus2' and meterName eq '{meter}'",
                                        "currencyCode": "USD"})
            response.raise_for_status()
            data = response.json()
            if (data.get("NextPageLink") or len(data["Items"]) != 1
                    or data["Items"][0]["retailPrice"] != price or data["Items"][0]["unitOfMeasure"] != "1K"):
                raise ValueError("Candidate price verification failed")
    dataset = project.datasets.get("03-triage-evaluation", "1")
    if dataset.id != DATASET:
        raise ValueError("Registered native dataset identity changed")
    return {"checked_at": native.now(), "deployment_id": d["id"], "dataset_id": dataset.id,
            "model": MODEL, "sku": d["sku"], "token_upper_bound_usd": plan["token_upper_bound_usd"]}


def execute(client, receipt, save):
    plan = receipt["plan"]
    job = plan["jobs"]["triage"]
    entry = receipt["jobs"].setdefault("triage", {})
    deadline = time.monotonic() + 900
    identity = {"demo_plan_sha256": receipt["plan_sha256"], "demo_role": "candidate-triage",
                "dataset_authorization": "demo-only"}
    entry["identity"] = identity
    if not entry.get("eval_id"):
        matches = [e for e in native.pages(client.evals.list, deadline=deadline) if e["name"] == NAME]
        if matches:
            if len(matches) != 1 or not native.includes(matches[0].get("metadata"), identity):
                raise ValueError("Existing candidate evaluation is not uniquely the same plan")
            evaluation = matches[0]
        elif entry.get("eval_submission_started"):
            raise RuntimeError("Uncertain prior evaluation POST; never repeat")
        else:
            entry["eval_submission_started"] = True
            save()
            evaluation = native.plain(client.evals.create(**job["create"], metadata=identity))
        entry["eval_id"] = evaluation["id"]
        save()
    remote = native.normalized_evaluation_definition(native.plain(client.evals.retrieve(entry["eval_id"])))
    if not native.includes(remote, job["create"]) or not native.includes(remote.get("metadata"), identity):
        raise ValueError("Candidate evaluation criteria readback mismatch")
    entry["definition_readback_verified"] = True
    save()
    if not entry.get("run_id"):
        runs = native.pages(client.evals.runs.list, eval_id=entry["eval_id"], deadline=deadline)
        if runs:
            if len(runs) != 1 or not native.includes(runs[0].get("metadata"), identity):
                raise ValueError("Existing candidate run is not uniquely the same plan")
            run = runs[0]
        elif entry.get("run_submission_started"):
            raise RuntimeError("Uncertain prior paid POST; never repeat")
        else:
            entry.update(run_submission_started=True, state="submission_started")
            receipt["inference_started"] = True
            save()
            run = native.plain(client.evals.runs.create(eval_id=entry["eval_id"], **job["run"], metadata=identity))
        entry.update(run_id=run["id"], service_status=run.get("status"), state="submitted")
        save()
    native.poll_run(client, job, entry, save, deadline=deadline, poll_seconds=10)
    receipt["state"] = entry["state"]
    receipt["completed_at"] = native.now() if receipt["state"] == "completed" else None
    save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.files)
    path = args.files / "orders-candidate-native-eval.json"
    with native.receipt_lock(path):
        receipt = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "schema_version": 1, "plan": plan, "plan_sha256": native.digest(plan), "jobs": {}, "state": "prepared"}
        if receipt["plan_sha256"] != native.digest(plan):
            raise ValueError("Candidate plan changed; no replacement run permitted")
        save = lambda: native.atomic_json(path, receipt)
        save()
        if args.apply:
            expiry = json.loads((args.files / "orders-candidate-deployment.json").read_text(encoding="utf-8"))["fixed_24h_expiry_utc"]
            if (datetime.fromisoformat(expiry) - datetime.now(timezone.utc)).total_seconds() < 1200:
                raise ValueError("Insufficient Developer lifetime remains")
            probe = json.loads((args.files / "orders-candidate-hosted-probe.json").read_text(encoding="utf-8"))
            if probe.get("state") != "verified" or probe.get("provider_model") != MODEL:
                raise ValueError("A real hosted custom-model probe is required before candidate evaluation")
            with AzureCliCredential(tenant_id=native.TENANT, process_timeout=120) as credential:
                with AIProjectClient(endpoint=native.ENDPOINT, credential=credential, retry_total=0) as project:
                    receipt["preflight"] = live_preflight(project, credential, plan)
                    save()
                    execute(project.get_openai_client(max_retries=0, timeout=120), receipt, save)
        print(json.dumps({"state": receipt["state"], "token_upper_bound_usd": plan["token_upper_bound_usd"],
                          "eval_id": receipt["jobs"].get("triage", {}).get("eval_id"),
                          "run_id": receipt["jobs"].get("triage", {}).get("run_id")}, indent=2))


if __name__ == "__main__":
    main()
