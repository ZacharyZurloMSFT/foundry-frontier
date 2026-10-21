"""Verify existing hosted responses and serving versions; never starts inference.

Record one completed response ID per insurance role after separately authorized
smoke calls. Outer synthetic responses must be retained for readback. Internal
model calls continue to use store=false. This verifier reads those responses and
the isolated pending-review row, then writes a new, secret-free activation receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.hosted.config import INSURANCE_PROMPTS
from apps.api.app.insurance_config import (
    INSURANCE_AGENT_NAMES, INSURANCE_CONNECTION, INSURANCE_INDEX, INSURANCE_KB, configuration_hash,
    insurance_routing, validate_fleet_receipt, validate_insurance_connection,
)
from scripts.orders_agent_package import PACKAGE_SOURCES

TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
API_ID = (
    "/subscriptions/137c905b-7734-479e-acc5-ab8222462936/resourceGroups/rg-northwind-apps"
    "/providers/Microsoft.App/containerApps/ca-api"
)
HASH_KEYS = ("source_sha256", "config_sha256", "prompt_sha256", "source_manifest_sha256")


def artifact_identity(receipt: dict, archive_path: Path) -> dict:
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != receipt["source_sha256"]:
        raise ValueError("Uploaded artifact bytes do not match their deployment receipt")
    expected = {*PACKAGE_SOURCES, "main.py", "requirements.txt",
                "agents/insurance/domain.py", "agents/insurance/config.py", "agents/hosted/build.json"}
    with zipfile.ZipFile(archive_path) as archive:
        if (len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected
                or sum(info.file_size for info in archive.infolist()) > 5 * 1024 * 1024):
            raise ValueError("Hosted archive differs from its bounded source allowlist")
        raw = archive.read("agents/hosted/build.json")
        manifest = json.loads(raw)
        if (manifest.get("schema_version") != 1 or manifest.get("source_clean") is not True
                or manifest.get("source_commit") != receipt["source_commit"]
                or not isinstance(manifest.get("source_commit"), str)
                or len(manifest["source_commit"]) != 40
                or any(char not in "0123456789abcdef" for char in manifest["source_commit"])
                or set(manifest.get("files_sha256", {})) != expected - {"agents/hosted/build.json"}):
            raise ValueError("Hosted archive requires a complete clean-source manifest")
        for name, digest in manifest["files_sha256"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("Hosted archive file hashes do not match their manifest")
        prompt = archive.read("agents/" + INSURANCE_PROMPTS[receipt["role"]])
        if (hashlib.sha256(prompt).hexdigest() != receipt["prompt_sha256"]
                or hashlib.sha256(raw).hexdigest() != receipt["source_manifest_sha256"]):
            raise ValueError("Deployment does not bind the selected canonical insurance prompt")
    return manifest


def verify_runtime(receipt: dict, version: dict, detail: dict, response: dict) -> dict:
    role = receipt["role"]
    definition = version.get("definition") or {}
    metadata = response.get("metadata") or {}
    rules = (detail.get("agent_endpoint") or {}).get("version_selector", {}).get("version_selection_rules", [])
    if (receipt.get("agent") != INSURANCE_AGENT_NAMES.get(role)
            or version.get("version") != receipt["version"] or version.get("status") != "active"
            or len(rules) != 1 or rules[0].get("agent_version") != receipt["version"]
            or rules[0].get("traffic_percentage") != 100
            or definition.get("code_configuration", {}).get("content_hash") != receipt["source_sha256"]
            or configuration_hash(definition.get("environment_variables") or {}) != receipt["config_sha256"]):
        raise ValueError("Live serving artifact, version or configuration drifted from deployment")
    if (response.get("status") != "completed" or metadata.get("business_domain") != "insurance"
            or metadata.get("agent_role") != role or metadata.get("source_clean") != "true"
            or metadata.get("source_commit") != receipt["source_commit"]
            or metadata.get("model_deployment") != receipt["model"]
            or any(metadata.get(key) != receipt[key] for key in HASH_KEYS if key != "source_sha256")):
        raise ValueError("Completed response does not prove the expected runtime source/config/prompt/model binding")
    output = response.get("output") or []
    if any(item.get("error") or item.get("status") in ("failed", "incomplete") for item in output):
        raise ValueError("A grounding or delegation operation failed")
    calls = {item.get("name") for item in output if item.get("type") in ("mcp_call", "function_call")}
    required = {
        "policy_claim_lookup": {"lookup_claim"},
        "policy_wording": {"knowledge_base_retrieve"},
        "claim_action_draft": {"draft_claim_action"},
        "orchestrator": {"triage", "policy_claim_lookup", "policy_lookup"},
    }.get(role, set())
    if not required <= calls:
        raise ValueError("Response is missing required insurance grounding/delegation evidence")
    if role == "orchestrator" and "claim_action_draft" in calls:
        raise ValueError("The read-only orchestrator smoke must not create a second review draft")
    return {
        "name": receipt["agent"], "version": receipt["version"], "runtime_verified": True,
        "response_id": response["id"], **{key: receipt[key] for key in HASH_KEYS},
    }


def pending_draft(response: dict, workflow: str) -> dict:
    output = response.get("output") or []
    calls = {item.get("call_id") for item in output
             if item.get("type") == "function_call" and item.get("name") == "draft_claim_action"}
    for item in output:
        if not (
            (item.get("type") == "function_call_output" and item.get("call_id") in calls)
            or (item.get("type") == "mcp_call" and item.get("name") == "draft_claim_action")
        ):
            continue
        value = item.get("output")
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, list):
            value = {"content": value}
        if isinstance(value, dict) and set(value) == {"result"} and isinstance(value["result"], str):
            value = json.loads(value["result"])
        if isinstance(value, dict) and "content" in value:
            texts = [part["text"] for part in value["content"] if part.get("type") == "text"]
            if len(texts) != 1:
                raise ValueError("Pending draft requires one structured tool result")
            value = json.loads(texts[0])
        if (isinstance(value, dict) and value.get("status") == "pending_approval"
                and value.get("workflow_id") == workflow and value.get("execution_status") == "not_executed"
                and value.get("synthetic") is True and value.get("hitl_id") and isinstance(value.get("draft"), dict)):
            return value
    raise ValueError("Draft response contains no persisted, workflow-bound pending-review tool evidence")


def verify_knowledge(project, search_endpoint: str, http) -> None:
    connection = project.connections.get(INSURANCE_CONNECTION, include_credentials=True).as_dict()
    validate_insurance_connection(connection, search_endpoint)
    credentials = connection.get("credentials") or {}
    key = credentials.get("api-key")
    if credentials.get("type") != "CustomKeys" or not isinstance(key, str) or not key:
        raise ValueError("Cannot verify insurance knowledge with the existing connection credential")
    source_name = "northwind-insurance-knowledge-ks"
    bodies = {}
    for kind, name in (("knowledgebases", INSURANCE_KB), ("knowledgesources", source_name), ("indexes", INSURANCE_INDEX)):
        response = http.get(
            f"{search_endpoint.rstrip('/')}/{kind}/{name}", params={"api-version": "2026-05-01-preview"},
            headers={"api-key": key}, follow_redirects=False,
        )
        if not response.is_success:
            raise ValueError("Cannot independently verify the existing insurance KB/index association")
        bodies[kind] = response.json()
    if ([item.get("name") for item in bodies["knowledgebases"].get("knowledgeSources", [])] != [source_name]
            or bodies["knowledgesources"].get("kind") != "searchIndex"
            or bodies["knowledgesources"].get("searchIndexParameters", {}).get("searchIndexName") != INSURANCE_INDEX
            or bodies["indexes"].get("name") != INSURANCE_INDEX):
        raise ValueError("Insurance knowledge is bound to an unexpected source or index")


def verify(paths: list[Path], observations: dict, project, http, arm_token) -> dict:
    receipts = {}
    for path in paths:
        receipt = json.loads(path.read_bytes())
        role = receipt["role"]
        if role not in INSURANCE_AGENT_NAMES or role in receipts:
            raise ValueError("Exactly one deployment receipt is required per insurance role")
        artifact_identity(receipt, path.with_suffix(".zip"))
        if receipt.get("project_endpoint") != insurance_routing()["INSURANCE_PROJECT_ENDPOINT"]:
            raise ValueError("All agents must target the retained claims project")
        receipts[role] = receipt
    if set(receipts) != set(INSURANCE_AGENT_NAMES) or set(observations.get("responses", {})) != set(receipts):
        raise ValueError("Verification must include all five insurance roles")
    commits = {receipt["source_commit"] for receipt in receipts.values()}
    if len(commits) != 1:
        raise ValueError("Fleet candidates must share one reviewed source commit")
    workflow = observations.get("workflow_id", "")
    if not isinstance(workflow, str) or not workflow.startswith("wf-insurance-verification-") or len(workflow) > 120:
        raise ValueError("Use a new isolated insurance verification workflow")
    policy = project.agents.get_version(receipts["policy_wording"]["agent"], receipts["policy_wording"]["version"]).as_dict()
    verify_knowledge(project, policy["definition"]["environment_variables"]["AZURE_SEARCH_ENDPOINT"], http)
    verified, responses = {}, {}
    for role, receipt in receipts.items():
        name = receipt["agent"]
        version = project.agents.get_version(name, receipt["version"]).as_dict()
        detail = project.agents.get(name).as_dict()
        with project.get_openai_client(agent_name=name, max_retries=0) as client:
            response = client.responses.retrieve(observations["responses"][role]).model_dump()
        if (response.get("metadata") or {}).get("workflow_id") != workflow:
            raise ValueError("Response belongs to a different verification workflow")
        verified[role] = verify_runtime(receipt, version, detail, response)
        responses[role] = response
    approval = pending_draft(responses["claim_action_draft"], workflow)
    arm = http.get(
        f"https://management.azure.com{API_ID}", params={"api-version": "2024-03-01"},
        headers={"Authorization": "Bearer " + arm_token()}, follow_redirects=False,
    )
    if not arm.is_success:
        raise ValueError("Cannot read the existing API for independent queue verification")
    fqdn = arm.json()["properties"]["configuration"]["ingress"]["fqdn"]
    if not isinstance(fqdn, str) or not fqdn.startswith("ca-api.") or not fqdn.endswith(".azurecontainerapps.io"):
        raise ValueError("Unexpected API ingress hostname")
    queue = http.get(f"https://{fqdn}/hitl", params={"workflow_id": workflow}, follow_redirects=False)
    if not queue.is_success:
        raise ValueError("Cannot independently verify pending-review persistence")
    items = queue.json().get("items")
    if (not isinstance(items, list) or len(items) != 1
            or items[0].get("id") != approval["hitl_id"] or items[0].get("workflowId") != workflow
            or items[0].get("status") != "pending" or items[0].get("kind") != "insurance_claim_action"
            or items[0].get("execution_status") != "not_executed" or items[0].get("synthetic") is not True
            or items[0].get("payload") != approval["draft"]):
        raise ValueError("Queue does not confirm exactly one unchanged, non-executed synthetic pending draft")
    result = {
        "state": "verified", "source_commit": next(iter(commits)),
        "routing_sha256": configuration_hash(insurance_routing()), "agents": verified,
        "workflow_id": workflow, "hitl_id": approval["hitl_id"],
        "synthetic": True, "draft_execution_status": "not_executed",
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    validate_fleet_receipt(result, result["source_commit"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployments", nargs=5, type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output path; existing verification evidence is immutable")
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential, get_bearer_token_provider

    with AzureCliCredential(tenant_id=TENANT) as credential, httpx.Client(timeout=30) as http:
        with AIProjectClient(
            endpoint=insurance_routing()["INSURANCE_PROJECT_ENDPOINT"], credential=credential, allow_preview=True,
            retry_total=0,
        ) as project:
            result = verify(
                args.deployments, json.loads(args.observations.read_bytes()), project, http,
                get_bearer_token_provider(credential, "https://management.azure.com/.default"),
            )
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({"state": result["state"], "receipt_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
