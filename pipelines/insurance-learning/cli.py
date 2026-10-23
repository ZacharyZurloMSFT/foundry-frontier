"""Local-only insurance dataset preparation; no training or dataset mutation HTTP endpoint.

`prepare` writes a NEW immutable, unreviewed candidate pack. It never calls Azure,
uploads, annotates or trains. Approval templates, receipt verification and audits stay
in scripts/review-insurance-datasets.py: a prepared pack pins that script's source hash,
so approvals must be verified there rather than through a second, unpinned path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from insurance_learning import ROOT, prepare


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    candidates = commands.add_parser(
        "prepare", aliases=["prepare-insurance"],
        help="Prepare unreviewed versioned local insurance candidates; never upload/train")
    candidates.add_argument("--output", type=Path, required=True, help="NEW pack directory")
    return ap


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    sys.path.insert(0, str(ROOT))
    try:
        result = prepare(args.output)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"schema_version": 1, "state": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
