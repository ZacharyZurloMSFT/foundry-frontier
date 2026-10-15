"""Smoke-test the Northwind manifest + all per-department content packs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

# Allow running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from data.northwind import load_company  # noqa: E402


def main() -> int:
    c = load_company()
    print(f"company: {c.name}")
    errors: list[str] = []

    for d in c.departments:
        print(f"\n[{d.slug}] project={d.foundry_project}")

        if not d.seed_dir.exists():
            errors.append(f"{d.slug}: seed_dir missing")
        else:
            items = [p for p in d.seed_dir.iterdir() if p.name != ".gitkeep"]
            print(f"  seed: {len(items)} items")

        if d.golden_path.exists():
            try:
                rows = [
                    json.loads(line)
                    for line in d.golden_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                print(f"  golden: {len(rows)} rows")
            except json.JSONDecodeError as exc:
                errors.append(f"{d.slug}: golden not valid JSONL — {exc}")
        else:
            print("  golden: (none)")

        if d.prompts_dir.exists():
            yamls = list(d.prompts_dir.glob("*.yaml"))
            for y in yamls:
                try:
                    with open(y, encoding="utf-8") as fh:
                        doc = yaml.safe_load(fh)
                    name = doc.get("name") if isinstance(doc, dict) else None
                    print(f"  prompt: {name or y.name}")
                except yaml.YAMLError as exc:
                    errors.append(f"{d.slug}: {y.name} — {exc}")

    if errors:
        print("\nERRORS:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("\nAll manifest content OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
