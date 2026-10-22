from __future__ import annotations

import json
import shutil
import socket
import sys
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

import cli  # noqa: E402
import curation  # noqa: E402
import evaluation  # noqa: E402
import native_evaluation  # noqa: E402
import seed  # noqa: E402
import training  # noqa: E402

ACCOUNT_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/unit-test"
    "/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind"
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original = socket.socket.connect

    def denied(sock, address):
        # Windows asyncio builds its wakeup socketpair over loopback.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original(sock, address)
        raise AssertionError("Offline tests may not open network connections")
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def workspace(monkeypatch):
    # Test artifacts remain under the repository, not the machine's temp directory.
    base = PIPELINE / "tests" / ".artifacts"
    path = base / uuid.uuid4().hex
    path.mkdir(parents=True)
    (path / "examples.jsonl").write_bytes(curation.jsonl(seed.examples()))
    monkeypatch.setattr(training, "journal_root", lambda: path / "user-journal")
    yield path
    shutil.rmtree(path)
    if base.exists() and not any(base.iterdir()):
        base.rmdir()


def accept_all(workspace):
    for row in seed.examples():
        curation.review(workspace, row["example_id"], "accepted", "unit-test-reviewer",
                        "Unit-test fixture only, never a production approval")


@pytest.fixture
def release(workspace):
    accept_all(workspace)
    return curation.export(workspace, "test-v1")


def approved_budget(release):
    curation.approve_dataset(release, "unit-test-approver", curation.file_hash(release))
    budget = training.budget_template(release)
    current = datetime.now(timezone.utc)
    approved_at = (current - timedelta(minutes=1)).isoformat()
    expires_at = (current + timedelta(hours=1)).isoformat()
    budget.update(approved=True, approver="unit-test-budget", approved_at=approved_at, expires_at=expires_at,
                  account_resource_id=ACCOUNT_RESOURCE_ID,
                  region="northcentralus", max_training_tokens=10000000, max_cost_usd=100,
                  training_price_per_million_tokens_usd=1, price_reference="unit-test-only",
                  acknowledge_no_service_hard_cap=True)
    budget["admission"].update(approved=True, reviewer="unit-test-admission", checked_at=approved_at,
                               expires_at=expires_at, evidence="unit-test-only", training_supported=True,
                               training_not_retired=True, deployment_not_retired=True, quota_confirmed=True)
    path = release.parent / "test-budget.json"
    curation.write_json(path, budget)
    return path


def saved_predictions(release, deployment, run_id, response=None):
    manifest = curation.read_json(release)
    provenance = evaluation.prediction_provenance(release, manifest, deployment, run_id)
    return [{"example_id": row["example_id"], "response": response if response is not None else json.dumps(row["expected"]),
             **provenance} for row in curation.read_rows(release.parent / "test.jsonl")]


def refresh_artifact_hashes(release):
    manifest = curation.read_json(release)
    for name in manifest["files"]:
        manifest["files"][name] = {"sha256": curation.file_hash(release.parent / name),
                                   "bytes": (release.parent / name).stat().st_size}
    curation.write_json(release, manifest, exclusive=False)


def test_seed_is_unreviewed_useful_and_grouped(workspace):
    rows = seed.examples()
    assert len(rows) == 44
    assert {r["expected"]["intent"] for r in rows} == set(curation.INTENTS)
    assert {r["expected"]["urgency"] for r in rows} == {"P1", "P2", "P3", "P4"}
    assert all(r["review_status"] == "unreviewed" and r["reviewer"] is None for r in rows)
    curation.assert_no_leakage(rows)
    result = cli.status(workspace)
    assert result["counts"]["unreviewed"] == 44
    assert result["evaluation"] == {"state": "not_run", "metrics": None}
    assert result["training"]["job_id"] is None
    assert result["staging"]["activation_environment"] == {}


def test_unreviewed_never_export(workspace):
    with pytest.raises(ValueError, match="every split"):
        curation.export(workspace, "v1")


