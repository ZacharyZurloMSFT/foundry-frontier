"""Approval-first local CLI training; never imported by a public API handler."""
from __future__ import annotations

import math
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from curation import (
    ACCOUNT, PROJECT, PROJECT_ENDPOINT, STAGING_DEPLOYMENT, digest, file_hash, now,
    read_json, verify_approval, verify_manifest, write_json,
)

GUIDE = "https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/fine-tuning"
RETIREMENTS = "https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirements"
ACCOUNT_API_VERSION = "2026-09-01"


class TrainingPlacementError(ValueError):
    def __init__(self, message: str, verification: dict):
        super().__init__(message)
        self.verification = verification


def budget_template(manifest_path: Path) -> dict:
    return {
        "schema_version": 1, "approved": False, "approver": "", "approved_at": "",
        "expires_at": "", "manifest_sha256": file_hash(manifest_path),
        "account": ACCOUNT, "project": PROJECT, "account_resource_id": "",
        "base_model": "gpt-4.1-mini", "model_version": "2025-04-14",
        "region": "", "training_tier": "Standard", "residency": "regional",
        "epochs": 1, "max_epochs": 1, "max_training_tokens": 0, "max_cost_usd": 0,
        "training_price_per_million_tokens_usd": 0, "price_reference": "",
        "acknowledge_no_service_hard_cap": False,
        "admission": {
            "approved": False, "reviewer": "", "checked_at": "", "expires_at": "",
            "training_supported": False, "training_not_retired": False,
            "deployment_not_retired": False, "quota_confirmed": False,
            "evidence": "", "references": [GUIDE, RETIREMENTS],
        },
        "note": "Candidate only, not guaranteed available. Verify current model/version/region/tier, quota, pricing and retirement.",
    }


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Approval times must include a timezone")
    return parsed


def _positive(value, name: str, *, integer: bool = False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0
            or (integer and not isinstance(value, int))):
        raise ValueError(f"{name} must be a finite positive {'integer' if integer else 'number'}")
    return value


def validate_account_resource_id(value: str) -> str:
    pattern = (r"/subscriptions/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
               r"/resourceGroups/[^/?#]+/providers/Microsoft\.CognitiveServices/accounts/" + re.escape(ACCOUNT))
    if not isinstance(value, str) or not re.fullmatch(pattern, value, flags=re.IGNORECASE):
        raise ValueError("Budget requires the exact ARM account_resource_id for " + ACCOUNT)
    return value


def read_account(credential, resource_id: str) -> dict:
    """GET only: verify routing against ARM rather than trusting a cached project selection."""
    import httpx

    resource_id = validate_account_resource_id(resource_id)
    token = credential.get_token("https://management.azure.com/.default")
    response = httpx.get(
        "https://management.azure.com" + resource_id,
        params={"api-version": ACCOUNT_API_VERSION},
        headers={"Authorization": "Bearer " + token.token},
        timeout=30, follow_redirects=False,
    )
    response.raise_for_status()
    return response.json()


def verify_training_project(credential, resource_id: str, selected_project: str, region: str) -> dict:
    if selected_project != PROJECT:
        raise ValueError("Explicit --training-project must match approved dataset project " + PROJECT)
    account = read_account(credential, resource_id)
    if str(account.get("id") or "").casefold() != resource_id.casefold() or str(account.get("name") or "").casefold() != ACCOUNT.casefold():
        raise ValueError("ARM returned a different account; training is blocked")
    properties = account.get("properties")
    actual_default = properties.get("defaultProject") if isinstance(properties, dict) else None
    verification = {"state": "blocked", "checked_at": now(), "account_resource_id": resource_id,
                    "requested_project": selected_project, "actual_default_project": actual_default,
                    "region": account.get("location"), "api_version": ACCOUNT_API_VERSION}
    if actual_default != selected_project:
        raise TrainingPlacementError(
            f"Account defaultProject={actual_default!r} differs from selected training project "
            f"{selected_project!r}; training is BLOCKED. No fallback to Claims, old project or default alias.",
            verification,
        )
    actual_region = str(account.get("location") or "")
    if actual_region.casefold().replace(" ", "") != region.casefold().replace(" ", ""):
        raise TrainingPlacementError("ARM account region differs from approved budget/admission region", verification)
    return {**verification, "state": "verified"}


