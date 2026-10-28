"""Allowlisted, deterministic hosted-agent packaging; no Azure dependencies."""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

PACKAGE_SOURCES = (
    "agents/__init__.py", "agents/hosted/__init__.py",
    "agents/hosted/config.py", "agents/hosted/main.py", "agents/hosted/runtime.py",
    "agents/hosted/scope.py", "agents/hosted/tools.py",
    "agents/insurance/__init__.py", "agents/insurance/models.py",
    "agents/insurance/triage.yaml", "agents/insurance/policy_claim_lookup.yaml",
    "agents/insurance/policy_wording.yaml", "agents/insurance/claim_action_draft.yaml",
    "agents/insurance/orchestrator.yaml", "data/northwind/insurance-fixtures.json",
)


def entries(root: Path, requirements: Path) -> dict[str, bytes]:
    return {
        "main.py": (root / "agents" / "orchestrator" / "main.py").read_bytes(),
        "requirements.txt": requirements.read_bytes(),
        **{name: root.joinpath(*name.split("/")).read_bytes() for name in PACKAGE_SOURCES},
        "agents/insurance/domain.py": (root / "apps" / "api" / "app" / "insurance_domain.py").read_bytes(),
        "agents/insurance/workflow_binding.py": (root / "apps" / "api" / "app" / "workflow_binding.py").read_bytes(),
    }


def bundle(root: Path, requirements: Path, destination: Path) -> str:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries(root, requirements).items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
    return hashlib.sha256(destination.read_bytes()).hexdigest()
