from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals import create_foundry_agent_datasets as datasets


def test_separate_specialist_datasets_and_complete_orchestrator():
    exported = datasets.build_datasets()
    assert {role: len(rows) for role, rows in exported.items()} == {
        "triage": 15, "policy_claim_lookup": 8, "policy_wording": 15,
        "claim_action_draft": 6, "orchestrator": 50,
    }
    original = [json.loads(line) for line in datasets.GOLDEN.read_text().splitlines() if line.strip()]
    by_id = {row["id"]: row for row in original}
    for role, rows in exported.items():
        assert len({row["id"] for row in rows}) == len(rows)
        assert len({row["query"] for row in rows}) == len(rows)
        for row in rows:
            source = by_id[row["source_case_id"]]
            assert role == "orchestrator" or row["workflow"] == role
            assert row["query"].startswith(source["input"])
            assert json.loads(row["ground_truth"]) == source["expected"]
            assert json.loads(row["expected_behavior"])["case_expectations"] == source["expected"]
            assert json.loads(row["context"])["target_role"] == role
            assert "response" not in row and "execution_evidence" not in row
    assert {row["source_case_id"] for row in exported["orchestrator"]} == set(by_id)
    # Triage must not infer missing IDs from fixture context.
    missing = next(row for row in exported["triage"] if row["source_case_id"] == "triage-012")
    assert missing["query"] == by_id["triage-012"]["input"]


def test_standalone_draft_handoff_and_server_owned_workflow():
    row = datasets.build_datasets()["claim_action_draft"][0]
    handoff = json.loads(row["query"].split(":\n", 1)[1])
    assert handoff["claim"]["claim_id"] == "CLM-2001"
    assert handoff["policy"]["policy_id"] == handoff["claim"]["policy_id"]
    assert handoff["wording"]["wording_id"] == handoff["policy"]["wording_id"]
    assert "workflow_id" not in row["query"]
    assert "metadata.workflow_id" in row["expected_behavior"]
    assert "not a captured live tool result" in row["query"]


def test_wording_keeps_requested_out_of_period_date_and_state_mismatch():
    rows = {row["source_case_id"]: row for row in datasets.build_datasets()["policy_wording"]}
    context = json.loads(rows["policy-012"]["query"].split(":\n", 1)[1])
    assert context["loss_date"] == "2025-12-31"
    context = json.loads(rows["policy-011"]["query"].split(":\n", 1)[1])
    assert context["state"] == "WA"


def test_local_export_is_reproducible_and_refuses_changed_bytes(tmp_path):
    first = datasets.export_datasets(tmp_path)
    second = datasets.export_datasets(tmp_path)
    assert first == second
    for entry in first["datasets"]:
        assert entry["agent"] in entry["localFile"]
        assert entry["sha256"] == datasets._sha(Path(entry["localFile"]))
    path = Path(first["datasets"][0]["localFile"])
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        datasets.export_datasets(tmp_path)


@pytest.mark.parametrize("version", ["../v1", "1", "v0"])
def test_invalid_versions_are_rejected(tmp_path, version):
    with pytest.raises(ValueError, match="versions"):
        datasets.export_datasets(tmp_path, version)


def test_missing_workflow_is_rejected(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps({"id": "one", "workflow": "triage", "input": "Thanks", "expected": {}}))
    with pytest.raises(ValueError, match="every core workflow"):
        datasets.build_datasets(source)


@pytest.mark.parametrize("exists", [False, True])
def test_registration_reuses_existing_version_and_verifies_bytes(monkeypatch, exists):
    from azure.core.exceptions import ResourceNotFoundError

    remote = SimpleNamespace(name="triage-eval-seed", version="v1", data_uri="https://storage.example/data", id="dataset-id")
    uploads = []

    def get(**kwargs):
        assert kwargs == {"name": remote.name, "version": remote.version}
        if not exists:
            raise ResourceNotFoundError()
        return remote

    operations = SimpleNamespace(
        get=get,
        upload_file=lambda **kwargs: uploads.append(kwargs) or remote,
    )
    monkeypatch.setattr(datasets, "remote_dataset_hash", lambda *_: "abc")
    result = datasets.register_dataset(
        SimpleNamespace(datasets=operations),
        {"name": remote.name, "version": "v1", "localFile": "seed.jsonl", "sha256": "abc"},
    )
    assert result["created"] is not exists
    assert result["remote_sha256_verified"]
    assert len(uploads) == (0 if exists else 1)


def test_remote_mismatch_never_overwrites(monkeypatch):
    remote = SimpleNamespace(name="seed", version="v1")
    operations = SimpleNamespace(get=lambda **_: remote)
    monkeypatch.setattr(datasets, "remote_dataset_hash", lambda *_: "different")
    with pytest.raises(ValueError, match="no overwrite"):
        datasets.register_dataset(
            SimpleNamespace(datasets=operations),
            {"name": "seed", "version": "v1", "sha256": "approved"},
        )


def test_registration_does_not_treat_permission_errors_as_missing(monkeypatch):
    from azure.core.exceptions import HttpResponseError

    def get(**_):
        raise HttpResponseError("Forbidden")

    with pytest.raises(HttpResponseError, match="Forbidden"):
        datasets.register_dataset(
            SimpleNamespace(datasets=SimpleNamespace(get=get)),
            {"name": "seed", "version": "v1"},
        )


def test_agent_profile_keeps_matching_dataset_and_evaluator(tmp_path):
    import yaml

    output = tmp_path / "datasets"
    manifest = datasets.export_datasets(output)
    entry = manifest["datasets"][0]
    entry["datasetUri"] = "https://storage.example/seed.jsonl"
    endpoint = "https://foundry.example/api/projects/claims"
    path = datasets.save_agent_profile(output, entry, endpoint)
    assert datasets.save_agent_profile(output, entry, endpoint) == path
    environment = yaml.safe_load(path.read_text())["environments"]["claims"]
    assert environment["agentName"] == entry["agent"]
    assert environment["evaluationSuites"][0]["dataset"] == entry["name"]
    assert environment["evaluationSuites"][0]["evaluators"] == [{"name": entry["evaluator"]}]
    path.write_text("user-edited")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        datasets.save_agent_profile(output, entry, endpoint)


def test_read_back_hash_uses_actual_blob_bytes_without_persisting_sas(monkeypatch):
    from azure.storage.blob import BlobClient

    calls = []
    blob = SimpleNamespace(download_blob=lambda: SimpleNamespace(readall=lambda: b"dataset"))
    from contextlib import nullcontext
    monkeypatch.setattr(
        BlobClient, "from_blob_url",
        lambda *args, **kwargs: calls.append((args, kwargs)) or nullcontext(blob),
    )
    access = SimpleNamespace(blob_reference=SimpleNamespace(
        credential=SimpleNamespace(sas_uri="https://storage.example/container?sig=secret"),
    ))
    client = SimpleNamespace(datasets=SimpleNamespace(get_credentials=lambda **_: access))
    remote = SimpleNamespace(name="seed", version="v1", data_uri="https://storage.example/container/seed.jsonl")
    assert datasets.remote_dataset_hash(client, remote) == datasets.hashlib.sha256(b"dataset").hexdigest()
    assert calls == [((remote.data_uri,), {"credential": "sig=secret"})]
