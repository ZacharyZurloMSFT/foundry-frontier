"""Package the assistant prompts and immutable build provenance into the API image."""
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
    assistants = sorted((root / "agents" / "insurance" / "assistants").glob("*.yaml"))
    contents = [prompt.read_text(encoding="utf-8") for prompt in assistants]
    if not assistants or any(
            "business_domain: insurance\n" not in content or f"surface: {prompt.stem.replace('-', '_')}\n" not in content
            for prompt, content in zip(assistants, contents)):
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
        **{f"prompt_sha.assistant-{prompt.stem}": hashlib.sha256(content.encode()).hexdigest()
           for prompt, content in zip(assistants, contents)},
    }
    destination = root / "apps" / "api" / "app" / "_agent_prompts"
    destination.mkdir(parents=True, exist_ok=True)
    for source in assistants:
        shutil.copyfile(source, destination / source.name)
    (destination / "build.json").write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    return stamp


if __name__ == "__main__":
    print(json.dumps(bundle()))
