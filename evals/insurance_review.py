"""Offline insurance audit, immutable manifests, and explicitly attested user consent."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree


EXECUTION_REVIEW_FINDINGS = {
    r"evals\insurance_native.py": {"INS-EXEC-001", "INS-EXEC-002", "INS-LINEAGE-001"},
    r"evals\insurance_gates.py": {"INS-GATE-001", "INS-GATE-002", "INS-GATE-003"},
    r"evals\insurance_review.py": {"INS-LINEAGE-001"},
    r"scripts\run-insurance-quality-gates.py": {"INS-LINEAGE-001"},
}
EXECUTION_REVIEW_TESTS = (
    r"evals\tests\test_insurance_gates.py",
    r"scripts\tests\test_insurance_review_workflow.py",
    r"scripts\tests\test_insurance_execution_code_review.py",
)
LINEAGE_FIELDS = (
    "prepared_source_code_sha256", "executed_source_code_sha256",
    "execution_code_review_receipt_sha256", "execution_code_review_receipt",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def loads(text: str):
    return json.loads(text, object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def read_json(path: Path):
    return loads(path.read_text(encoding="utf-8-sig"))


def rows(path: Path) -> list[dict]:
    result = [loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in result):
        raise ValueError("JSONL rows must be objects")
    return result


def write_new(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def normalized_input(text: str) -> str:
    text = re.sub(r"\b(?:CLM|POL|PH)-\d+\b", "<ID>", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip().casefold()


def valid_labels(value: dict) -> bool:
    fields = {"claim_id", "policy_id", "policyholder_id", "intent", "sentiment", "urgency", "problem_summary"}
    return (
        isinstance(value, dict) and set(value) == fields
        and all(value[k] is None or (isinstance(value[k], str)
                and re.fullmatch(prefix + r"-[0-9]+", value[k]))
                for k, prefix in (("claim_id", "CLM"), ("policy_id", "POL"), ("policyholder_id", "PH")))
        and value["intent"] in ("FNOL", "CLAIM_STATUS", "POLICY_QUESTION", "EVIDENCE_SUBMISSION",
                                "ADJUSTER_REQUEST", "SETTLEMENT_QUERY", "COMPLAINT", "OTHER")
        and value["sentiment"] in ("calm", "frustrated", "angry")
        and value["urgency"] in ("P1", "P2", "P3", "P4")
        and isinstance(value["problem_summary"], str) and bool(value["problem_summary"].strip())
    )


def verify_pack(path: Path) -> dict:
    manifest = read_json(path / "manifest.json")
    files = manifest.get("files_sha256")
    if not isinstance(files, dict) or not files:
        raise ValueError("Manifest requires artifact hashes")
    for name, digest in files.items():
        if Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError("Manifest artifact must be a direct child file")
        if not re.fullmatch(r"[a-f0-9]{64}", str(digest)) or sha256(path / name) != digest:
            raise ValueError(f"Artifact hash mismatch: {name}")
    if manifest.get("business_domain") != "insurance" or manifest.get("synthetic") is not True:
        raise ValueError("Only explicitly synthetic insurance packs are supported")
    return manifest


def _exact_fields(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{name} requires exact fields: {', '.join(sorted(fields))}")


def _is_sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _root_file(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or "/" in name or ":" in name
            or str(Path(name)) != name or Path(name).is_absolute()
            or any(part in (".", "..") for part in Path(name).parts)):
        raise ValueError("Only canonical project-relative Windows file paths are permitted")
    source = (root / name).resolve()
    if not source.is_relative_to(root.resolve()) or not source.is_file():
        raise ValueError("Source/evidence path is outside the project or is not a file")
    return source


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Execution review requires timezone-aware timestamps") from exc


def execution_review_test_command(result_path: str) -> list[str]:
    return [
        r".\.venv\Scripts\python.exe", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--basetemp", ".insurance-execution-code-test-work", "--junitxml", result_path,
        *EXECUTION_REVIEW_TESTS,
    ]


def _verify_test_evidence(evidence, root, pack, manifest_hash, executed, reviewed_at):
    _exact_fields(evidence, ("command", "test_sources_sha256", "result_path", "result_sha256"), "Test evidence")
    tests = evidence["test_sources_sha256"]
    _exact_fields(tests, EXECUTION_REVIEW_TESTS, "Regression test sources")
    for name, expected in tests.items():
        if not _is_sha256(expected) or sha256(_root_file(root, name)) != expected:
            raise ValueError("Regression test source hash changed")
    result_path = _root_file(root, evidence["result_path"])
    if result_path.is_relative_to(pack.resolve()) or result_path.suffix != ".xml":
        raise ValueError("Pytest result evidence must be an XML file outside the immutable pack")
    if evidence["command"] != execution_review_test_command(evidence["result_path"]):
        raise ValueError("Test evidence must use the complete fixed regression command, without selectors")
    result_bytes = result_path.read_bytes()
    if (not _is_sha256(evidence["result_sha256"])
            or hashlib.sha256(result_bytes).hexdigest() != evidence["result_sha256"]):
        raise ValueError("Pytest result evidence hash mismatch")
    if len(result_bytes) > 10000000 or any(token in result_bytes.upper() for token in (b"<!DOCTYPE", b"<!ENTITY")):
        raise ValueError("Unsafe or oversized pytest XML evidence")
    try:
        report = ElementTree.fromstring(result_bytes)
        if report.tag != "testsuites" or len(report) != 1 or report[0].tag != "testsuite":
            raise ValueError("Expected one complete pytest suite")
        suite = report[0]
        cases = suite.findall("testcase")
        if (suite.get("name") != "pytest" or int(suite.get("tests", "0")) != len(cases) or not cases
                or any(int(suite.get(key, "-1")) != 0 for key in ("failures", "errors", "skipped"))
                or any(case.find(key) is not None for case in cases for key in ("failure", "error", "skipped"))):
            raise ValueError("Pytest evidence must contain complete passing results, with no errors or skips")
        identities = [(case.get("classname"), case.get("name")) for case in cases]
        modules = {str(Path(name).with_suffix("")).replace("\\", ".") for name in EXECUTION_REVIEW_TESTS}
        if (len(set(identities)) != len(cases) or any(not name for _, name in identities)
                or {module for module, _ in identities} != modules):
            raise ValueError("Pytest evidence must cover each required regression module exactly")
        containers = suite.findall("properties")
        if len(containers) != 1:
            raise ValueError("Pytest source snapshot properties are missing")
        properties = unique_object([(item.attrib["name"], item.attrib["value"]) for item in containers[0]
                                    if item.tag == "property"])
        _exact_fields(properties, (
            "insurance_prepared_manifest_sha256", "insurance_executed_source_code_sha256",
            "insurance_test_sources_sha256", "insurance_test_evidence_recorded_at", "insurance_test_pytest_args",
        ), "Pytest source snapshot")
        if (properties["insurance_prepared_manifest_sha256"] != manifest_hash
                or loads(properties["insurance_executed_source_code_sha256"]) != executed
                or loads(properties["insurance_test_sources_sha256"]) != tests
                or loads(properties["insurance_test_pytest_args"]) != evidence["command"][3:]):
            raise ValueError("Pytest results were produced for a different manifest/source/test snapshot")
        recorded_at = _timestamp(properties["insurance_test_evidence_recorded_at"])
        if not reviewed_at - timedelta(hours=24) <= recorded_at <= reviewed_at:
            raise ValueError("Pytest source evidence is stale or later than its review")
    except (ElementTree.ParseError, KeyError, TypeError) as exc:
        raise ValueError("Malformed pytest result evidence") from exc


def _verify_execution_review(pack, root, manifest, executed, path, pin, approval_path):
    if not _is_sha256(pin) or approval_path is None:
        raise ValueError("Execution review requires an independent receipt SHA256 and actual user approval receipt")
    if path.resolve().is_relative_to(pack.resolve()):
        raise ValueError("Execution review receipts must remain outside the immutable pack")
    receipt_bytes = path.read_bytes()
    if hashlib.sha256(receipt_bytes).hexdigest() != pin:
        raise ValueError("Execution code review receipt SHA256 mismatch")
    receipt = loads(receipt_bytes.decode("utf-8-sig"))
    _exact_fields(receipt, (
        "schema_version", "kind", "receipt_version", "actor_type", "reviewer_session_id",
        "reviewed_at", "expires_at", "scope", "individual_human_annotations", "manifest_sha256",
        "artifact_approval_receipt_sha256", "changes", "test_evidence",
    ), "Execution code review receipt")
    if (type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
            or receipt["kind"] != "insurance_reviewed_execution_code"
            or receipt["scope"] != "evaluation_only_no_training" or receipt["actor_type"] != "assistant"
            or type(receipt["individual_human_annotations"]) is not int or receipt["individual_human_annotations"] != 0
            or not isinstance(receipt["reviewer_session_id"], str) or not receipt["reviewer_session_id"].strip()
            or not isinstance(receipt["receipt_version"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*-v[1-9][0-9]*", receipt["receipt_version"])):
        raise ValueError("Execution review is versioned assistant code review only, never human annotation/consent")
    reviewed_at, expires_at = _timestamp(receipt["reviewed_at"]), _timestamp(receipt["expires_at"])
    if not reviewed_at <= datetime.now(timezone.utc) < expires_at <= reviewed_at + timedelta(hours=24):
        raise ValueError("Execution review expired, future-dated or exceeds the 24-hour source attestation window")
    manifest_hash = sha256(pack / "manifest.json")
    if receipt["manifest_sha256"] != manifest_hash:
        raise ValueError("Execution review manifest SHA256 mismatch")
    approval_bytes = approval_path.read_bytes()
    if (not _is_sha256(receipt["artifact_approval_receipt_sha256"])
            or hashlib.sha256(approval_bytes).hexdigest() != receipt["artifact_approval_receipt_sha256"]):
        raise ValueError("Execution review is bound to a different artifact approval receipt")
    approval = loads(approval_bytes.decode("utf-8-sig"))
    verify_receipt(pack, approval, list((approval.get("artifacts_sha256") or {}).keys()), "evaluate")
    prepared = manifest["source_code_sha256"]
    changed = {name for name in prepared if prepared[name] != executed[name]}
    changes = receipt["changes"]
    if (not changed or not isinstance(changes, dict) or set(changes) != changed
            or changed - EXECUTION_REVIEW_FINDINGS.keys()):
        raise ValueError("Execution review has missing/extra transitions or unauthorized source paths")
    for name, change in changes.items():
        _exact_fields(change, ("prepared_sha256", "executed_sha256", "finding_ids"), "Reviewed transition")
        findings = change["finding_ids"]
        if (change["prepared_sha256"] != prepared[name] or change["executed_sha256"] != executed[name]
                or not isinstance(findings, list) or not all(isinstance(item, str) for item in findings)
                or len(set(findings)) != len(findings) or set(findings) != EXECUTION_REVIEW_FINDINGS[name]):
            raise ValueError("Reviewed transition hash/finding IDs differ from the exact reviewed fix")
    _verify_test_evidence(receipt["test_evidence"], root, pack, manifest_hash, executed, reviewed_at)
    return receipt


def lineage_record(manifest: dict) -> dict:
    return {name: manifest[name] for name in LINEAGE_FIELDS}


def check_execution_review_expiry(lineage: dict) -> None:
    receipt = lineage.get("execution_code_review_receipt")
    if receipt is not None and not (
        _timestamp(receipt["reviewed_at"]) <= datetime.now(timezone.utc) < _timestamp(receipt["expires_at"])
    ):
        raise ValueError("Execution code review expired; do not submit further native work")


def verify_code_lineage(pack: Path, root: Path, *, execution_code_review: Path | None = None,
                        execution_code_review_sha256: str | None = None,
                        approval_receipt_path: Path | None = None) -> dict:
    """Keep the prepared manifest intact; a separately pinned code review can attest only named fixes."""
    if (execution_code_review is None) != (execution_code_review_sha256 is None):
        raise ValueError("Supply both --execution-code-review and --execution-code-review-sha256")
    manifest = verify_pack(pack)
    sources = manifest.get("source_code_sha256")
    if not isinstance(sources, dict) or not sources:
        raise ValueError("Evaluation pack must bind its scorer and execution source hashes")
    executed = {}
    for name, expected in sources.items():
        if not _is_sha256(expected):
            raise ValueError("Prepared source SHA256 is invalid")
        executed[name] = sha256(_root_file(root, name))
    prompts = manifest.get("prompt_sources_sha256")
    if not isinstance(prompts, dict) or not prompts:
        raise ValueError("Evaluation pack must bind its immutable prompts")
    for name, expected in prompts.items():
        if not _is_sha256(expected) or sha256(_root_file(root, name)) != expected:
            raise ValueError("Prepared prompt hash changed; execution review cannot authorize prompt changes")
    if (not _is_sha256(manifest.get("fixture_sha256"))
            or sha256(_root_file(root, r"data\northwind\insurance-fixtures.json")) != manifest["fixture_sha256"]):
        raise ValueError("Golden fixture hash changed; execution review cannot authorize fixture changes")
    receipt = None
    if execution_code_review is not None:
        receipt = _verify_execution_review(pack, root, manifest, executed, execution_code_review,
                                           execution_code_review_sha256, approval_receipt_path)
    elif sources != executed:
        raise ValueError("Evaluation source changed; a separately pinned reviewed-execution-code receipt is required")
    return {
        **manifest, "prepared_source_code_sha256": dict(sources), "executed_source_code_sha256": executed,
        "execution_code_review_receipt_sha256": execution_code_review_sha256,
        "execution_code_review_receipt": receipt,
    }


def audit_pack(path: Path, fixture_path: Path) -> dict:
    """This is a code/assistant audit, not professional insurance or human label review."""
    from evals.insurance import canonical_citations

    manifest = read_json(path / "manifest.json")
    annotations = rows(path / "annotation-candidates.jsonl")
    evaluations = rows(path / "evaluation-candidates.jsonl")
    fixtures = read_json(fixture_path)
    canonical = canonical_citations(fixtures)
    findings = []

    def finding(code, severity, detail, examples=()):
        findings.append({"code": code, "severity": severity, "detail": detail,
                         "affected_count": len(examples), "example_ids": list(examples)})

    hashes = {name: {"expected": digest, "actual": sha256(path / name),
                     "matches": digest == sha256(path / name)}
              for name, digest in manifest["files_sha256"].items()}
    bad_hashes = [name for name, value in hashes.items() if not value["matches"]]
    if bad_hashes:
        finding("artifact_hash_mismatch", "blocker", "Bytes differ from source manifest.", bad_hashes)
    invalid = [r["example_id"] for r in annotations if not valid_labels(r.get("expected"))]
    if invalid:
        finding("invalid_annotation_schema", "blocker", "Labels do not match extraction schema.", invalid)
    groups = defaultdict(list)
    families = defaultdict(set)
    for row in annotations:
        groups[normalized_input(row["input"])].append(row)
        families[row["family"]].add(row["split"])
    leakage = [{"normalized_input": key, "splits": sorted({r["split"] for r in group}),
                "example_ids": [r["example_id"] for r in group],
                "declared_families": [r["family"] for r in group]}
               for key, group in groups.items() if len({r["split"] for r in group}) > 1]
    if leakage:
        finding("normalized_template_cross_split_leakage", "blocker",
                f"{len(leakage)} ID-normalized template groups recur across splits; split-prefixed "
                "family names conceal semantic leakage. Distinct IDs do not create independent tests.",
                [r for group in leakage for r in group["example_ids"]])
    unreviewed = [r["example_id"] for r in annotations if r.get("review_status") != "accepted" or not r.get("reviewer")]
    if unreviewed:
        finding("no_individual_human_annotation", "blocker", "Synthetic suggestions, not human labels.", unreviewed)
    known = {r[k] for table, k in (("claims", "claim_id"), ("policies", "policy_id"),
                                  ("policyholders", "policyholder_id")) for r in fixtures[table]}
    unknown = [r["example_id"] for r in annotations
               if any(r["expected"].get(k) not in known for k in ("claim_id", "policy_id", "policyholder_id"))]
    if unknown:
        finding("unknown_ids_extraction_only", "warning",
                "80000-series IDs are legitimate extraction inputs, but no such lookup records exist.", unknown)
    if len(groups) < 30:
        finding("limited_representativeness", "warning",
                "Small synthetic English auto-only sample; no real claims, multilingual, operational "
                "distribution or qualified insurance validation; not training/model-readiness evidence.",
                [r["example_id"] for r in annotations])
    missing_ids = [r["example_id"] for r in annotations if any(r["expected"].get(k) is None
                   for k in ("claim_id", "policy_id", "policyholder_id"))]
    if not missing_ids:
        finding("missing_identifier_cases_absent", "warning", "All annotations supply three explicit IDs.")
    schema = read_json(path / "label-schema.json")
    unanchored = [name for name in ("claim_id", "policy_id", "policyholder_id")
                  if not (schema["properties"][name].get("pattern", "").startswith("^")
                          and schema["properties"][name].get("pattern", "").endswith("$"))]
    if unanchored:
        finding("identifier_schema_not_anchored", "warning",
                "JSON Schema patterns permit prefixed/suffixed matches; runtime full-match IDs are stricter.", unanchored)
    train_count = sum(row["split"] == "train" for row in annotations)
    if train_count < 10:
        finding("insufficient_sft_training_rows", "blocker",
                f"Only {train_count} suggested training rows; below the existing minimum ten-example "
                "training gate, independent of consent. Do not inflate this pack into a training experiment.")
    for field in ("expected_behavior", "ground_truth"):
        absent = [r["example_id"] for r in evaluations if not r.get(field)]
        if absent:
            finding("evaluation_missing_" + field, "blocker",
                    f"Missing {field}; native judge mapping cannot infer a gold contract.", absent)
    if not any(r["target_role"] == "triage" for r in evaluations):
        finding("triage_evaluation_absent", "warning", "No triage case despite triage candidate pack.")
    unbound = [r["example_id"] for r in evaluations if not isinstance(r.get("context"), dict)
               and "fixture_sha256" not in str(r.get("context", ""))]
    if unbound:
        finding("fixture_and_trace_lineage_absent", "blocker",
                "Generic context filename is not fixture bytes, tool traces or queue evidence.", unbound)
    missing_metadata, bad_refs = [], []
    for row in evaluations:
        expected = row["expected"]
        ref = expected.get("policy_ref")
        if ref and ref not in canonical:
            bad_refs.append(row["example_id"])
        if ref and not all(k in expected for k in ("effective_from", "effective_to", "source_uri", "section_id")):
            missing_metadata.append(row["example_id"])
    if bad_refs:
        finding("noncanonical_citation", "blocker", "Reference is not canonical.", bad_refs)
    if missing_metadata:
        finding("citation_truth_incomplete", "blocker",
                "Existing references are canonical where present, but no complete dates/section/source "
                "support contract is bound to them.", missing_metadata)
    effects = [r["example_id"] for r in evaluations if r.get("side_effects_possible")]
    if effects:
        finding("side_effectful_cases_require_controlled_execution", "blocker",
                "Draft/orchestrator cases may write HITL records. Never blindly replay; require "
                "unique test workflows and reconciled cleanup limited to records created by that run.", effects)
    if not any("rental" in r["query"].casefold() or "glass" in r["query"].casefold() for r in evaluations):
        finding("endorsement_coverage_absent", "warning",
                "No WA rental or CO glass negative-applicability cases for fixture v2.")
    parity = {}
    for split in ("train", "validation", "test"):
        sft = path / f"suggested-{split}.jsonl"
        if sft.exists():
            suggested = rows(sft)
            selected = [r for r in annotations if r["split"] == split]
            parity[split] = len(suggested) == len(selected) and all(
                row["messages"][1]["content"] == source["input"]
                and loads(row["messages"][2]["content"]) == source["expected"]
                for row, source in zip(suggested, selected))
    return {
        "schema_version": 2, "review_kind": "assistant_code_audit_not_human_annotation",
        "qualified_insurance_review": False, "user_approved": False,
        "source_pack": str(path), "source_manifest_sha256": sha256(path / "manifest.json"),
        "source_files_sha256": {p.name: sha256(p) for p in sorted(path.iterdir()) if p.is_file()},
        "fixture_sha256": sha256(fixture_path), "fixture_dataset_version": fixtures["dataset_version"],
        "facts": {
            "annotation_rows": len(annotations), "evaluation_rows": len(evaluations),
            "rubrics": len(read_json(path / "native-evaluator-definitions.json")),
            "split_counts": dict(Counter(r["split"] for r in annotations)),
            "unique_normalized_templates": len(groups), "leaking_template_groups": len(leakage),
            "declared_family_cross_split_groups": sum(len(s) > 1 for s in families.values()),
            "unknown_id_annotation_rows": len(unknown), "invalid_label_rows": len(invalid),
            "canonical_linked_claims": len(fixtures["claims"]), "canonical_clauses": len(canonical),
            "side_effectful_evaluation_rows": len(effects), "suggested_sft_label_parity": parity,
            "human_annotations_verified": 0, "native_evaluations_verified": 0,
        },
        "hash_checks": hashes, "leakage_groups": leakage, "findings": findings,
        "finding_counts": dict(Counter(f["severity"] for f in findings)),
        "disposition": "not_approved_requires_user_artifact_acceptance_before_manual_upload_or_evaluation",
        "training": "blocked_separate_explicit_insurance_training_and_budget_approval_required",
    }


def approval_template(pack: Path, artifacts: list[str] | None = None) -> dict:
    manifest = verify_pack(pack)
    names = artifacts or ["evaluation-candidates.jsonl", "annotation-candidates.jsonl"]
    if any(name not in manifest["files_sha256"] for name in names):
        raise ValueError("Approval names must be hashed manifest artifacts")
    return {
        "schema_version": 2, "approved": False, "actor_type": "human_user", "approver": "",
        "approval_mode": "demo_acceptance_not_individual_annotation",
        "approved_at": "", "expires_at": "", "confirmation_source": "", "confirmation_text": "",
        "manifest_sha256": sha256(pack / "manifest.json"),
        "artifacts_sha256": {name: manifest["files_sha256"][name] for name in names},
        "scope": "evaluation_only", "no_training": True,
        "operations": ["manual_upload", "evaluate"],
        "individual_human_annotations": 0,
        "evaluation_budget": {"approved": False, "max_samples": 8, "max_concurrency": 3,
                              "max_total_tokens": 200000, "cost_cap_usd": "1.00",
                              "acknowledge_conditional_bound_not_service_billing_cap": False},
        "authentication_note": "Operator-recorded user attestation, not a cryptographic identity signature.",
    }


def verify_receipt(pack: Path, receipt: dict, artifacts: list[str], operation: str) -> dict:
    manifest = verify_pack(pack)
    if (receipt.get("schema_version") != 2 or receipt.get("approved") is not True
            or receipt.get("actor_type") != "human_user"
            or receipt.get("scope") != "evaluation_only" or receipt.get("no_training") is not True
            or receipt.get("individual_human_annotations") != 0
            or receipt.get("approval_mode") not in ("demo_acceptance_not_individual_annotation",
                                                   "per_artifact_acceptance_not_individual_annotation")):
        raise ValueError("Explicit evaluation-only user acceptance required; this is not human annotation/training")
    approver = receipt.get("approver")
    if (not isinstance(approver, str) or not approver.strip()
            or re.search(r"\b(assistant|copilot|model|agent|gpt|claude|automation)\b", approver, re.I)):
        raise ValueError("Approver must be the actual named user, not assistant/model")
    if not all(isinstance(receipt.get(k), str) and receipt[k].strip()
               for k in ("confirmation_source", "confirmation_text")):
        raise ValueError("Record the actual explicit user confirmation and its conversation/source reference")
    try:
        start = datetime.fromisoformat(receipt["approved_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(receipt["expires_at"].replace("Z", "+00:00"))
        current = datetime.now(timezone.utc)
        if start.tzinfo is None or end.tzinfo is None or not start <= current < end:
            raise ValueError("Approval expired or not yet valid")
    except (KeyError, TypeError) as exc:
        raise ValueError("Approval requires timezone-aware dates") from exc
    if receipt.get("manifest_sha256") != sha256(pack / "manifest.json"):
        raise ValueError("Approval manifest hash mismatch")
    operations = receipt.get("operations")
    if (not isinstance(operations, list) or not operations
            or any(item not in ("manual_upload", "evaluate") for item in operations)
            or len(set(operations)) != len(operations)
            or not isinstance(receipt.get("artifacts_sha256"), dict)):
        raise ValueError("Approval operations/artifact hash schema is invalid")
    if operation not in ("manual_upload", "evaluate") or operation not in operations:
        raise ValueError("Requested operation not approved")
    if not artifacts or len(set(artifacts)) != len(artifacts):
        raise ValueError("Select unique approved artifacts")
    for name in artifacts:
        if (name not in manifest["files_sha256"]
                or receipt.get("artifacts_sha256", {}).get(name) != manifest["files_sha256"][name]):
            raise ValueError(f"Artifact not approved or changed: {name}")
    return receipt


def require_training_approval(manifest: dict, budget: dict | None = None) -> None:
    if manifest.get("business_domain") == "insurance":
        raise ValueError("Insurance candidate packs are evaluation_only/no_training. A separate reviewed "
                         "training release, explicit user training consent, insurance model admission "
                         "and hash-bound approved training budget are required.")
