"""Offline, human-reviewed Orders triage data and immutable release artifacts."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "aisvc-zurlo-northwind"
PROJECT = "northwind-orders"
PROJECT_ENDPOINT = f"https://{ACCOUNT}.services.ai.azure.com/api/projects/{PROJECT}"
STAGING_DEPLOYMENT = "northwind-orders-triage-ft-v1"
INTENTS = ["ORDER_STATUS", "RETURN", "CANCEL", "MODIFY", "COMPLAINT", "REFUND_REQUEST", "OTHER"]
LABEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["order_id", "customer_hint", "intent", "sentiment", "urgency", "problem_summary"],
    "properties": {
        "order_id": {"type": ["string", "null"]},
        "customer_hint": {"type": ["string", "null"]},
        "intent": {"type": "string", "enum": INTENTS},
        "sentiment": {"type": "string", "enum": ["calm", "frustrated", "angry"]},
        "urgency": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
        "problem_summary": {"type": "string"},
    },
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_json(path: Path, value, *, exclusive: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x" if exclusive else "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def jsonl(rows: list[dict], *, bom: bool = False) -> bytes:
    return (("\ufeff" if bom else "") + "".join(canonical(row) + "\n" for row in rows)).encode("utf-8")


@contextmanager
def workspace_lock(workspace: Path):
    """An exclusive local lock prevents two reviewers appending the same revision."""
    lock = workspace / ".curation.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(now())
    try:
        yield
    finally:
        lock.unlink()


def labels_valid(labels) -> bool:
    if not isinstance(labels, dict) or set(labels) != set(LABEL_SCHEMA["required"]):
        return False
    for key in ("order_id", "customer_hint"):
        if labels[key] is not None and (not isinstance(labels[key], str) or not labels[key].strip()):
            return False
    return (
        labels["intent"] in INTENTS
        and labels["sentiment"] in ("calm", "frustrated", "angry")
        and labels["urgency"] in ("P1", "P2", "P3", "P4")
        and isinstance(labels["problem_summary"], str)
        and bool(labels["problem_summary"].strip())
    )


def instructions() -> str:
    return yaml.safe_load((ROOT / "agents" / "triage.yaml").read_text(encoding="utf-8"))["instructions"].strip()


def validate_sources(rows: list[dict]) -> None:
    seen = set()
    for row in rows:
        required = {"example_id", "family", "input", "expected", "review_status", "reviewer", "provenance", "split"}
        if not required.issubset(row):
            raise ValueError("Example is missing canonical fields")
        for key in ("example_id", "family", "input"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"Invalid {key}")
        if row["example_id"] in seen:
            raise ValueError(f"Duplicate example_id: {row['example_id']}")
        seen.add(row["example_id"])
        if row["review_status"] != "unreviewed" or row["reviewer"] is not None:
            raise ValueError("Source rows must remain unreviewed; approvals belong in the review ledger")
        if row["split"] not in ("train", "validation", "test") or not labels_valid(row["expected"]):
            raise ValueError(f"Invalid split or labels: {row['example_id']}")
        provenance = row["provenance"]
        if not isinstance(provenance, dict) or not provenance.get("source") or not provenance.get("kind"):
            raise ValueError("Source provenance is required")
        if provenance.get("kind") != "synthetic" and provenance.get("pii_reviewed") is not True:
            raise ValueError("Non-synthetic examples require explicit provenance.pii_reviewed=true")
        if provenance.get("contains_dynamic_order_truth") is not False or provenance.get("contains_policy_text") is not False:
            raise ValueError("Provenance must explicitly exclude dynamic order truth and policy text")


def import_examples(workspace: Path, source: Path) -> dict:
    additions = read_rows(source)
    validate_sources(additions)
    with workspace_lock(workspace):
        existing = read_rows(workspace / "examples.jsonl")
        validate_sources(existing + additions)
        materialize(workspace)
        registry_path = workspace / "split-registry.json"
        assert_no_leakage(existing + additions, read_json(registry_path) if registry_path.exists() else None)
        with (workspace / "examples.jsonl").open("ab") as stream:
            if (workspace / "examples.jsonl").stat().st_size and not (workspace / "examples.jsonl").read_bytes().endswith(b"\n"):
                stream.write(b"\n")
            stream.write(jsonl(additions))
    return {"imported": len(additions), "review_status": "unreviewed", "source_sha256": file_hash(source)}


def ledger(workspace: Path, sources: list[dict]) -> list[dict]:
    path = workspace / "reviews.jsonl"
    records = read_rows(path) if path.exists() else []
    validate_ledger(records, sources)
    return records


def validate_ledger(records: list[dict], sources: list[dict]) -> None:
    previous = None
    versions = {}
    source_by_id = {row["example_id"]: row for row in sources}
    for record in records:
        unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
        example_id = record["example_id"]
        if record.get("previous_sha256") != previous or digest(unsigned) != record["record_sha256"]:
            raise ValueError("Review ledger hash chain is invalid")
        if example_id not in source_by_id or record["source_sha256"] != digest(source_by_id[example_id]):
            raise ValueError("Source changed after review; import a new example_id and preserve the old source")
        if type(record["version"]) is not int or record["version"] != versions.get(example_id, 0) + 1:
            raise ValueError("Review version is not consecutive")
        if record["review_status"] not in ("accepted", "rejected") or not record.get("reviewer", "").strip():
            raise ValueError("A named reviewer and accepted/rejected decision are required")
        if not labels_valid(record["corrected_labels"]):
            raise ValueError("Invalid corrected labels")
        versions[example_id] = record["version"]
        previous = record["record_sha256"]


def reviewed_rows(sources: list[dict], records: list[dict]) -> list[dict]:
    latest = {record["example_id"]: record for record in records}
    rows = []
    for source in sources:
        row = dict(source)
        review = latest.get(row["example_id"])
        if review:
            row.update(expected=review["corrected_labels"], review_status=review["review_status"],
                       reviewer=review["reviewer"], review_version=review["version"],
                       review_sha256=review["record_sha256"])
        rows.append(row)
    return rows


def materialize(workspace: Path) -> tuple[list[dict], list[dict]]:
    sources = read_rows(workspace / "examples.jsonl")
    validate_sources(sources)
    records = ledger(workspace, sources)
    return reviewed_rows(sources, records), records


def review(workspace: Path, example_id: str, decision: str, reviewer: str,
           reason: str, corrected: dict | None = None, annotation_ref: str | None = None) -> dict:
    if decision not in ("accepted", "rejected") or not reviewer.strip() or not reason.strip():
        raise ValueError("A decision, named human reviewer, and review reason are required")
    with workspace_lock(workspace):
        rows, records = materialize(workspace)
        row = next((row for row in rows if row["example_id"] == example_id), None)
        if row is None:
            raise ValueError(f"Unknown example: {example_id}")
        sources = {r["example_id"]: r for r in read_rows(workspace / "examples.jsonl")}
        labels = corrected if corrected is not None else row["expected"]
        if not labels_valid(labels):
            raise ValueError("Corrected labels must match the triage output schema")
        record = {
            "schema_version": 1, "example_id": example_id,
            "version": row.get("review_version", 0) + 1,
            "review_status": decision, "reviewer": reviewer.strip(), "reviewed_at": now(),
            "reason": reason, "corrected_labels": labels, "annotation_ref": annotation_ref,
            "source_sha256": digest(sources[example_id]),
            "previous_sha256": records[-1]["record_sha256"] if records else None,
        }
        record["record_sha256"] = digest(record)
        with (workspace / "reviews.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(canonical(record) + "\n")
    return record


def leakage_keys(row: dict) -> set[str]:
    """Group paraphrases, shared identifiers, threads and normalized duplicates."""
    keys = {"family:" + row["family"].casefold(),
            "input:" + re.sub(r"\W+", " ", row["input"].casefold()).strip()}
    for order_id in re.findall(r"\bNW-\d+\b", row["input"], flags=re.IGNORECASE):
        keys.add("order_id:" + order_id.casefold())
    for email in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", row["input"]):
        keys.add("customer_hint:" + email.casefold())
    for name in ("order_id", "customer_hint"):
        if row["expected"].get(name):
            keys.add(name + ":" + row["expected"][name].casefold().strip())
    for name in ("trace_id", "conversation_id", "source_group"):
        if row["provenance"].get(name):
            keys.add(name + ":" + str(row["provenance"][name]))
    return keys


def assert_no_leakage(rows: list[dict], historical: dict | None = None) -> dict:
    registry = dict(historical or {})
    for row in rows:
        for key in leakage_keys(row) | {"example_id:" + row["example_id"]}:
            if key in registry and registry[key] != row["split"]:
                raise ValueError(f"Cross-split leakage or historical split change: {key}")
            registry[key] = row["split"]
    return registry


def export(workspace: Path, version: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", version):
        raise ValueError("Version must be a safe, nonempty file name")
    with workspace_lock(workspace):
        rows, records = materialize(workspace)
        accepted = sorted((r for r in rows if r["review_status"] == "accepted"), key=lambda r: r["example_id"])
        counts = {split: sum(r["split"] == split for r in accepted) for split in ("train", "validation", "test")}
        if not all(counts.values()):
            raise ValueError("Review and accept at least one family in every split before exporting")
        registry_path = workspace / "split-registry.json"
        registry = assert_no_leakage(rows, read_json(registry_path) if registry_path.exists() else None)
        prompt = instructions()
        artifacts = {"accepted.jsonl": jsonl(accepted), "reviews.jsonl": jsonl(records),
                     "sources.jsonl": (workspace / "examples.jsonl").read_bytes()}
        for split in ("train", "validation"):
            artifacts[split + ".jsonl"] = jsonl([
                {"messages": [{"role": "system", "content": prompt},
                              {"role": "user", "content": r["input"]},
                              {"role": "assistant", "content": canonical(r["expected"])}]}
                for r in accepted if r["split"] == split
            ], bom=True)
        test_rows = [r for r in accepted if r["split"] == "test"]
        artifacts["test.jsonl"] = jsonl(test_rows)
        test_hash = hashlib.sha256(artifacts["test.jsonl"]).hexdigest()
        frozen = workspace / "frozen-test.json"
        if frozen.exists() and read_json(frozen)["sha256"] != test_hash:
            raise ValueError("Frozen test changed; use a separate, explicitly reviewed benchmark workspace")
        target = workspace / "releases" / version
        target.mkdir(parents=True, exist_ok=False)
        for name, data in artifacts.items():
            (target / name).write_bytes(data)
        (target / "triage-instructions.txt").write_text(prompt, encoding="utf-8")
        files = {name: {"sha256": file_hash(target / name), "bytes": (target / name).stat().st_size}
                 for name in [*artifacts, "triage-instructions.txt"]}
        manifest = {
            "schema_version": 1, "dataset_name": "northwind-orders-triage", "version": version,
            "created_at": now(), "account": ACCOUNT, "project": PROJECT,
            "review_status": "awaiting_dataset_approval", "counts": counts, "files": files,
            "review_ledger_head": records[-1]["record_sha256"], "frozen_test_sha256": test_hash,
            "source_sha256": file_hash(workspace / "examples.jsonl"),
            "prompt_source": "agents/triage.yaml", "prompt_sha256": files["triage-instructions.txt"]["sha256"],
            "split_policy": "family+entities+normalized_input+trace/conversation/source_group; historical split lock",
            "scope": "intent/entity/sentiment/urgency extraction only; no order truth or policy memorization",
        }
        write_json(target / "manifest.json", manifest)
        write_json(registry_path, registry, exclusive=False)
        if not frozen.exists():
            write_json(frozen, {"sha256": test_hash, "version": version, "created_at": now()})
    return target / "manifest.json"


def verify_manifest(path: Path) -> dict:
    manifest = read_json(path)
    if manifest.get("schema_version") != 1 or manifest.get("account") != ACCOUNT or manifest.get("project") != PROJECT:
        raise ValueError("Wrong manifest schema/account/project")
    required = {"train.jsonl", "validation.jsonl", "test.jsonl", "accepted.jsonl", "reviews.jsonl", "sources.jsonl",
                "triage-instructions.txt"}
    if set(manifest.get("files", {})) != required:
        raise ValueError("Manifest must list exactly the expected release artifacts")
    for name, metadata in manifest["files"].items():
        if file_hash(path.parent / name) != metadata["sha256"]:
            raise ValueError(f"Artifact hash mismatch: {name}")
        if (path.parent / name).stat().st_size != metadata["bytes"]:
            raise ValueError(f"Artifact size mismatch: {name}")
        if name in ("train.jsonl", "validation.jsonl") and metadata["bytes"] >= 512 * 1024 * 1024:
            raise ValueError("Fine-tuning files must be smaller than 512 MB")
    sources = read_rows(path.parent / "sources.jsonl")
    validate_sources(sources)
    if file_hash(path.parent / "sources.jsonl") != manifest.get("source_sha256"):
        raise ValueError("Release source hash differs from manifest lineage")
    records = read_rows(path.parent / "reviews.jsonl")
    if not records or records[-1].get("record_sha256") != manifest.get("review_ledger_head"):
        raise ValueError("Review ledger is empty or its head does not match the manifest")
    validate_ledger(records, sources)
    accepted = read_rows(path.parent / "accepted.jsonl")
    expected_accepted = sorted(
        (row for row in reviewed_rows(sources, records) if row["review_status"] == "accepted"),
        key=lambda row: row["example_id"],
    )
    if accepted != expected_accepted:
        raise ValueError("Accepted rows must exactly match latest ledger decisions, labels, reviewers and revisions")
    if not accepted or any(r.get("review_status") != "accepted" or not r.get("reviewer") for r in accepted):
        raise ValueError("Release contains unaccepted rows")
    if len({r["example_id"] for r in accepted}) != len(accepted):
        raise ValueError("Duplicate release examples")
    assert_no_leakage(accepted)
    counts = {split: sum(r["split"] == split for r in accepted) for split in ("train", "validation", "test")}
    if counts != manifest["counts"] or not all(counts.values()):
        raise ValueError("Manifest counts differ from accepted examples")
    if file_hash(path.parent / "test.jsonl") != manifest["frozen_test_sha256"]:
        raise ValueError("Frozen test hash mismatch")
    prompt = (path.parent / "triage-instructions.txt").read_text(encoding="utf-8")
    if manifest.get("prompt_sha256") != file_hash(path.parent / "triage-instructions.txt"):
        raise ValueError("Manifest prompt hash does not match release instructions")
    for split in ("train", "validation", "test"):
        expected = [r for r in accepted if r["split"] == split]
        if split != "test":
            expected = [{"messages": [{"role": "system", "content": prompt},
                                     {"role": "user", "content": r["input"]},
                                     {"role": "assistant", "content": canonical(r["expected"])}]} for r in expected]
        if read_rows(path.parent / (split + ".jsonl")) != expected:
            raise ValueError(f"{split} file differs from accepted rows")
    return manifest


def approve_dataset(manifest_path: Path, reviewer: str, confirmation: str) -> Path:
    verify_manifest(manifest_path)
    sha = file_hash(manifest_path)
    if not reviewer.strip() or confirmation != sha:
        raise ValueError("Dataset approval requires a named human and exact manifest SHA256 confirmation")
    path = manifest_path.parent / "dataset-approval.json"
    write_json(path, {"schema_version": 1, "approved": True, "approver": reviewer.strip(),
                      "approved_at": now(), "manifest_sha256": sha,
                      "attestation": "Reviewed accepted labels, provenance, split isolation and frozen test."})
    return path


def verify_approval(manifest_path: Path) -> dict:
    verify_manifest(manifest_path)
    approval = read_json(manifest_path.parent / "dataset-approval.json")
    if (approval.get("approved") is not True or not approval.get("approver", "").strip()
            or approval.get("manifest_sha256") != file_hash(manifest_path)):
        raise ValueError("Dataset approval is missing or does not bind this exact manifest")
    return approval
