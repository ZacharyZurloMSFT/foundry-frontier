import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("bundle_harness", ROOT / "scripts" / "bundle-harness-assets.py")
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def test_canonical_harness_prompt_and_support_are_bundled_with_provenance(monkeypatch):
    copies, writes = {}, {}
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bundle.shutil, "copyfile", lambda src, dest: copies.__setitem__(dest.name, src.read_bytes()))
    monkeypatch.setattr(Path, "write_text", lambda path, text, **_: writes.__setitem__(path.name, text))
    stamp = bundle.bundle()
    assert set(copies) == {"instructions.prompty", "northwind-adjuster-support.yaml", "concierge.yaml", "help-center.yaml"}
    assert set(stamp) == {"git_sha", "repo", "prompt_sha.copilot-harness", "prompt_sha.adjuster-support",
                          "prompt_sha.concierge", "prompt_sha.help_center"}
    assert stamp["git_sha"] == "a" * 40
    assert stamp["prompt_sha.copilot-harness"] == hashlib.sha256(
        copies["instructions.prompty"].decode().replace("\r\n", "\n").encode()).hexdigest()
    assert stamp["prompt_sha.adjuster-support"] == hashlib.sha256(
        copies["northwind-adjuster-support.yaml"].decode().replace("\r\n", "\n").encode()).hexdigest()
    for surface, filename in (("concierge", "concierge.yaml"), ("help_center", "help-center.yaml")):
        assert stamp[f"prompt_sha.{surface}"] == hashlib.sha256(
            copies[filename].decode().replace("\r\n", "\n").encode()).hexdigest()
    assert json.loads(writes["build.json"]) == stamp
    assert b"business_domain: insurance" in copies["instructions.prompty"]


def test_only_the_insurance_harness_prompt_exists_to_be_bundled():
    assert sorted(path.name for path in (ROOT / "agents" / "copilot-harness").iterdir()) == ["instructions.prompty"]


def test_a_harness_prompt_for_another_domain_fails_before_staging(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(bundle.shutil, "copyfile", lambda *_: pytest.fail("Must validate before copying"))
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: (
        original(path, *args, **kwargs).replace("business_domain: insurance\n", "business_domain: other\n")
        if path.name == "instructions.prompty" else original(path, *args, **kwargs)))
    with pytest.raises(ValueError, match="invalid business domain"):
        bundle.bundle()


def test_invalid_build_sha_fails_before_staging(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "not-a-commit")
    monkeypatch.setattr(bundle.shutil, "copyfile", lambda *_: pytest.fail("Must validate before copying"))
    with pytest.raises(ValueError, match="commit hash"):
        bundle.bundle()


@pytest.mark.parametrize("filename,old,new,message", [
    ("concierge.yaml", "surface: concierge", "surface: help_center", "surface"),
    ("concierge.yaml", "business_domain: insurance", "business_domain: other", "insurance domain"),
    ("help-center.yaml", "instructions: |", "description: |", "instructions"),
    ("help-center.yaml", "index: nw-insurance-help-customer-v1", "index: nw-insurance-adjuster-support", "isolated customer index"),
])
def test_invalid_assistant_prompt_fails_before_staging(monkeypatch, filename, old, new, message):
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(bundle.shutil, "copyfile", lambda *_: pytest.fail("Must validate before copying"))
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: (
        original(path, *args, **kwargs).replace(old, new)
        if path.name == filename else original(path, *args, **kwargs)))
    with pytest.raises(ValueError, match=message):
        bundle.bundle()
