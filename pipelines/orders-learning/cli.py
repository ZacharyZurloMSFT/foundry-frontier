"""Approval-first Orders learning commands and read-only status contract."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from curation import (
    ACCOUNT, PROJECT, PROJECT_ENDPOINT, ROOT, STAGING_DEPLOYMENT, approve_dataset,
    export, file_hash, import_examples, jsonl, materialize, now, read_json, review, verify_approval,
    verify_manifest, write_json,
)
from evaluation import evaluate, predict_live
from native_evaluation import run_native
from seed import examples
from training import budget_template, job_status, publish_dataset, submit, training_plan

DEFAULT_WORKSPACE = ROOT / "pipelines" / "orders-learning" / "workspace"


def status(workspace: Path, manifest_path: Path | None = None,
           evaluation_path: Path | None = None, training_path: Path | None = None) -> dict:
    rows, records = materialize(workspace)
    result = {
        "schema_version": 1, "generated_at": now(), "account": ACCOUNT, "project": PROJECT,
        "project_endpoint": PROJECT_ENDPOINT,
        "counts": {state: sum(r["review_status"] == state for r in rows)
                   for state in ("unreviewed", "accepted", "rejected")},
        "review_revisions": len(records),
        "dataset": {"state": "not_exported", "version": None, "manifest_sha256": None},
        "evaluation": {"state": "not_run", "metrics": None},
        "training": {"state": "not_submitted", "job_id": None},
        "training_placement": {"state": "not_verified", "requested_project": PROJECT,
                               "actual_default_project": None,
                               "requirement": "Live ARM account.properties.defaultProject must equal northwind-orders before any training upload/job."},
        "staging": {"state": "not_deployed", "deployment_name": STAGING_DEPLOYMENT,
                    "activation_environment": {}},
        "native_annotations": {"state": "manual_review_required", "portal": "https://ai.azure.com",
                               "note": "Native Human Evaluation/trace annotations are not fabricated from local reviews."},
        "blocked_reasons": [],
    }
    result["counts"].update({split: sum(r["split"] == split and r["review_status"] == "accepted" for r in rows)
                             for split in ("train", "validation", "test")})
    promotion_path = workspace / "default-promotion.json"
    if promotion_path.exists():
        promotion = read_json(promotion_path)
        if promotion.get("account") != ACCOUNT or promotion.get("requested_project") != PROJECT:
            raise ValueError("Default promotion report belongs to a different account/project")
        result["default_project_promotion"] = promotion
        if promotion.get("state") == "blocked":
            result["blocked_reasons"].append("Account default-project promotion is blocked (operator-reported; not live authorization)")
    if manifest_path:
        manifest = verify_manifest(manifest_path)
        result["dataset"].update(state="awaiting_approval", version=manifest["version"],
                                 manifest_sha256=file_hash(manifest_path), counts=manifest["counts"],
                                 frozen_test_sha256=manifest["frozen_test_sha256"])
        try:
            approval = verify_approval(manifest_path)
            result["dataset"].update(state="approved", approval=approval)
        except (ValueError, OSError):
            result["blocked_reasons"].append("Dataset release requires explicit human approval")
    else:
        result["blocked_reasons"].append("No reviewed dataset release selected")
    for path, key in ((evaluation_path, "evaluation"), (training_path, "training")):
        if path:
            artifact = read_json(path)
            if not manifest_path or artifact.get("manifest_sha256") != file_hash(manifest_path):
                raise ValueError(f"{key} artifact does not match the selected dataset manifest")
            result[key] = {k: v for k, v in artifact.items() if k not in ("rows", "operations")}
            result[key]["artifact_sha256"] = file_hash(path)
            if key == "training":
                result["training_placement"] = artifact.get("default_project_verification", result["training_placement"])
                result["blocked_reasons"].extend(artifact.get("blocked_reasons", []))
    if result["counts"]["unreviewed"]:
        result["blocked_reasons"].append("Synthetic/source examples remain unreviewed; only accepted rows can export")
    if result["training"]["state"] == "not_submitted":
        result["blocked_reasons"].append("No training submitted; budget/model-admission approval still required")
    if result["training_placement"]["state"] != "verified":
        result["blocked_reasons"].append("Training placement is blocked until the account default project is verified as northwind-orders")
    if not result["evaluation"].get("passed"):
        result["blocked_reasons"].append("No passing candidate-vs-baseline frozen-test evaluation selected")
    result["blocked_reasons"].append("Staging deployment and four-tier evaluation promotion require a human")
    return result


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    commands = ap.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create 44 synthetic unreviewed examples; never calls Azure")
    insurance = commands.add_parser("prepare-insurance", help="Prepare unreviewed versioned local insurance candidates; never upload/train")
    insurance.add_argument("--output", type=Path, required=True)
    import_cmd = commands.add_parser("import", help="Append canonical redacted examples as unreviewed")
    import_cmd.add_argument("--source", type=Path, required=True)
    report = commands.add_parser("status", help="Read-only JSON status, no credentials/network")
    report.add_argument("--manifest", type=Path)
    report.add_argument("--evaluation", type=Path)
    report.add_argument("--training", type=Path)
    report.add_argument("--output", type=Path)
    rev = commands.add_parser("review", help="Append one named human decision/correction")
    rev.add_argument("--example-id", required=True)
    rev.add_argument("--decision", required=True, choices=("accepted", "rejected"))
    rev.add_argument("--reviewer", required=True)
    rev.add_argument("--reason", required=True)
    rev.add_argument("--corrected-labels", type=Path)
    rev.add_argument("--annotation-ref", help="Native trace/annotation or Human Evaluation reference for provenance")
    rev.add_argument("--confirm-reviewed", action="store_true", required=True)
    release = commands.add_parser("export")
    release.add_argument("--version", required=True)
    approve = commands.add_parser("approve-dataset")
    approve.add_argument("--manifest", type=Path, required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--confirm", required=True, help="Exact manifest SHA256")
    budget = commands.add_parser("budget-template")
    budget.add_argument("--manifest", type=Path, required=True)
    budget.add_argument("--output", type=Path, required=True)
    train = commands.add_parser("train", help="Dry-run unless --submit and exact budget confirmation")
    train.add_argument("--manifest", type=Path, required=True)
    train.add_argument("--budget", type=Path)
    train.add_argument("--output", type=Path, required=True)
    train.add_argument("--submit", action="store_true")
    train.add_argument("--training-project", choices=(PROJECT,), help="Required explicit project choice for --submit")
    train.add_argument("--confirm", help="Exact SHA256 of the human-approved budget document")
    ev = commands.add_parser("evaluate", help="Grade saved predictions; live inference requires explicit flags")
    ev.add_argument("--manifest", type=Path, required=True)
    ev.add_argument("--predictions", type=Path, required=True)
    ev.add_argument("--output", type=Path, required=True)
    ev.add_argument("--deployment", required=True)
    ev.add_argument("--baseline", type=Path)
    ev.add_argument("--execute-live", action="store_true")
    ev.add_argument("--confirm", help="Exact approved manifest SHA256 for live inference")
    publish = commands.add_parser("publish", help="Native versioned dataset upload; dry-run by default")
    publish.add_argument("--manifest", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    publish.add_argument("--execute", action="store_true")
    publish.add_argument("--confirm")
    native = commands.add_parser("native-evaluate", help="Documented Foundry string graders; dry-run by default")
    native.add_argument("--manifest", type=Path, required=True)
    native.add_argument("--predictions", type=Path, required=True)
    native.add_argument("--output", type=Path, required=True)
    native.add_argument("--execute", action="store_true")
    native.add_argument("--confirm")
    job = commands.add_parser("job-status", help="Explicit authenticated read of a previously submitted job")
    job.add_argument("--receipt", type=Path, required=True)
    job.add_argument("--output", type=Path, required=True)
    return ap


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare-insurance":
            sys.path.insert(0, str(ROOT))
            from insurance_learning import prepare
            result = prepare(args.output)
        elif args.command == "init":
            args.workspace.mkdir(parents=True, exist_ok=True)
            with (args.workspace / "examples.jsonl").open("xb") as stream:
                stream.write(jsonl(examples()))
            result = status(args.workspace)
        elif args.command == "status":
            result = status(args.workspace, args.manifest, args.evaluation, args.training)
            if args.output:
                # Status is a snapshot, not an append-only approval artifact.
                write_json(args.output, result, exclusive=False)
        elif args.command == "import":
            result = import_examples(args.workspace, args.source)
        elif args.command == "review":
            result = review(args.workspace, args.example_id, args.decision, args.reviewer, args.reason,
                            read_json(args.corrected_labels) if args.corrected_labels else None, args.annotation_ref)
        elif args.command == "export":
            path = export(args.workspace, args.version)
            result = {"manifest": str(path), "manifest_sha256": file_hash(path)}
        elif args.command == "approve-dataset":
            result = {"approval": str(approve_dataset(args.manifest, args.reviewer, args.confirm))}
        elif args.command == "budget-template":
            sys.path.insert(0, str(ROOT))
            from evals.insurance_review import require_training_approval
            require_training_approval(read_json(args.manifest))
            verify_manifest(args.manifest)
            result = budget_template(args.manifest)
            write_json(args.output, result)
        elif args.command == "train":
            sys.path.insert(0, str(ROOT))
            from evals.insurance_review import require_training_approval
            require_training_approval(read_json(args.manifest))
            if args.submit:
                if not args.budget or not args.confirm:
                    raise ValueError("--submit requires --budget and --confirm budget-file-SHA256")
                result = submit(args.manifest, args.budget, args.output, args.confirm, args.training_project)
            else:
                result = training_plan(args.manifest, args.budget)
                write_json(args.output, result)
        elif args.command == "publish":
            result = publish_dataset(args.manifest, args.output, args.execute, args.confirm)
        elif args.command == "job-status":
            result = job_status(args.receipt, args.output)
        elif args.command == "native-evaluate":
            result = run_native(args.manifest, args.predictions, args.output, args.execute, args.confirm)
        else:
            if args.execute_live:
                predict_live(args.manifest, args.predictions, args.deployment, args.confirm or "")
            result = evaluate(args.manifest, args.predictions, args.output, args.deployment, args.baseline)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "evaluate" and not result["passed"]:
            return 2 if all(result["gates"].values()) and result["regression_status"] == "unavailable" else 1
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"schema_version": 1, "state": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
