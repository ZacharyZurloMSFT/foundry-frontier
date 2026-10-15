import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_upload_pack as pack


def test_literal_fixture_is_read_without_executing_source(tmp_path):
    source = tmp_path / "fixture.py"
    source.write_text("raise RuntimeError('Do not import')\nORDERS = [{'id': 'fictional'}]\n")
    assert pack.literal_assignment(source, "ORDERS") == [{"id": "fictional"}]
    with pytest.raises(ValueError, match="not found"):
        pack.literal_assignment(source, "MISSING")


def test_fixture_lookups_match_real_demo_records_and_absence():
    orders = pack.literal_assignment(
        pack.ROOT / "apps" / "mcp-tech-lookup" / "app" / "main.py", "_ORDERS"
    )
    rows = pack.lookup_rows(orders)
    assert len(rows) == 50
    for row in rows:
        expected = json.loads(row["ground_truth"])
        assert expected["required_tool"] == "lookup_order"
        assert "response" not in row
        for order in expected["orders"]:
            assert order in orders
    no_match = next(row for row in rows if "NW-49999" in row["query"])
    assert json.loads(no_match["ground_truth"])["orders"] == []


def test_evaluations_do_not_fabricate_responses_or_approvals():
    orders = pack.literal_assignment(
        pack.ROOT / "apps" / "mcp-tech-lookup" / "app" / "main.py", "_ORDERS"
    )
    for rows in (pack.policy_rows(), pack.resolution_rows(orders), pack.manager_rows(orders)):
        for row in rows:
            assert "response" not in row
            assert row["review_status"] == "unreviewed"
            assert row["ground_truth"] and json.loads(row["ground_truth"])
    assert len(pack.policy_rows()) == 60
    assert len(pack.resolution_rows(orders)) == 30
    assert all(row["side_effects_possible"] for row in pack.resolution_rows(orders))
    assert len(pack.manager_rows(orders)) == 40


def test_pack_has_exact_counts_disjoint_splits_and_valid_messages(tmp_path):
    output = tmp_path / "upload-pack"
    manifest = pack.build(output)
    assert manifest["triage_split_counts"] == {"train": 200, "validation": 50, "test": 100}
    assert manifest["review_status"] == "unreviewed"
    assert manifest["training_approved"] is False
    assert manifest["evaluation_results"] is None
    review_rows = [
        json.loads(line) for line in (output / "08-triage-annotation-review.jsonl").read_text().splitlines()
    ]
    assert len(review_rows) == 250
    assert all(row["split"] in {"train", "validation"} and row["training_authorized"] is False
               for row in review_rows)
    frozen = [
        json.loads(line) for line in (output / "03-triage-evaluation.jsonl").read_text().splitlines()
    ]
    assert {row["example_id"] for row in review_rows}.isdisjoint(row["example_id"] for row in frozen)
    rows = [json.loads(line) for line in (output / "review-only" / "examples.jsonl").read_text().splitlines()]
    assert len(rows) == 350
    assert all(row["reviewer"] is None and row["review_status"] == "unreviewed" for row in rows)
    assert (output / "review-only" / "reviews.jsonl").read_bytes() == b""
    for filename, count in (("01-triage-training.jsonl", 200), ("02-triage-validation.jsonl", 50)):
        raw = (output / filename).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        records = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines()]
        assert len(records) == count
        for record in records:
            assert set(record) == {"messages"}
            assert [m["role"] for m in record["messages"]] == ["system", "user", "assistant"]
            assert pack.labels_valid(json.loads(record["messages"][2]["content"]))
    assert output.with_suffix(".zip").is_file()
    pack.validate_pack(output)
    with pytest.raises(FileExistsError):
        pack.build(output)


def test_hash_mismatch_is_not_silent(tmp_path):
    output = tmp_path / "changed-pack"
    pack.build(output)
    (output / "01-triage-training.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        pack.validate_pack(output)