def training_plan(manifest_path: Path, budget_path: Path | None) -> dict:
    manifest = verify_manifest(manifest_path)
    plan = {
        "schema_version": 1, "state": "dry_run", "created_at": now(),
        "manifest_sha256": file_hash(manifest_path), "account": ACCOUNT, "project": PROJECT,
        "staging_deployment": STAGING_DEPLOYMENT, "blocked_reasons": [],
        "default_project_verification": {"state": "not_checked", "requested_project": PROJECT,
                                         "actual_default_project": None, "api_version": ACCOUNT_API_VERSION},
        "budget_scope": "training-only; validation, storage, inference and deployment charges need separate approval",
    }
    try:
        verify_approval(manifest_path)
    except (ValueError, OSError) as exc:
        plan["blocked_reasons"].append(f"dataset_approval: {exc}")
    if manifest["counts"]["train"] < 10:
        plan["blocked_reasons"].append("Azure fine-tuning requires at least 10 accepted training examples")
    # One byte per token plus generous per-example chat framing is conservative for
    # the text-only GPT candidate; not the service's authoritative billed count.
    per_epoch = (manifest_path.parent / "train.jsonl").stat().st_size + 1024 * manifest["counts"]["train"]
    plan["training_tokens_upper_bound_per_epoch"] = per_epoch
    if budget_path is None:
        plan["blocked_reasons"].append("An explicit human-approved budget/admission document is required")
        return plan
    budget = read_json(budget_path)
    plan["budget_sha256"] = file_hash(budget_path)
    try:
        current = datetime.now(timezone.utc)
        if budget.get("approved") is not True or not budget.get("approver", "").strip():
            raise ValueError("Budget must be explicitly approved by a named human")
        if not _time(budget["approved_at"]) <= current < _time(budget["expires_at"]):
            raise ValueError("Budget approval is expired or not yet valid")
        for field, expected in (("manifest_sha256", file_hash(manifest_path)), ("account", ACCOUNT), ("project", PROJECT)):
            if budget.get(field) != expected:
                raise ValueError(f"Budget {field} does not match this release")
        plan["account_resource_id"] = validate_account_resource_id(budget.get("account_resource_id"))
        if not budget.get("base_model", "").strip() or not budget.get("model_version", "").strip():
            raise ValueError("Base model and exact version are mandatory")
        if not budget.get("region", "").strip():
            raise ValueError("Resource region is mandatory")
        tier = budget.get("training_tier")
        if tier not in ("Standard", "GlobalStandard", "Developer"):
            raise ValueError("Unsupported training tier; reverify current API before adding one")
        residency = budget.get("residency")
        if (tier == "Standard" and residency != "regional") or (tier != "Standard" and residency != "global"):
            raise ValueError("Standard requires regional residency; GlobalStandard/Developer require global consent")
        admission = budget["admission"]
        if not admission.get("reviewer", "").strip() or not admission.get("evidence", "").strip():
            raise ValueError("A named admission reviewer and current region/model/tier evidence are required")
        if not _time(admission["checked_at"]) <= current < _time(admission["expires_at"]):
            raise ValueError("Model admission/retirement verification expired or not yet valid")
        if any(admission.get(key) is not True for key in (
            "approved", "training_supported", "training_not_retired", "deployment_not_retired", "quota_confirmed"
        )):
            raise ValueError("Model training, deployment, retirement and quota gates must all be approved")
        if not admission.get("references") or not budget.get("price_reference", "").strip():
            raise ValueError("Current admission and price references are required")
        epochs = _positive(budget["epochs"], "epochs", integer=True)
        if epochs > _positive(budget["max_epochs"], "max_epochs", integer=True):
            raise ValueError("Epoch bound exceeded")
        tokens = per_epoch * epochs
        if tokens > _positive(budget["max_training_tokens"], "max_training_tokens", integer=True):
            raise ValueError("Conservative training-token bound exceeds approved maximum")
        price = _positive(budget["training_price_per_million_tokens_usd"], "training price")
        cost = tokens * price / 1_000_000
        if cost > _positive(budget["max_cost_usd"], "max_cost_usd"):
            raise ValueError("Conservative training cost exceeds approved budget")
        if budget.get("acknowledge_no_service_hard_cap") is not True:
            raise ValueError("Acknowledge this is a preflight bound, not an Azure billing hard cap")
        plan.update(
            model=budget["base_model"] + "-" + budget["model_version"], epochs=epochs,
            training_tier=tier, region=budget["region"], residency=residency,
            training_tokens_upper_bound=tokens, estimated_training_cost_upper_usd=round(cost, 6),
        )
    except (ValueError, KeyError, TypeError) as exc:
        plan["blocked_reasons"].append(f"budget_or_admission: {exc}")
    return plan


