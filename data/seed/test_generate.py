"""The seed generator is offline and insurance-only (CI runs `generate.py --domain insurance`)."""
import importlib.util
import json
from pathlib import Path

import pytest

GENERATE = Path(__file__).resolve().parent / "generate.py"


@pytest.fixture
def generate(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("seed_generate", GENERATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "OUT", tmp_path / "generated")
    monkeypatch.setattr(module, "MANIFEST", tmp_path / "generated" / "manifest.json")
    return module


@pytest.mark.parametrize("domain", ["other", "Insurance", ""])
def test_only_the_insurance_domain_is_accepted(generate, monkeypatch, domain, capsys):
    monkeypatch.setattr(generate, "generate_insurance", lambda: pytest.fail("an invalid domain must not generate"))
    with pytest.raises(SystemExit) as raised:
        generate.main(["--domain", domain])
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--domain", "insurance"]])
def test_insurance_is_the_default_domain(generate, monkeypatch, argv):
    calls = []
    monkeypatch.setattr(generate, "generate_insurance", lambda: calls.append("insurance"))
    generate.main(argv)
    assert calls == ["insurance"]


def test_generation_writes_only_the_synthetic_insurance_corpus(generate):
    pytest.importorskip("reportlab")
    generate.main(["--domain", "insurance"])
    manifest = json.loads(generate.MANIFEST.read_text(encoding="utf-8"))
    assert manifest and {entry["business_domain"] for entry in manifest} == {"insurance"}
    assert all(entry["fictional"] is True for entry in manifest)
    assert {"nw-auto-wa-2026-v1", "nw-record-clm-2001"} <= {entry["id"] for entry in manifest}
    assert {"product", "precedent"}.isdisjoint(entry["doc_type"] for entry in manifest)
    for entry in manifest:
        assert (generate.OUT / Path(entry["path"]).name).stat().st_size > 0
