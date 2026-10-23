"""Offline audit/prepare/consent validation. No Azure, cache refresh, upload or training."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipelines" / "insurance-learning"))
from evals.insurance import FIXTURE
from evals.insurance_review import approval_template, audit_pack, read_json, sha256, verify_pack, verify_receipt, write_new
from insurance_learning import SOURCE_PACK, prepare


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pack", type=Path, default=SOURCE_PACK)
    parser.add_argument("--prepare", type=Path, help="NEW immutable v2 pack directory")
    parser.add_argument("--pack", type=Path, help="Pack to verify for approval or upload")
    parser.add_argument("--output", type=Path, help="NEW audit report file")
    parser.add_argument("--approval-template", type=Path, help="NEW unapproved JSON template; never records approval")
    parser.add_argument("--approval-receipt", type=Path, help="Actual explicit user attestation, filled by operator after confirmation")
    parser.add_argument("--artifact", action="append", help="Exact manifest artifact name (repeatable)")
    parser.add_argument("--operation", choices=("manual_upload", "evaluate"), default="manual_upload")
    args = parser.parse_args(argv)
    try:
        if args.prepare:
            result = prepare(args.prepare, args.source_pack)
        elif args.approval_template:
            if not args.pack:
                raise ValueError("--pack is required")
            result = approval_template(args.pack, args.artifact)
            write_new(args.approval_template, result)
        elif args.approval_receipt:
            if not args.pack or not args.artifact:
                raise ValueError("--pack and --artifact required")
            receipt = verify_receipt(args.pack, read_json(args.approval_receipt), args.artifact, args.operation)
            manifest = verify_pack(args.pack)
            result = {
                "state": "approved_bytes_verified_not_uploaded",
                "approval_receipt_sha256": sha256(args.approval_receipt), "approver": receipt["approver"],
                "scope": "evaluation_only", "no_training": True, "individual_human_annotations": 0,
                "artifacts": [{"path": str(args.pack / name), "sha256": manifest["files_sha256"][name],
                               "bytes": (args.pack / name).stat().st_size} for name in args.artifact],
                "manual_upload": "Parent/user uploads exact approved bytes through Foundry Data Upload. "
                                 "Verify remote read-back hash; no network-policy workaround.",
            }
        else:
            result = audit_pack(args.source_pack, FIXTURE)
        if args.output:
            write_new(args.output, result)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"state": "blocked", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
