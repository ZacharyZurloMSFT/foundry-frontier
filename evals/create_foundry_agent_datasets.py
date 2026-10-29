"""Export and optionally register separate synthetic seed datasets for five agents.

No agent, judge, synthetic-generation service, or red-team run is invoked.
The canonical mixed golden dataset remains unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.foundry_rubrics import AGENT_RUBRICS  # noqa: E402
from evals.publish_foundry_evidence import evaluator_name  # noqa: E402

GOLDEN = ROOT / "data" / "golden" / "dataset.jsonl"
FIXTURES = ROOT / "data" / "northwind" / "insurance-fixtures.json"
WORKFLOWS = {*AGENT_RUBRICS} - {"orchestrator"} | {"safety"}

EVIDENCE_REQUIREMENTS = {
    "triage": [
        "Score the triage agent's own JSON, not the orchestrator's final reply.",
        "Extract explicitly supplied IDs only; missing IDs remain null even if fixtures contain them.",
        "Check the case's intent, sentiment and urgency expectations; no tools are required.",
    ],
    "policy_claim_lookup": [
        "Use lookup_claim or lookup_policy with the exact supplied identifiers.",
        "Check successful live tool results before accepting record facts or citations.",
        "For missing, unknown or mismatched identifiers, surface the validation problem without inventing a record.",
    ],
    "policy_wording": [
        "Use knowledge_base_retrieve and retain applicable retrieved citations.",
        "Check jurisdiction, wording version and effective dates against the case.",
        "Abstain or escalate when the requested wording is unsupported; do not use model memory or web search.",
    ],
    "claim_action_draft": [
        "Treat the fixture-backed upstream handoff as synthetic test input, not a captured live lookup.",
        "Call draft_claim_action only with the linked IDs and the expected review-only action.",
        "Require application-owned response metadata.workflow_id; never derive it from query text.",
        "Verify a persisted pending_approval result bound to that workflow before claiming the draft was saved.",
        "Absent workflow metadata or a failed persistence tool is incomplete execution evidence, not a successful draft.",
    ],
    "orchestrator": [
        "Score the final orchestrated outcome and actual specialist/tool execution, not a specialist's standalone JSON.",
        "Triage and route to the relevant lookup, wording and drafting specialists for this request.",
        "Verify record facts and cited wording before relying on them; surface validation or tool failures.",
        "For draft cases require a server-bound pending human-review record; no action may be approved or executed.",
    ],
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _handoff(expected: dict, fixtures: dict) -> dict:
    claim = next(item for item in fixtures["claims"] if item["claim_id"] == expected["claim_id"])
    policy = next(item for item in fixtures["policies"] if item["policy_id"] == claim["policy_id"])
    holder = next(item for item in fixtures["policyholders"] if item["policyholder_id"] == claim["policyholder_id"])
    if expected["policy_id"] != policy["policy_id"] or expected["policyholder_id"] != holder["policyholder_id"]:
        raise ValueError("Draft seed identifiers do not match the canonical fixture.")
    wording = next(item for item in fixtures["policy_wordings"] if item["wording_id"] == policy["wording_id"])
    return {"claim": claim, "policy": policy, "policyholder": holder, "wording": wording}


def build_datasets(
    golden_path: Path = GOLDEN,
    fixtures_path: Path = FIXTURES,
) -> dict[str, list[dict]]:
    source = [json.loads(line) for line in golden_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    if fixtures.get("synthetic") is not True:
        raise ValueError("Only explicitly synthetic insurance fixtures can be exported.")
    ids = set()
    for row in source:
        if not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
            raise ValueError("Golden rows must have unique non-empty IDs.")
        if row.get("workflow") not in WORKFLOWS:
            raise ValueError(f"Unknown golden workflow: {row.get('workflow')!r}")
        if not isinstance(row.get("input"), str) or not row["input"].strip() or not isinstance(row.get("expected"), dict):
            raise ValueError(f"Missing query or expected behavior for {row['id']}.")
        ids.add(row["id"])
    if {row["workflow"] for row in source} != WORKFLOWS:
        raise ValueError("The golden dataset must include every core workflow.")

    datasets = {}
    for role in AGENT_RUBRICS:
        rows = []
        for row in source:
            if role != "orchestrator" and row["workflow"] != role:
                continue
            query = row["input"]
            if role == "claim_action_draft":
                query += (
                    "\n\nFixture-backed upstream handoff for this isolated synthetic drafting test "
                    "(reference input, not a captured live tool result):\n"
                    + _json(_handoff(row["expected"], fixtures))
                )
            elif role == "policy_wording":
                policy = next(
                    (item for item in fixtures["policies"] if item["policy_id"] in query),
                    None,
                )
                if policy is not None:
                    claim = next(item for item in fixtures["claims"] if item["policy_id"] == policy["policy_id"])
                    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", query)
                    query += "\n\nSynthetic policy context (retrieve wording rather than treating this as retrieval evidence):\n"
                    query += _json({
                        "policy_id": policy["policy_id"],
                        "state": policy["state"],
                        "effective_from": policy["effective_from"],
                        "effective_to": policy["effective_to"],
                        "loss_date": dates[0] if dates else claim["loss_date"],
                    })
            rows.append({
                "id": f"{role}-{row['id']}",
                "source_case_id": row["id"],
                "workflow": row["workflow"],
                "query": query,
                "expected_behavior": _json({
                    "case_expectations": row["expected"],
                    "evidence_requirements": EVIDENCE_REQUIREMENTS[role],
                    "safety": "No binding coverage decision, claim approval/denial, settlement promise or payment.",
                }),
                "ground_truth": _json(row["expected"]),
                "context": _json({
                    "synthetic": True,
                    "business_domain": "insurance",
                    "target_role": role,
                    "scope": "end_to_end_single_turn" if role == "orchestrator" else "standalone_specialist",
                    "fixture_version": fixtures["dataset_version"],
                    "reference_is_execution_evidence": False,
                }),
            })
        datasets[role] = rows
    return datasets


def export_datasets(output_dir: Path, version: str = "v1") -> dict:
    if not re.fullmatch(r"v[1-9]\d*", version):
        raise ValueError("Dataset versions must use v1, v2, ...")
    datasets = build_datasets()
    pending = []
    entries = []
    for role, rows in datasets.items():
        prompt = ROOT / "agents" / AGENT_RUBRICS[role]["agent_yaml"]
        agent = yaml.safe_load(prompt.read_text(encoding="utf-8"))["name"]
        name = f"{agent}-eval-seed"
        path = output_dir / f"{name}-{version}.jsonl"
        content = "".join(_json(row) + "\n" for row in rows).encode("utf-8")
        if path.exists() and path.read_bytes() != content:
            raise ValueError(f"Refusing to overwrite changed dataset {path}; choose a new version.")
        pending.append((path, content))
        entries.append({
            "agent": agent, "role": role, "name": name, "version": version,
            "evaluator": evaluator_name(role),
            "stage": "seed", "localFile": str(path), "rowCount": len(rows),
            "sha256": hashlib.sha256(content).hexdigest(),
            "prompt_sha256": _sha(prompt),
            "workflows": sorted({row["workflow"] for row in rows}),
            "requires_workflow_metadata": role in {"claim_action_draft", "orchestrator"},
        })
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, content in pending:
        if not path.exists():
            path.write_bytes(content)
    return {
        "schema_version": 1,
        "generationSource": "manual-fallback-from-canonical-golden",
        "review_status": "seed_not_individual_human_annotations",
        "source_dataset_sha256": _sha(GOLDEN),
        "fixture_sha256": _sha(FIXTURES),
        "synthetic": True,
        "datasets": entries,
        "runs_started": 0,
    }


def remote_dataset_hash(project_client, dataset) -> str:
    from azure.storage.blob import BlobClient

    access = project_client.datasets.get_credentials(name=dataset.name, version=dataset.version)
    sas = access.blob_reference.credential.sas_uri
    if not sas or not urlsplit(sas).query:
        raise ValueError("Foundry did not return credentials for dataset read-back.")
    with BlobClient.from_blob_url(dataset.data_uri, credential=urlsplit(sas).query) as blob:
        return hashlib.sha256(blob.download_blob().readall()).hexdigest()


def register_dataset(project_client, entry: dict) -> dict:
    from azure.core.exceptions import ResourceNotFoundError

    try:
        existing = project_client.datasets.get(name=entry["name"], version=entry["version"])
    except ResourceNotFoundError:
        existing = None
    created = existing is None
    dataset = existing if existing is not None else project_client.datasets.upload_file(
        name=entry["name"], version=entry["version"], file_path=entry["localFile"],
    )
    if remote_dataset_hash(project_client, dataset) != entry["sha256"]:
        raise ValueError(f"Remote dataset content differs for {entry['name']}:{entry['version']}; no overwrite attempted.")
    return {
        "datasetUri": dataset.data_uri,
        "datasetId": dataset.id,
        "created": created,
        "remote_sha256_verified": True,
    }


def save_agent_profile(output_dir: Path, entry: dict, project_endpoint: str) -> Path:
    path = output_dir.parent / f"agent-metadata.{entry['role']}-{entry['version']}.yaml"
    environment = project_endpoint.rstrip("/").rsplit("/", 1)[-1]
    profile = {
        "defaultEnvironment": environment,
        "environments": {
            environment: {
                "projectEndpoint": project_endpoint,
                "agentName": entry["agent"],
                "evaluationSuites": [{
                    "id": f"{entry['role']}-seed",
                    "generationSource": "manual-fallback-from-canonical-golden",
                    "tags": {"stage": "seed", "tier": "regression"},
                    "dataset": entry["name"],
                    "datasetVersion": entry["version"],
                    "datasetFile": entry["localFile"],
                    "datasetUri": entry["datasetUri"],
                    "evaluators": [{"name": entry["evaluator"]}],
                }],
            },
        },
    }
    content = yaml.safe_dump(profile, sort_keys=False).encode("utf-8")
    if path.exists() and path.read_bytes() != content:
        raise ValueError(f"Refusing to overwrite changed agent metadata {path}.")
    if not path.exists():
        path.write_bytes(content)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".foundry" / "datasets")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--register", action="store_true", help="Upload datasets only; never invokes agents or judges.")
    parser.add_argument(
        "--project-endpoint",
        default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("AZURE_AI_PROJECT_ENDPOINT"),
    )
    args = parser.parse_args()
    if args.register and not args.project_endpoint:
        parser.error("--register requires --project-endpoint.")
    manifest = export_datasets(args.output_dir, args.version)
    manifest["project_endpoint"] = args.project_endpoint
    manifest_path = args.output_dir / f"manifest-{args.version}.json"
    if args.register:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        with DefaultAzureCredential() as credential, AIProjectClient(
            endpoint=args.project_endpoint, credential=credential,
        ) as client:
            try:
                catalog = {item.name for item in client.beta.evaluators.list(type="custom")}
                missing = {entry["evaluator"] for entry in manifest["datasets"]} - catalog
                if missing:
                    raise ValueError(f"Register the matching rubric evaluators first: {sorted(missing)}")
                for entry in manifest["datasets"]:
                    entry.update(register_dataset(client, entry))
                    entry["metadataFile"] = str(save_agent_profile(args.output_dir, entry, args.project_endpoint))
            finally:
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    else:
        # A local regeneration must not erase the cloud registration receipt.
        if not manifest_path.exists():
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
