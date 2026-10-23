import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from insurance_learning import INTENTS, SOURCE_PACK, examples, prepare
from evals.insurance_review import audit_pack, normalized_input, sha256, valid_labels, verify_pack
from evals.insurance import FIXTURE


def test_synthetic_insurance_labels_are_unreviewed_and_family_disjoint():
    rows = examples()
    assert len(rows) == 24 and all(row["review_status"] == "unreviewed" for row in rows)
    entities = {}
    families = {}
    for split in ("train", "validation", "test"):
        selected = [r for r in rows if r["split"] == split]
        assert {r["expected"]["intent"] for r in selected} == set(INTENTS)
        entities[split] = {r["expected"][field] for r in selected for field in ("claim_id", "policy_id", "policyholder_id")}
        entities[split].discard(None)
        families[split] = {r["family"] for r in selected}
        assert all(r["provenance"]["contains_policy_text"] is False for r in selected)
        assert all(valid_labels(r["expected"]) and r["task"] == "extract_unknown_ids_not_lookup" for r in selected)
    assert not entities["train"] & entities["test"]
    assert not entities["validation"] & entities["test"]
    assert not families["train"] & families["test"]
    assert not families["validation"] & families["test"]
    assert len({normalized_input(r["input"]) for r in rows}) == len(rows)
    assert sum(r["expected"]["claim_id"] is None for r in rows) == 4


def test_preparation_is_not_native_annotation_training_or_model_readiness(tmp_path):
    path = tmp_path / "insurance-v2"
    original = {p.name: sha256(p) for p in SOURCE_PACK.iterdir() if p.is_file()}
    manifest = prepare(path)
    assert manifest["state"] == "prepared_unreviewed" and manifest["training_status"] == "not_submitted"
    assert manifest["native_annotation_status"] == "not_created"
    assert manifest["native_evaluation_status"] == "not_run"
    assert manifest["insurance_model_ready"] is False
    assert manifest["earlier_fine_tunes_compatible"] is False
    assert manifest["scope"] == "evaluation_only" and manifest["no_training"] is True
    assert manifest["human_annotations"] == 0 and manifest["user_approval"] == "not_requested"
    assert verify_pack(path) == manifest
    for name, digest in manifest["files_sha256"].items():
        assert hashlib.sha256((path / name).read_bytes()).hexdigest() == digest
    assert len((path / "evaluation-candidates.jsonl").read_text().splitlines()) >= 10
    assert not list(path.glob("suggested-*.jsonl"))
    assert len((path / "native-smoke-triage.jsonl").read_text().splitlines()) == 2
    assert len((path / "native-smoke-lookup.jsonl").read_text().splitlines()) == 3
    assert len((path / "native-smoke-policy_wording.jsonl").read_text().splitlines()) == 3
    assert {p.name: sha256(p) for p in SOURCE_PACK.iterdir() if p.is_file()} == original
    with pytest.raises(FileExistsError):
        prepare(path)


def test_existing_v1_audit_is_honest_and_detects_hidden_leakage():
    report = audit_pack(SOURCE_PACK, FIXTURE)
    assert report["user_approved"] is False and report["qualified_insurance_review"] is False
    assert report["facts"]["annotation_rows"] == 24 and report["facts"]["evaluation_rows"] == 11
    assert report["facts"]["leaking_template_groups"] == 8
    assert report["facts"]["declared_family_cross_split_groups"] == 0
    assert report["facts"]["unknown_id_annotation_rows"] == 24
    assert report["facts"]["canonical_clauses"] == 6 and report["facts"]["side_effectful_evaluation_rows"] == 4
    assert all(value["matches"] for value in report["hash_checks"].values())
    assert all(report["facts"]["suggested_sft_label_parity"].values())
    assert "normalized_template_cross_split_leakage" in {f["code"] for f in report["findings"]}


def test_manifest_changed_bytes_fail_closed(tmp_path):
    pack = tmp_path / "pack"
    prepare(pack)
    (pack / "evaluation-candidates.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_pack(pack)