def test_reviews_append_corrected_labels_and_native_reference(workspace):
    row = seed.examples()[0]
    first = curation.review(workspace, row["example_id"], "rejected", "human-a", "Needs corrected label")
    labels = {**row["expected"], "urgency": "P2"}
    second = curation.review(workspace, row["example_id"], "accepted", "human-b", "Corrected urgency",
                             labels, "trace:synthetic/unit-test")
    assert second["version"] == 2
    assert second["previous_sha256"] == first["record_sha256"]
    rows, records = curation.materialize(workspace)
    assert len(records) == 2
    assert rows[0]["expected"]["urgency"] == "P2"
    assert rows[0]["reviewer"] == "human-b"
    assert curation.read_rows(workspace / "examples.jsonl")[0]["review_status"] == "unreviewed"


def test_tampered_ledger_rejected(workspace):
    row = seed.examples()[0]
    curation.review(workspace, row["example_id"], "accepted", "human", "Reviewed")
    path = workspace / "reviews.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace('"human"', '"imposter"'), encoding="utf-8")
    with pytest.raises(ValueError, match="hash chain"):
        curation.materialize(workspace)


def test_changed_source_invalidates_review(workspace):
    row = seed.examples()[0]
    curation.review(workspace, row["example_id"], "accepted", "human", "Reviewed")
    rows = seed.examples()
    rows[0]["input"] += " Changed."
    (workspace / "examples.jsonl").write_bytes(curation.jsonl(rows))
    with pytest.raises(ValueError, match="Source changed"):
        curation.materialize(workspace)


@pytest.mark.parametrize("key", ["family", "entity", "input", "thread"])
def test_leakage_fails_across_splits(key):
    rows = [dict(seed.examples()[0]), dict(seed.examples()[-1])]
    if key == "family":
        rows[1]["family"] = rows[0]["family"]
    elif key == "entity":
        rows[1]["input"] += " Also NW-91001."
    elif key == "input":
        rows[1]["input"] = rows[0]["input"].upper()
    else:
        rows[0]["provenance"] = {"conversation_id": "shared"}
        rows[1]["provenance"] = {"conversation_id": "shared"}
    with pytest.raises(ValueError, match="leakage"):
        curation.assert_no_leakage(rows)


def test_release_hashes_bom_and_accepted_only(release):
    manifest = curation.verify_manifest(release)
    assert manifest["counts"] == {"train": 24, "validation": 8, "test": 12}
    assert (release.parent / "train.jsonl").read_bytes().startswith(b"\xef\xbb\xbf")
    assert all(r["review_status"] == "accepted" for r in curation.read_rows(release.parent / "test.jsonl"))
    train = curation.read_rows(release.parent / "train.jsonl")
    assert set(train[0]) == {"messages"}
    assert train[0]["messages"][0]["content"] == curation.instructions()
    with pytest.raises(FileExistsError):
        curation.export(release.parents[2], "test-v1")


def test_rejected_never_exports(workspace):
    accept_all(workspace)
    curation.review(workspace, seed.examples()[0]["example_id"], "rejected", "human", "Excluded")
    manifest = curation.export(workspace, "v1")
    assert curation.read_json(manifest)["counts"]["train"] == 23


def test_frozen_test_and_historical_splits_do_not_move(release):
    workspace = release.parents[2]
    curation.export(workspace, "test-v2")
    row = seed.examples()[-1]
    curation.review(workspace, row["example_id"], "accepted", "human", "Attempted new test label",
                    {**row["expected"], "urgency": "P2"})
    with pytest.raises(ValueError, match="Frozen test"):
        curation.export(workspace, "test-v3")
    assert not (workspace / "releases" / "test-v3").exists()


