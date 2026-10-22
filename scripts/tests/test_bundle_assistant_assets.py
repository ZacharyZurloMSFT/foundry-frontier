import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("bundle_assistant", ROOT / "scripts" / "bundle-assistant-assets.py")
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def test_assistant_prompts_are_bundled_with_provenance(monkeypatch):
    copies, writes = {}, {}
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bundle.shutil, "copyfile", lambda src, dest: copies.__setitem__(dest.name, src.read_bytes()))
    monkeypatch.setattr(Path, "write_text", lambda path, text, **_: writes.__setitem__(path.name, text))
    stamp = bundle.bundle()
    assert set(copies) == {"concierge.yaml", "help-center.yaml"}
    assert stamp["git_sha"] == "a" * 40
    assert set(stamp) == {"git_sha", "repo", "prompt_sha.assistant-concierge", "prompt_sha.assistant-help-center"}
    for name, surface in (("concierge", "concierge"), ("help-center", "help_center")):
        assert stamp[f"prompt_sha.assistant-{name}"] == hashlib.sha256(
            copies[f"{name}.yaml"].decode().replace("\r\n", "\n").encode()).hexdigest()
        assert f"surface: {surface}".encode() in copies[f"{name}.yaml"] and b"business_domain: insurance" in copies[f"{name}.yaml"]
    assert b"index: nw-insurance-help-customer-v1" in copies["help-center.yaml"]
    assert json.loads(writes["build.json"]) == stamp


def test_invalid_build_sha_fails_before_staging(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "not-a-commit")
    monkeypatch.setattr(bundle.shutil, "copyfile", lambda *_: pytest.fail("Must validate before copying"))
    with pytest.raises(ValueError, match="commit hash"):
        bundle.bundle()
