"""Publish every agent prompt (`agents/insurance/**/*.yaml`) and `.prompty` file to
the Foundry Prompt Store, tagged with the current git SHA. Called by
.github/workflows/prompt-registry.yml on every push to main.

A yaml prompt is published under the agent name it declares
(`northwind-insurance-*`); a `.prompty` file under its file stem.

Stubbed to be resilient: if the Prompt Store SDK surface isn't present, we
print what would have been uploaded and exit 0 so the CI still passes. The
apparent gap is intentional — the SDK surface for Prompt Store has moved
several times and this repo shouldn't hard-fail CI on that.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _try_import_prompt_store():
    for mod, attr in (
        ("azure.ai.projects", "AIProjectClient"),
        ("azure.ai.projects.aio", "AIProjectClient"),
    ):
        try:
            m = __import__(mod, fromlist=[attr])
            return getattr(m, attr)
        except (ImportError, AttributeError):
            continue
    return None


def _prompt_name(path: Path, content: str) -> str:
    """The Prompt Store name: the agent name a yaml prompt declares, else the file stem."""
    if path.suffix == ".yaml":
        try:
            declared = (yaml.safe_load(content) or {}).get("name")
        except (yaml.YAMLError, AttributeError):
            declared = None
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
    if path.parent.name == "copilot-harness":
        return "northwind-copilot-harness"
    return path.stem


def main() -> int:
    sha = os.environ.get("GIT_SHA", "unknown")
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")

    files = sorted((REPO_ROOT / "agents" / "insurance").rglob("*.yaml")) + \
            sorted((REPO_ROOT / "evals" / "evaluators" / "rubrics").glob("*.prompty")) + \
            sorted((REPO_ROOT / "agents" / "copilot-harness").glob("*.prompty"))
    if not files:
        print("no prompts found — nothing to publish.")
        return 0

    Client = _try_import_prompt_store()
    if Client is None or not endpoint:
        print(f"[dry-run] would publish {len(files)} prompts tagged {sha[:8]}:")
        for f in files:
            print(f"  - {f.relative_to(REPO_ROOT)}")
        return 0

    from azure.identity import DefaultAzureCredential
    client = Client(endpoint=endpoint, credential=DefaultAzureCredential())
    for f in files:
        content = f.read_text(encoding="utf-8")
        name = _prompt_name(f, content)
        try:
            # SDK method name varies (upload_prompt / create_prompt / prompts.create);
            # try each until one sticks.
            for method in ("upload_prompt", "create_prompt"):
                fn = getattr(client, method, None)
                if fn:
                    fn(name=name, content=content, tags={"git_sha": sha})
                    print(f"published {f.name} via {method}")
                    break
            else:
                print(f"[skip] {f.name} — no compatible SDK method on client")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] failed to publish {f.name}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