def test_manifest_tampering_blocks_approval(release):
    with (release.parent / "train.jsonl").open("ab") as stream:
        stream.write(b"{}\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        curation.approve_dataset(release, "human", curation.file_hash(release))


def test_dataset_approval_requires_hash(release):
    with pytest.raises(ValueError, match="SHA256"):
        curation.approve_dataset(release, "human", "wrong")
    curation.approve_dataset(release, "human", curation.file_hash(release))
    assert curation.verify_approval(release)["approved"]


def test_training_default_is_blocked_offline_plan(release):
    plan = training.training_plan(release, None)
    assert plan["state"] == "dry_run"
    assert len(plan["blocked_reasons"]) == 2
    assert training.budget_template(release)["approved"] is False


@pytest.mark.parametrize("change", ["tokens", "epochs", "cost", "retired", "expired", "residency", "approval", "nan"])
def test_training_budget_fail_closed(release, change):
    path = approved_budget(release)
    budget = curation.read_json(path)
    if change == "tokens":
        budget["max_training_tokens"] = 1
    elif change == "epochs":
        budget["epochs"] = 2
    elif change == "cost":
        budget["max_cost_usd"] = 0.000001
    elif change == "retired":
        budget["admission"]["training_not_retired"] = False
    elif change == "expired":
        budget["expires_at"] = "2020-01-01T00:00:00+00:00"
    elif change == "residency":
        budget.update(training_tier="Developer", residency="regional")
    elif change == "approval":
        budget["approved"] = False
    else:
        budget["max_cost_usd"] = float("nan")
    path.write_text(json.dumps(budget), encoding="utf-8")
    assert training.training_plan(release, path)["blocked_reasons"]


def test_submit_requires_explicit_budget_hash_before_network(release):
    path = approved_budget(release)
    assert not training.training_plan(release, path)["blocked_reasons"]
    with pytest.raises(ValueError, match="SHA256"):
        training.submit(release, path, release.parent / "receipt.json", "wrong", curation.PROJECT)
    assert not (release.parent / "receipt.json").exists()


def test_submit_sdk_contract_and_no_test_upload(release, monkeypatch):
    path = approved_budget(release)
    uploaded = []
    kwargs = {}

    def upload(*, file, purpose):
        assert file[1].startswith(b"\xef\xbb\xbf")
        uploaded.append((file[0], purpose))
        return SimpleNamespace(id="file-" + str(len(uploaded)))

    def create(**values):
        kwargs.update(values)
        return SimpleNamespace(id="ftjob-unit-test", status="queued")

    class FakeClient(SimpleNamespace):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    client = FakeClient(
        files=SimpleNamespace(create=upload, wait_for_processing=lambda *_a, **_k: SimpleNamespace(status="processed")),
        fine_tuning=SimpleNamespace(jobs=SimpleNamespace(create=create)),
    )
    monkeypatch.setattr(training, "openai_client", lambda: (nullcontext(), client))
    reads = []

    def account_read(_credential, resource_id):
        reads.append(resource_id)
        return {"id": resource_id, "name": curation.ACCOUNT, "location": "northcentralus",
                "properties": {"defaultProject": curation.PROJECT}}

    monkeypatch.setattr(training, "read_account", account_read)
    result = training.submit(release, path, release.parent / "receipt.json", curation.file_hash(path), curation.PROJECT)
    assert result["state"] == "submitted"
    assert uploaded == [("train.jsonl", "fine-tune"), ("validation.jsonl", "fine-tune")]
    assert kwargs["model"] == "gpt-4.1-mini-2025-04-14"
    assert kwargs["method"]["supervised"]["hyperparameters"]["n_epochs"] == 1
    assert kwargs["extra_body"] == {"trainingType": "Standard"}
    assert reads == [ACCOUNT_RESOURCE_ID] * 3
    assert result["default_project_verification"]["actual_default_project"] == curation.PROJECT
    with pytest.raises(ValueError, match="already has a submission"):
        training.submit(release, path, release.parent / "different-receipt.json", curation.file_hash(path), curation.PROJECT)
    copied = release.parents[2] / "copied-release"
    shutil.copytree(release.parent, copied)
    with pytest.raises(ValueError, match="persistent journal"):
        training.submit(copied / "manifest.json", copied / path.name, copied / "new-receipt.json",
                        curation.file_hash(copied / path.name), curation.PROJECT)
    assert len(uploaded) == 2 and len(reads) == 3
    journal = curation.read_json(Path(result["journal_path"]))
    assert journal["job_id"] == "ftjob-unit-test" and journal["state"] == "submitted"
    assert Path(result["journal_path"]).parent == training.journal_root()
    assert not Path(result["journal_path"]).is_relative_to(release.parent)


@pytest.mark.parametrize("mutation", ["invalid", "extra", "intent", "entity", "urgent"])
def test_deterministic_failure_metrics(mutation):
    expected = {**seed.examples()[0]["expected"], "urgency": "P1"}
    actual = dict(expected)
    if mutation == "invalid":
        response = "Here is the answer"
    else:
        if mutation == "extra":
            actual["approved"] = True
        elif mutation == "intent":
            actual["intent"] = "OTHER"
        elif mutation == "entity":
            actual["order_id"] = "NW-99999"
        else:
            actual["urgency"] = "P3"
        response = json.dumps(actual)
    scores = evaluation.grade(expected, response)
    assert scores["joint_accuracy"] == 0
    if mutation in ("invalid", "extra", "urgent"):
        assert scores["p1_recall"] == 0


def test_metrics_artifacts_are_computed_not_baselines(release):
    # Canned responses are confined to unit tests, not checked-in benchmark results.
    predictions = release.parent / "test-predictions.jsonl"
    predictions.write_bytes(curation.jsonl(saved_predictions(release, "unit-base", "base-run")))
    baseline = release.parent / "test-baseline.json"
    report = evaluation.evaluate(release, predictions, baseline, "unit-base")
    assert report["metrics"]["schema_accuracy"] == 1
    assert report["regression_status"] == "unavailable"
    assert not report["passed"]
    candidate_predictions = release.parent / "candidate-predictions.jsonl"
    candidate_predictions.write_bytes(curation.jsonl(saved_predictions(release, "unit-candidate", "candidate-run")))
    candidate = evaluation.evaluate(release, candidate_predictions, release.parent / "test-candidate.json", "unit-candidate", baseline)
    assert candidate["passed"]
    predictions.write_bytes(curation.jsonl(saved_predictions(release, "unit-candidate", "failed-run", "{}")[:1]))
    failed = evaluation.evaluate(release, predictions, release.parent / "test-failed.json", "unit-candidate", baseline)
    assert failed["state"] == "incomplete"
    assert not failed["passed"]
    assert failed["metrics"]["schema_accuracy"] == 0
    assert failed["denominators"]["schema_accuracy"] == 12


def test_status_rejects_unrelated_evaluation(workspace, release):
    fake = workspace / "wrong-report.json"
    curation.write_json(fake, {"manifest_sha256": "wrong", "metrics": {"intent_accuracy": 1}})
    with pytest.raises(ValueError, match="does not match"):
        cli.status(workspace, release, fake)


def test_native_publish_default_is_plan_only(release):
    plan = training.publish_dataset(release, release.parent / "publication-plan.json", False, None)
    assert plan["state"] == "dry_run"
    assert len(plan["operations"]) == 3
    assert all(op["version"] == "test-v1" for op in plan["operations"])


def test_native_eval_plan_compares_actual_response_not_reference_to_itself(release):
    predictions = release.parent / "native-predictions.jsonl"
    predictions.write_bytes(curation.jsonl(saved_predictions(release, "unit-candidate", "native-run", "{}")))
    plan = native_evaluation.run_native(
        release, predictions, release.parent / "native-plan.json", False, None)
    assert plan["state"] == "dry_run"
    assert plan["create"]["testing_criteria"][0]["input"] == "{{item.actual_schema}}"
    assert plan["create"]["testing_criteria"][0]["reference"] == "{{item.expected_schema}}"
    item = plan["run"]["data_source"]["source"]["content"][0]["item"]
    assert item["actual_schema"] == "false"
    assert item["expected_schema"] == "true"


def test_import_redacted_native_examples_requires_unreviewed_and_extraction_scope(workspace):
    row = seed.examples()[0]
    row.update(example_id="native-trace-example", family="native-case")
    row["provenance"].update(kind="native_trace", source="manual-export", pii_reviewed=True)
    path = workspace / "import.jsonl"
    path.write_bytes(curation.jsonl([row]))
    assert curation.import_examples(workspace, path)["review_status"] == "unreviewed"
    assert cli.status(workspace)["counts"]["unreviewed"] == 45
    row["example_id"] = "native-other"
    row["provenance"]["contains_policy_text"] = True
    path.write_bytes(curation.jsonl([row]))
    with pytest.raises(ValueError, match="policy text"):
        curation.import_examples(workspace, path)


def test_submit_requires_explicit_training_project_even_with_approved_budget(release):
    path = approved_budget(release)
    with pytest.raises(ValueError, match="explicit --training-project"):
        training.submit(release, path, release.parent / "receipt.json", curation.file_hash(path))


@pytest.mark.parametrize("actual_default", ["northwind-claims", "default", None, "northwind-orders-old"])
def test_wrong_account_default_blocks_every_upload_and_job(release, monkeypatch, actual_default):
    path = approved_budget(release)

    class NoWrites:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @property
        def files(self):
            raise AssertionError("No file API may be reached when default project differs")

        @property
        def fine_tuning(self):
            raise AssertionError("No training API may be reached when default project differs")

    monkeypatch.setattr(training, "openai_client", lambda: (nullcontext(), NoWrites()))
    monkeypatch.setattr(training, "read_account", lambda _cred, resource_id: {
        "id": resource_id, "name": curation.ACCOUNT, "location": "northcentralus",
        "properties": {"defaultProject": actual_default},
    })
    output = release.parent / "blocked-training.json"
    with pytest.raises(ValueError, match="defaultProject"):
        training.submit(release, path, output, curation.file_hash(path), curation.PROJECT)
    receipt = curation.read_json(output)
    assert receipt["state"] == "blocked"
    assert receipt["default_project_verification"]["actual_default_project"] == actual_default
    assert receipt["training_file_id"] is None and receipt["job_id"] is None
    assert not training.journal_root().exists()
    status = cli.status(release.parents[2], release, training_path=output)
    assert status["training_placement"]["state"] == "blocked"
    assert status["training_placement"]["actual_default_project"] == actual_default


def test_arm_check_uses_only_get_and_never_follows_redirects(monkeypatch):
    import httpx
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"properties": {"defaultProject": "northwind-orders"}}

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    class FakeCredential:
        def get_token(self, scope):
            assert scope == "https://management.azure.com/.default"
            return SimpleNamespace(token="unit-test-not-a-secret")

    monkeypatch.setattr(httpx, "get", get)
    training.read_account(FakeCredential(), ACCOUNT_RESOURCE_ID)
    assert len(calls) == 1
    assert calls[0][0] == "https://management.azure.com" + ACCOUNT_RESOURCE_ID
    assert calls[0][1]["params"] == {"api-version": "2026-09-01"}
    assert calls[0][1]["follow_redirects"] is False
    with pytest.raises(ValueError, match="account_resource_id"):
        training.read_account(FakeCredential(), ACCOUNT_RESOURCE_ID + "?bad=1")


