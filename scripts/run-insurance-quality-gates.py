"""Dry-run-first insurance gates. Offline scoring is NOT native evaluation.

--pack PACK --print-config-template : credential-free native smoke configuration.
--pack PACK --config CONFIG        : credential-free native SDK payload/budget plan.
--pack PACK --config CONFIG --execute --approval-receipt USER.json
  --confirm RECEIPT_SHA256 --run-receipt NEW.json : explicit paid native smoke.
--pack PACK --observations RESPONSES.jsonl [--tier smoke|all] : offline plan.
Add --execute --approval-receipt USER.json --confirm RECEIPT_SHA256 --output NEW.json
  to execute local deterministic contract scoring only. No synthetic responses generated.
Reviewed software fixes require BOTH --execution-code-review REVIEW.json and
  --execution-code-review-sha256 INDEPENDENT_PIN, plus the bound --approval-receipt.
The review is assistant code attestation, never human annotation or budget admission.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.insurance import FIXTURE
from evals.insurance_gates import THRESHOLDS, evaluate, response_contract
from evals.insurance_native import build_plan, config_template, execute
from evals.insurance_review import lineage_record, read_json, rows, sha256, verify_code_lineage, verify_receipt, write_new


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--print-config-template", action="store_true")
    parser.add_argument("--print-response-contract", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--tier", choices=("smoke", "all"), default="smoke")
    parser.add_argument("--execute", "--apply", dest="execute", action="store_true")
    parser.add_argument("--approval-receipt", type=Path)
    parser.add_argument("--execution-code-review", type=Path, help="Versioned assistant code-review receipt outside PACK")
    parser.add_argument("--execution-code-review-sha256", help="Independently reviewed exact code-review receipt SHA256")
    parser.add_argument("--confirm", help="Exact SHA256 of actual user approval receipt")
    parser.add_argument("--run-receipt", type=Path, help="NEW native execution journal; existing paths cannot replay")
    parser.add_argument("--output", type=Path, help="NEW plan or offline report file")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        review_options = {
            "execution_code_review": args.execution_code_review,
            "execution_code_review_sha256": args.execution_code_review_sha256,
        }
        manifest = verify_code_lineage(args.pack, ROOT, approval_receipt_path=args.approval_receipt, **review_options)
        if sha256(FIXTURE) != manifest.get("fixture_sha256"):
            raise ValueError("Golden fixture hash changed; prepare/approve a new version")
        if args.config and args.observations:
            raise ValueError("Choose native config or offline observations, never both")
        if args.print_config_template or args.print_response_contract:
            if args.execute:
                raise ValueError("Template printing never executes")
            result = config_template(args.pack) if args.print_config_template else response_contract()
        elif args.config:
            config = read_json(args.config)
            if args.execute:
                if not args.approval_receipt or not args.run_receipt or not args.confirm:
                    raise ValueError("Native execution requires --approval-receipt --confirm --run-receipt NEW")
                if not 1 <= args.timeout_seconds <= 900:
                    raise ValueError("Timeout must be 1..900 seconds")
                result = execute(args.pack, config, args.approval_receipt, args.run_receipt,
                                 args.confirm, timeout_seconds=args.timeout_seconds, **review_options)
            else:
                approval = read_json(args.approval_receipt) if args.approval_receipt else None
                result = build_plan(args.pack, config, approval, approval_receipt_path=args.approval_receipt,
                                    **review_options)
        else:
            selected = [case for case in rows(args.pack / "evaluation-candidates.jsonl")
                        if args.tier == "all" or case["tier"] == "smoke"]
            if args.execute:
                if not args.observations or not args.approval_receipt or not args.output or not args.confirm:
                    raise ValueError("Offline execution requires observations, approval receipt, exact confirmation and NEW output")
                if args.confirm != sha256(args.approval_receipt):
                    raise ValueError("User receipt confirmation hash mismatch")
                verify_receipt(args.pack, read_json(args.approval_receipt), ["evaluation-candidates.jsonl"], "evaluate")
                result = evaluate(selected, rows(args.observations), read_json(FIXTURE))
                result.update(manifest_sha256=sha256(args.pack / "manifest.json"),
                              fixture_sha256=sha256(FIXTURE), observations_sha256=sha256(args.observations),
                              approval_receipt_sha256=sha256(args.approval_receipt),
                              source_code_sha256=manifest["source_code_sha256"], **lineage_record(manifest))
            else:
                result = {
                    "state": "dry_run", "execution_kind": "offline_contract_plan_not_native_run",
                    "selected_cases": len(selected), "tier": args.tier, "thresholds": THRESHOLDS,
                    "release_approved": False, "training_approved": False,
                    "required": ["Explicit user acceptance of exact evaluation artifact hash.",
                                 "Actual saved responses, server tool traces and per-workflow queue evidence.",
                                 "--execute --approval-receipt --confirm --observations --output NEW"],
                    "response_contract": response_contract(),
                    "manifest_sha256": sha256(args.pack / "manifest.json"),
                    "source_code_sha256": manifest["source_code_sha256"], **lineage_record(manifest),
                }
        if args.output:
            write_new(args.output, result)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.execute and args.observations and not result["contract_passed"]:
            return 1
        return 0
    except Exception as exc:
        # Native request exceptions can contain credentials; show only offline validation messages.
        message = str(exc) if isinstance(exc, (ValueError, OSError, KeyError, TypeError)) and not args.config else type(exc).__name__
        print(json.dumps({"state": "blocked", "error": message, "release_approved": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
