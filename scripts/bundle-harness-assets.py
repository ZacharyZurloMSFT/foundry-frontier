"""Package canonical harness, support and customer assistant prompts with immutable provenance."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bundle(root: Path = ROOT) -> dict[str, str]:
    harness = root / "agents" / "copilot-harness" / "instructions.prompty"
    harness_content = harness.read_text(encoding="utf-8")
    if "business_domain: insurance\n" not in harness_content:
        raise ValueError("Canonical harness prompt has an invalid business domain.")
    support = (root / "data" / "northwind" / "departments" / "adjuster-support"
               / "prompts" / "northwind-adjuster-support.yaml")
    support_content = support.read_text(encoding="utf-8")
    if "index: nw-insurance-adjuster-support\n" not in support_content:
        raise ValueError("Adjuster support prompt must target its isolated index.")
    assistants = {}
    for surface, filename in (("concierge", "concierge.yaml"), ("help_center", "help-center.yaml")):
        source = root / "agents" / "insurance" / "assistants" / filename
        content = source.read_text(encoding="utf-8")
        if ("business_domain: insurance\n" not in content or f"surface: {surface}\n" not in content
                or "instructions: |\n" not in content):
            raise ValueError("Assistant prompt must declare its surface, insurance domain and instructions.")
        if surface == "help_center" and "index: nw-insurance-help-customer-v1\n" not in content:
            raise ValueError("Help Center prompt must target its isolated customer index.")
        assistants[surface] = (source, content)
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        result = subprocess.run(["git", "--no-pager", "rev-parse", "HEAD"],
                                cwd=root, capture_output=True, text=True, check=False)
        sha = result.stdout.strip() if result.returncode == 0 else "local"
    if sha != "local" and not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ValueError("Build SHA must be a commit hash.")
    stamp = {
        "git_sha": sha, "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "prompt_sha.copilot-harness": hashlib.sha256(harness_content.encode()).hexdigest(),
        "prompt_sha.adjuster-support": hashlib.sha256(support_content.encode()).hexdigest(),
    }
    stamp.update({f"prompt_sha.{surface}": hashlib.sha256(content.encode()).hexdigest()
                  for surface, (_, content) in assistants.items()})
    destination = root / "apps" / "api" / "app" / "_agent_prompts"
    destination.mkdir(parents=True, exist_ok=True)
    for source in (harness, support, *(source for source, _ in assistants.values())):
        shutil.copyfile(source, destination / source.name)
    (destination / "build.json").write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    return stamp


if __name__ == "__main__":
    print(json.dumps(bundle()))