def test_default_switch_between_uploads_blocks_job(release, monkeypatch):
    path = approved_budget(release)
    writes = []
    reads = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @property
        def files(self):
            return SimpleNamespace(create=self.upload, wait_for_processing=lambda *_a, **_kw: SimpleNamespace(status="processed"))

        def upload(self, *, file, purpose):
            writes.append(file[0])
            return SimpleNamespace(id="file-unit-test")

        @property
        def fine_tuning(self):
            raise AssertionError("No job may be submitted after the account default changes")

    def account_read(_cred, resource_id):
        reads.append(resource_id)
        return {"id": resource_id, "name": curation.ACCOUNT, "location": "northcentralus",
                "properties": {"defaultProject": curation.PROJECT if len(reads) == 1 else "northwind-claims"}}

    monkeypatch.setattr(training, "read_account", account_read)
    monkeypatch.setattr(training, "openai_client", lambda: (nullcontext(), FakeClient()))
    output = release.parent / "changed-default.json"
    with pytest.raises(ValueError, match="defaultProject"):
        training.submit(release, path, output, curation.file_hash(path), curation.PROJECT)
    assert writes == ["train.jsonl"]
    receipt = curation.read_json(output)
    assert receipt["state"] == "submission_uncertain"
    assert receipt["job_id"] is None
    assert receipt["default_project_verification"]["state"] == "blocked"


