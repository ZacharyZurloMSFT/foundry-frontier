"""Submit one explicitly authorised Developer demo job from imported native files."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from azure.identity import AzureCliCredential, get_bearer_token_provider
from openai import AzureOpenAI, BadRequestError

ACCOUNT_ID = (
    "/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-dev"
    "/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind"
)


def approved_plan(approval: dict, imported: dict) -> dict:
    auth = approval["authorization"]
    training = approval["training"]
    if (auth.get("synthetic_labels_accepted_for_demo") is not True
            or auth.get("global_processing_permitted") is not True
            or auth.get("hourly_custom_model_hosting_permitted") is not False
            or auth.get("max_incremental_cost_usd") != 10):
        raise ValueError("Expected the explicit demo-only $10/global/Developer authorization")
    if (approval.get("account") != "aisvc-zurlo-northwind"
            or approval.get("training_project") != "nw-claims-warranty"
            or training.get("tier") != "Developer"
            or training.get("epochs") != 1 or training.get("max_experiments") != 1):
        raise ValueError("Training location/tier/experiment count differs from the authorization")
    if training["conservative_training_cost_usd"] > training["reserved_cost_usd"]:
        raise ValueError("Training exceeds its budget reservation")
    if (imported.get("account") != approval["account"] or imported.get("project") != approval["training_project"]
            or imported.get("local_file_uploaded") is not False):
        raise ValueError("Expected imported user-uploaded native datasets in the selected training project")
    files = {item["dataset"]: item for item in imported["files"]}
    train = files["01-triage-training"]
    validation = files["02-triage-validation"]
    if train["records"] != 200 or validation["records"] != 50 or not train.get("id") or not validation.get("id"):
        raise ValueError("Training/validation file records or IDs are missing")
    return {
        "training_file": train["id"], "validation_file": validation["id"],
        "model": training["model"] + "-" + training["version"],
        "suffix": "zz-orders-triage", "seed": 106,
        "method": {"type": "supervised", "supervised": {"hyperparameters": {"n_epochs": 1}}},
        "extra_body": {"trainingType": "developerTier"},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--import-receipt", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    approval_bytes = args.approval.read_bytes()
    approval = json.loads(approval_bytes)
    imported = json.loads(args.import_receipt.read_text(encoding="utf-8"))
    plan = approved_plan(approval, imported)
    digest = hashlib.sha256(approval_bytes).hexdigest()
    journal = Path.home() / ".foundry-frontier" / "orders-learning" / "demo-training-journal" / (digest + ".json")
    if not args.apply:
        print(json.dumps({"state": "dry_run", "plan": plan, "approval_sha256": digest}, indent=2))
        return
    if args.receipt.exists() or journal.exists():
        raise ValueError("A prior attempt exists for this approval; reconcile it instead of submitting another job")
    credential = AzureCliCredential(tenant_id=args.tenant, process_timeout=120)
    arm = get_bearer_token_provider(credential, "https://management.azure.com/.default")
    inference = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
    try:
        response = httpx.get(
            "https://management.azure.com" + ACCOUNT_ID, params={"api-version": "2026-09-01"},
            headers={"Authorization": f"Bearer {arm()}"}, timeout=120,
        )
        response.raise_for_status()
        account = response.json()
        if account["properties"].get("defaultProject") != approval["training_project"]:
            raise ValueError("Account default routing no longer matches the authorised training project")
        with AzureOpenAI(
            azure_endpoint="https://aisvc-zurlo-northwind.openai.azure.com",
            api_version="2025-04-01-preview",
            azure_ad_token_provider=inference, max_retries=0, timeout=120,
        ) as client:
            for file_id in (plan["training_file"], plan["validation_file"]):
                file = client.files.wait_for_processing(file_id, poll_interval=3, max_wait_seconds=300)
                if file.status != "processed":
                    raise ValueError(f"Native fine-tuning file is not usable: {file_id}, {file.status}")
            record = {
                "schema_version": 1, "state": "submission_started", "job_id": None,
                "approval_sha256": digest, "account": approval["account"],
                "training_project": approval["training_project"], "plan": plan,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "labels": "synthetic-authorised-for-demo; not individually human-reviewed",
                "reserved_training_cost_usd": approval["training"]["reserved_cost_usd"],
            }
            journal.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            with journal.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(record, indent=2))
            with args.receipt.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(record, indent=2))
            try:
                job = client.fine_tuning.jobs.create(**plan)
            except BadRequestError:
                record.update(state="request_rejected", http_status=400,
                              note="No success is implied; reconcile native jobs before any retry.")
                for path in (journal, args.receipt):
                    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
                raise
            record.update(state="submitted", job_id=job.id, service_status=job.status)
            for path in (journal, args.receipt):
                path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(record, indent=2))
    finally:
        credential.close()


if __name__ == "__main__":
    main()