def openai_client():
    from azure.identity import AzureCliCredential, get_bearer_token_provider
    from openai import OpenAI

    credential = AzureCliCredential()
    return credential, OpenAI(
        base_url=f"https://{ACCOUNT}.openai.azure.com/openai/v1/",
        api_key=get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default"),
        max_retries=0,
    )


def journal_root() -> Path:
    return Path.home() / ".foundry-frontier" / "orders-learning" / "training-journal"


def submission_journal_path(plan: dict) -> Path:
    key = digest({"approval_sha256": plan["budget_sha256"], "account": plan["account"],
                  "account_resource_id": plan["account_resource_id"], "project": plan["project"]})
    return journal_root() / (key + ".json")


def submit(manifest_path: Path, budget_path: Path, output: Path, confirmation: str,
           training_project: str | None = None) -> dict:
    if training_project != PROJECT:
        raise ValueError("Submission requires explicit --training-project " + PROJECT + "; no implicit default alias")
    plan = training_plan(manifest_path, budget_path)
    if plan["blocked_reasons"]:
        raise ValueError("; ".join(plan["blocked_reasons"]))
    if confirmation != plan["budget_sha256"]:
        raise ValueError("Submission requires --confirm equal to SHA256 of the approved budget file")
    manifest = verify_manifest(manifest_path)
    uploads = {}
    for split in ("train", "validation"):
        name = split + ".jsonl"
        data = (manifest_path.parent / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["files"][name]["sha256"]:
            raise ValueError(f"Approved artifact changed while preparing upload: {name}")
        uploads[split] = (name, data, "application/jsonl")
    # Write the receipt BEFORE any request. An ambiguous failure must be reconciled,
    # never blindly retried (which might submit/pay for the same job twice).
    receipt = {**plan, "state": "submission_started", "training_file_id": None,
               "validation_file_id": None, "job_id": None}
    marker = submission_journal_path(plan)
    if marker.exists():
        raise ValueError(f"This budget already has a submission attempt; reconcile persistent journal {marker}")
    receipt.update(journal_path=str(marker), receipt_path=str(output.resolve()))
    write_json(output, receipt)
    credential, client = openai_client()
    writes_started = False
    journal_claimed = False

    def persist():
        write_json(output, receipt, exclusive=False)
        if journal_claimed:
            write_json(marker, receipt, exclusive=False)

    try:
        with credential, client:
            for split in ("train", "validation"):
                receipt["default_project_verification"] = {
                    "state": "checking", "requested_project": training_project, "actual_default_project": None}
                receipt["default_project_verification"] = verify_training_project(
                    credential, plan["account_resource_id"], training_project, plan["region"])
                persist()
                if not writes_started:
                    # Exclusive create is the atomic cross-release/user-wide claim.
                    # A competing process cannot reach any upload using this approval.
                    write_json(marker, receipt)
                    journal_claimed = True
                writes_started = True
                uploaded = client.files.create(file=uploads[split], purpose="fine-tune")
                receipt["training_file_id" if split == "train" else "validation_file_id"] = uploaded.id
                persist()
                processed = client.files.wait_for_processing(uploaded.id, max_wait_seconds=300)
                if processed.status != "processed":
                    raise ValueError(f"{split} file was not successfully processed")
            receipt["default_project_verification"] = {
                "state": "checking", "requested_project": training_project, "actual_default_project": None}
            receipt["default_project_verification"] = verify_training_project(
                credential, plan["account_resource_id"], training_project, plan["region"])
            persist()
            response = client.fine_tuning.jobs.create(
                model=plan["model"], training_file=receipt["training_file_id"],
                validation_file=receipt["validation_file_id"], seed=105, suffix="orders-triage-v1",
                method={"type": "supervised", "supervised": {"hyperparameters": {"n_epochs": plan["epochs"]}}},
                extra_body={"trainingType": plan["training_tier"]},
            )
            receipt.update(state="submitted", job_id=response.id, job_status=response.status)
            persist()
    except Exception as exc:
        if isinstance(exc, TrainingPlacementError):
            receipt["default_project_verification"] = exc.verification
        elif receipt["default_project_verification"]["state"] == "checking":
            receipt["default_project_verification"]["state"] = "unavailable"
        receipt.update(state="submission_uncertain" if writes_started else "blocked",
                       error_type=type(exc).__name__,
                       action_required=("Inspect Foundry files/jobs and this receipt before any retry."
                                        if writes_started else "Default-project preflight failed; no files uploaded or training job created."))
        receipt["blocked_reasons"] = [str(exc)] if isinstance(exc, ValueError) else ["Account default-project preflight or submission failed"]
        persist()
        raise
    return receipt


def job_status(receipt_path: Path, output: Path) -> dict:
    receipt = read_json(receipt_path)
    if receipt.get("account") != ACCOUNT or not receipt.get("job_id"):
        raise ValueError("Receipt must identify a submitted job in the intended account")
    credential, client = openai_client()
    with credential, client:
        job = client.fine_tuning.jobs.retrieve(receipt["job_id"])
    result = {
        "schema_version": 1, "account": ACCOUNT, "project": PROJECT, "checked_at": now(),
        "state": job.status, "job_id": job.id, "fine_tuned_model": job.fine_tuned_model,
        "trained_tokens": job.trained_tokens, "result_files": job.result_files,
        "manifest_sha256": receipt["manifest_sha256"],
        "staging": {
            "state": "not_deployed", "deployment_name": STAGING_DEPLOYMENT,
            "candidate_environment": {"TRIAGE_MODEL_DEPLOYMENT": STAGING_DEPLOYMENT},
            "activation_environment": {},
            "required": "Human-approved ARM/portal staging deployment, frozen-test comparison and all four eval tiers.",
        },
    }
    write_json(output, result)
    return result


def publish_dataset(manifest_path: Path, output: Path, execute: bool, confirmation: str | None) -> dict:
    manifest = verify_manifest(manifest_path)
    plan = {
        "schema_version": 1, "state": "dry_run", "project_endpoint": PROJECT_ENDPOINT,
        "dataset_name": manifest["dataset_name"], "dataset_version": manifest["version"],
        "manifest_sha256": file_hash(manifest_path),
        "operations": [{"name": manifest["dataset_name"] + "-" + split, "version": manifest["version"],
                        "file_path": str(manifest_path.parent / (split + ".jsonl"))}
                       for split in ("train", "validation", "test")],
    }
    if not execute:
        write_json(output, plan)
        return plan
    verify_approval(manifest_path)
    if confirmation != file_hash(manifest_path):
        raise ValueError("Publishing requires exact approved manifest SHA confirmation")
    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import AzureCliCredential

    plan.update(state="publication_started", published=[])
    write_json(output, plan)
    with AzureCliCredential() as credential, AIProjectClient(PROJECT_ENDPOINT, credential=credential) as project:
        # create_or_update/upload_file can overwrite: explicitly reject existing versions.
        for operation in plan["operations"]:
            try:
                project.datasets.get(name=operation["name"], version=operation["version"])
            except ResourceNotFoundError:
                continue
            raise ValueError("Native dataset version already exists; choose a new version, never overwrite it")
        for operation in plan["operations"]:
            dataset = project.datasets.upload_file(**operation)
            plan["published"].append({"name": operation["name"], "version": operation["version"], "id": dataset.id})
            write_json(output, plan, exclusive=False)
    plan["state"] = "published"
    write_json(output, plan, exclusive=False)
    return plan