def test_operator_report_marks_default_promotion_blocked_without_authorizing_training(workspace):
    curation.write_json(workspace / "default-promotion.json", {
        "account": curation.ACCOUNT, "requested_project": curation.PROJECT,
        "state": "blocked", "source": "operator_report",
    })
    result = cli.status(workspace)
    assert result["default_project_promotion"]["state"] == "blocked"
    assert result["training_placement"]["state"] == "not_verified"
    assert result["training"]["state"] == "not_submitted"


@pytest.mark.parametrize("field", [
    "deployment", "manifest_sha256", "dataset_version", "frozen_test_sha256", "prompt_sha256", "prediction_run_id",
])
def test_prediction_provenance_must_match_recorded_release_and_deployment(release, field):
    path = release.parent / "predictions.jsonl"
    rows = saved_predictions(release, "unit-base", "base-run")
    rows[-1][field] = "mismatch"
    path.write_bytes(curation.jsonl(rows))
    with pytest.raises(ValueError, match="provenance"):
        evaluation.evaluate(release, path, release.parent / "result.json", "unit-base")


def test_predictions_cannot_be_relabelled_or_reused_as_candidate(release):
    path = release.parent / "base-predictions.jsonl"
    path.write_bytes(curation.jsonl(saved_predictions(release, "unit-base", "base-run")))
    baseline = release.parent / "baseline-run.json"
    evaluation.evaluate(release, path, baseline, "unit-base")
    with pytest.raises(ValueError, match="provenance"):
        evaluation.evaluate(release, path, release.parent / "never-invoked.json", "unit-never-invoked", baseline)
    with pytest.raises(ValueError, match="reuse the same predictions"):
        evaluation.evaluate(release, path, release.parent / "same-predictions.json", "unit-base", baseline)
    path.write_bytes(curation.jsonl(saved_predictions(release, "unit-candidate", "base-run")))
    with pytest.raises(ValueError, match="same predictions or inference run"):
        evaluation.evaluate(release, path, release.parent / "same-run.json", "unit-candidate", baseline)


