"""Package canonical harness instructions and immutable build provenance."""
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
    sources = [root / "agents" / "copilot-harness" / name
               for name in ("instructions.prompty", "retail.instructions.prompty")]
    contents = [source.read_text(encoding="utf-8") for source in sources]
    for domain, content in zip(("insurance", "retail"), contents):
        if f"business_domain: {domain}\n" not in content:
            raise ValueError("Canonical harness prompt has an invalid business domain.")
    support = (root / "data" / "northwind" / "departments" / "employee-help-desk"
               / "prompts" / "northwind-adjuster-support.yaml")
    support_content = support.read_text(encoding="utf-8")
    if "index: nw-insurance-adjuster-support\n" not in support_content:
        raise ValueError("Adjuster support prompt must target its isolated index.")
    assistants = sorted((root / "agents" / "insurance" / "assistants").glob("*.yaml"))
    assistant_contents = [prompt.read_text(encoding="utf-8") for prompt in assistants]
    if not assistants or any(
            "business_domain: insurance\n" not in content or f"surface: {prompt.stem.replace('-', '_')}\n" not in content
            for prompt, content in zip(assistants, assistant_contents)):
        raise ValueError("Assistant prompts must declare their surface and the insurance domain.")
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        result = subprocess.run(["git", "--no-pager", "rev-parse", "HEAD"],
                                cwd=root, capture_output=True, text=True, check=False)
        sha = result.stdout.strip() if result.returncode == 0 else "local"
    if sha != "local" and not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ValueError("Build SHA must be a commit hash.")
    stamp = {
        "git_sha": sha, "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "prompt_sha.copilot-harness": hashlib.sha256(contents[0].encode()).hexdigest(),
        "prompt_sha.copilot-harness-retail": hashlib.sha256(contents[1].encode()).hexdigest(),
        "prompt_sha.adjuster-support": hashlib.sha256(support_content.encode()).hexdigest(),
        **{f"prompt_sha.assistant-{prompt.stem}": hashlib.sha256(content.encode()).hexdigest()
           for prompt, content in zip(assistants, assistant_contents)},
    }
    destination = root / "apps" / "api" / "app" / "_agent_prompts"
    destination.mkdir(parents=True, exist_ok=True)
    for source in [*sources, support, *assistants]:
        shutil.copyfile(source, destination / source.name)
    (destination / "build.json").write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    return stamp


if __name__ == "__main__":
    print(json.dumps(bundle()))
