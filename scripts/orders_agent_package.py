"""Allowlisted, deterministic hosted-agent packaging; no Azure dependencies."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

PACKAGE_SOURCES = (
    "agents/__init__.py", "agents/hosted/__init__.py",
    "agents/hosted/config.py", "agents/hosted/main.py", "agents/hosted/runtime.py",
    "agents/hosted/scope.py", "agents/hosted/tools.py", "agents/triage.yaml",
    "agents/tech_lookup.yaml", "agents/manual_retrieval.yaml",
    "agents/work_order_draft.yaml", "agents/orchestrator/instructions.yaml",
    "agents/insurance/__init__.py", "agents/insurance/models.py",
    "agents/insurance/triage.yaml", "agents/insurance/tech_lookup.yaml",
    "agents/insurance/manual_retrieval.yaml", "agents/insurance/work_order_draft.yaml",
    "agents/insurance/orchestrator.yaml", "data/northwind/insurance-fixtures.json",
)


def entries(root: Path, requirements: Path) -> dict[str, bytes]:
    contents = {
        "main.py": (root / "agents" / "orchestrator" / "main.py").read_bytes(),
        "requirements.txt": requirements.read_bytes(),
        **{name: root.joinpath(*name.split("/")).read_bytes() for name in PACKAGE_SOURCES},
        "agents/insurance/domain.py": (root / "apps" / "api" / "app" / "insurance_domain.py").read_bytes(),
        "agents/insurance/config.py": (root / "apps" / "api" / "app" / "insurance_config.py").read_bytes(),
    }
    source_commit = None
    source_clean = False
    if (root / ".git").exists():
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        source_clean = not subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
    manifest = {
        "schema_version": 1, "source_commit": source_commit, "source_clean": source_clean,
        "files_sha256": {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()},
    }
    contents["agents/hosted/build.json"] = json.dumps(manifest, sort_keys=True).encode()
    return contents


def bundle(root: Path, requirements: Path, destination: Path) -> str:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries(root, requirements).items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def stage(root: Path, output: Path) -> None:
    allowed = {root / "agents" / role for role in (
        "orchestrator", "triage", "orderlookup", "policylookup", "resolutiondraft"
    )}
    if output.resolve() not in {p.resolve() for p in allowed}:
        raise ValueError("Package output must be an Orders agent service directory")
    contents = entries(root, root / "agents" / "hosted" / "requirements.txt")
    nested = output / "agents"
    if nested.exists():
        extras = [p for p in nested.rglob("*") if p.is_file()
                  and p.relative_to(output).as_posix() not in contents]
        if extras:
            raise ValueError(f"Unexpected files in generated package: {extras}")
    for name, content in contents.items():
        target = output.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stage(Path(__file__).resolve().parents[1], args.output)
    print("Prepared canonical retail and insurance hosted-agent files.")