@pytest.mark.parametrize("tamper", ["empty", "head", "chain", "review_hash", "reviewer", "revision", "labels", "latest_rejected"])
def test_dataset_approval_requires_real_latest_review_ledger(release, tamper):
    manifest = curation.read_json(release)
    if tamper == "empty":
        (release.parent / "reviews.jsonl").write_bytes(b"")
        manifest["review_ledger_head"] = None
    elif tamper == "head":
        manifest["review_ledger_head"] = "not-the-head"
    elif tamper == "chain":
        records = curation.read_rows(release.parent / "reviews.jsonl")
        records[0]["reviewer"] = "changed-without-valid-chain"
        (release.parent / "reviews.jsonl").write_bytes(curation.jsonl(records))
    elif tamper == "latest_rejected":
        record = curation.review(release.parents[2], seed.examples()[0]["example_id"],
                                 "rejected", "test-reviewer", "Reject previously accepted row")
        (release.parent / "reviews.jsonl").write_bytes((release.parents[2] / "reviews.jsonl").read_bytes())
        manifest["review_ledger_head"] = record["record_sha256"]
    else:
        rows = curation.read_rows(release.parent / "accepted.jsonl")
        if tamper == "review_hash":
            rows[0]["review_sha256"] = "nonexistent-review"
        elif tamper == "reviewer":
            rows[0]["reviewer"] = "not-the-reviewer"
        elif tamper == "revision":
            rows[0]["review_version"] += 1
        else:
            rows[0]["expected"]["urgency"] = "P4"
        (release.parent / "accepted.jsonl").write_bytes(curation.jsonl(rows))
    curation.write_json(release, manifest, exclusive=False)
    refresh_artifact_hashes(release)
    with pytest.raises(ValueError, match="ledger|Accepted rows"):
        curation.approve_dataset(release, "test-approver", curation.file_hash(release))
    assert not (release.parent / "dataset-approval.json").exists()
