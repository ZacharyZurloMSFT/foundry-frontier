"""Publish only synthetic insurance knowledge; dry-run unless --apply is explicit.

Uses push indexing and minimal/extractive KB retrieval: no blob/indexer cascade,
embeddings, model deployment, role grant, network change, or any other bootstrap step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
DATA_GROUP = "rg-northwind-data"
SEARCH_SERVICE = "srch-zurlo-northwind"
SEARCH_ENDPOINT = f"https://{SEARCH_SERVICE}.search.windows.net"
INDEX_NAME = "northwind-insurance-knowledge"
SOURCE_NAME = "northwind-insurance-knowledge-ks"
KB_NAME = "northwind-insurance-kb"
CONNECTION_NAME = "nw-insurance-claims-kb-mcp"
PROJECT_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-northwind-ai"
    "/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind"
    "/projects/nw-insurance-claims"
)
SEARCH_API = "2026-05-01-preview"
ARM_API = "2025-10-01-preview"
OWNER = "Northwind synthetic insurance publisher v1"
DISCLAIMER = (
    "Entirely fictional synthetic insurance demonstration. Not a real policy, "
    "coverage determination, claim decision, payment authority, or legal advice."
)


def required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing or invalid " + label + ".")
    return value


def _markdown_metadata(text: str) -> dict:
    version = re.search(r"\*\*Version:\*\*\s*([0-9]+(?:\.[0-9]+)*)", text)
    effective = re.search(r"\*\*Effective:\*\*\s*(\d{4}-\d{2}-\d{2})(?:\s+through\s+(\d{4}-\d{2}-\d{2}))?", text)
    jurisdiction = re.search(r"\*\*Jurisdiction:\*\*\s*([^\.]+)", text)
    reference = re.search(r"\*\*(?:Source(?: reference)?|Document ID):\*\*\s*([A-Za-z0-9-]+)", text)
    if not all((version, effective, jurisdiction, reference)) or not re.search(r"\bsynthetic\b", text, re.I):
        raise ValueError("Insurance seed markdown must declare synthetic provenance, reference, version, effective date and jurisdiction.")
    jurisdiction_text = " ".join(jurisdiction.group(1).split())
    if "fictional" not in jurisdiction_text.lower():
        raise ValueError("Insurance seed jurisdiction must be explicitly fictional.")
    states = [
        state for state, name in (("WA", "Washington"), ("CO", "Colorado"))
        if name in jurisdiction_text or re.search(r"\b" + state + r"\b", jurisdiction_text)
    ]
    if not states:
        raise ValueError("Insurance seed must identify a supported fictional jurisdiction.")
    start = date.fromisoformat(effective.group(1))
    end = date.fromisoformat(effective.group(2)) if effective.group(2) else None
    if end is not None and start > end:
        raise ValueError("Insurance seed effective dates are reversed.")
    return {
        "policy_ref": reference.group(1),
        "version": version.group(1),
        "effective_from": start.isoformat(),
        "effective_to": end.isoformat() if end is not None else None,
        "jurisdiction": jurisdiction_text,
        "state": states[0] if len(states) == 1 else None,
        "states": states,
    }


def documents(root: Path = ROOT) -> list[dict]:
    fixture_path = root / "data" / "northwind" / "insurance-fixtures.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict) or fixture.get("synthetic") is not True or fixture.get("business_domain") != "insurance":
        raise ValueError("Canonical fixtures must be explicitly synthetic insurance data.")
    dataset_version = required_text(fixture.get("dataset_version"), "dataset version")
    wordings = fixture.get("policy_wordings")
    if not isinstance(wordings, list) or not wordings:
        raise ValueError("Canonical fixtures require policy wordings.")
    common = {
        "business_domain": "insurance", "synthetic": True, "fictional": True,
        "publisher": OWNER, "dataset_version": dataset_version,
    }
    result = []
    for i, wording in enumerate(wordings):
        if not isinstance(wording, dict) or wording.get("synthetic") is not True:
            raise ValueError("Every policy wording must be explicitly synthetic.")
        wording_id = required_text(wording.get("wording_id"), "wording ID")
        title = required_text(wording.get("title"), "wording title")
        if "fictional" not in title.lower():
            raise ValueError("Policy wording titles must disclose fictional provenance.")
        state = required_text(wording.get("state"), "wording state")
        if state not in ("WA", "CO"):
            raise ValueError("Unsupported fictional wording jurisdiction.")
        start = date.fromisoformat(required_text(wording.get("effective_from"), "effective date"))
        end = date.fromisoformat(required_text(wording.get("effective_to"), "end date"))
        if start > end:
            raise ValueError("Wording effective dates are reversed.")
        version = required_text(wording.get("version"), "wording version")
        sections = wording.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ValueError("Policy wordings require cited sections.")
        for j, section in enumerate(sections):
            if not isinstance(section, dict):
                raise ValueError("Policy wording sections must be objects.")
            section_id = required_text(section.get("section_id"), "section ID")
            reference = required_text(section.get("policy_ref"), "policy reference")
            uri = required_text(section.get("source_uri"), "source URI")
            if reference != f"{wording_id}#{section_id}" or uri != f"urn:northwind:synthetic:policy:{reference}":
                raise ValueError("Wording citations must match their canonical form and section.")
            content = required_text(section.get("text"), "wording content")
            result.append({
                **common,
                "id": "wording-" + hashlib.sha256(reference.encode()).hexdigest(),
                "title": title, "document_id": wording_id, "section_id": section_id,
                "doc_type": "policy_wording", "version": version, "state": state,
                "states": [state], "jurisdiction": "fictional " + state,
                "effective_from": start.isoformat(), "effective_to": end.isoformat(),
                "policy_ref": reference, "source_uri": uri,
                "source_path": f"data/northwind/insurance-fixtures.json#/policy_wordings/{i}/sections/{j}",
                "content": (
                    f"{DISCLAIMER}\nForm: {wording_id}; version: {version}; state: {state}; "
                    f"effective: {start} through {end}.\nCitation: {reference} ({uri}).\n{content}"
                ),
            })

    manifest = json.loads((root / "data" / "seed" / "insurance-corpus.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("Insurance seed manifest must be a nonempty list.")
    seed_root = (root / "data" / "northwind" / "departments").resolve()
    for entry in manifest:
        if not isinstance(entry, dict):
            raise ValueError("Insurance seed entries must be objects.")
        relative = required_text(entry.get("source"), "seed source")
        path = root.joinpath(*relative.split("/")).resolve()
        if not path.is_relative_to(seed_root) or path.suffix != ".md" or path.parent.name != "seed":
            raise ValueError("Insurance seed source must stay inside department seed markdown.")
        text = path.read_text(encoding="utf-8")
        section = entry.get("section")
        if section is not None:
            section = required_text(section, "seed section")
            lines = text.splitlines()
            if lines.count(section) != 1 or not section.startswith("## "):
                raise ValueError("Insurance section selector must identify one exact level-two heading.")
            start = lines.index(section)
            stop = next((i for i in range(start + 1, len(lines)) if re.match(r"^#{1,2} ", lines[i])), len(lines))
            text = "\n".join(lines[start:stop])
        metadata = _markdown_metadata(text)
        if metadata["policy_ref"] != entry.get("policy_ref"):
            raise ValueError("Insurance seed citation does not match its manifest.")
        if "source_version" in entry and metadata["version"] != entry["source_version"]:
            raise ValueError("Insurance seed version does not match its manifest.")
        seed_id = required_text(entry.get("id"), "seed ID")
        if not re.fullmatch(r"[a-z0-9-]+", seed_id):
            raise ValueError("Insurance seed ID must be a stable lowercase identifier.")
        source_path = path.relative_to(root.resolve()).as_posix()
        source_uri = "urn:northwind:synthetic:seed:" + seed_id
        result.append({
            **common, **metadata, "id": "seed-" + seed_id,
            "title": required_text(entry.get("title"), "seed title"),
            "document_id": seed_id, "section_id": section or "document",
            "doc_type": required_text(entry.get("doc_type"), "seed document type"),
            "source_uri": source_uri, "source_path": source_path + (("#" + section) if section else ""),
            "content": f"{DISCLAIMER}\nCitation: {metadata['policy_ref']} ({source_uri}).\n{text}",
        })
    if len({document["id"] for document in result}) != len(result):
        raise ValueError("Insurance knowledge contains duplicate document IDs.")
    if len(result) > 1000 or len(json.dumps(result).encode()) > 15 * 1024 * 1024:
        raise ValueError("Insurance corpus exceeds one bounded push-index batch; review before publishing.")
    return result


def index_body() -> dict:
    searchable = {"title", "content", "doc_type", "policy_ref", "state"}
    names = (
        "id", "title", "content", "source_uri", "source_path", "document_id",
        "section_id", "doc_type", "policy_ref", "version", "state", "states",
        "jurisdiction", "effective_from", "effective_to", "business_domain",
        "publisher", "synthetic", "fictional", "dataset_version",
    )
    fields = [{
        "name": name,
        "type": "Edm.Boolean" if name in ("synthetic", "fictional") else "Collection(Edm.String)" if name == "states" else "Edm.String",
        "key": name == "id", "searchable": name in searchable,
        "filterable": name not in ("title", "content", "source_uri", "source_path"),
        "retrievable": True,
    } for name in names]
    return {
        "name": INDEX_NAME, "fields": fields,
        "semantic": {"configurations": [{
            "name": "default",
            "prioritizedFields": {
                "titleField": {"fieldName": "title"},
                "prioritizedContentFields": [{"fieldName": "content"}],
                "prioritizedKeywordsFields": [{"fieldName": name} for name in ("doc_type", "state", "policy_ref")],
            },
        }]},
    }


def source_body() -> dict:
    return {
        "name": SOURCE_NAME, "kind": "searchIndex", "description": OWNER + ". " + DISCLAIMER,
        "searchIndexParameters": {
            "searchIndexName": INDEX_NAME, "semanticConfigurationName": "default",
            "sourceDataFields": [{"name": field["name"]} for field in index_body()["fields"]],
            "searchFields": [{"name": name} for name in ("title", "content", "policy_ref")],
        },
    }


def kb_body() -> dict:
    # Minimal/extractive retrieval needs no Foundry model credentials or new roles.
    return {
        "name": KB_NAME, "description": OWNER + ". " + DISCLAIMER,
        "knowledgeSources": [{"name": SOURCE_NAME}],
        "models": [], "outputMode": "extractiveData",
        "retrievalReasoningEffort": {"kind": "minimal"},
    }


def search_admin_key() -> str:
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI is required for an authorized admin-key lookup.")
    result = subprocess.run(
        [executable, "search", "admin-key", "show", "--subscription", SUBSCRIPTION,
         "--resource-group", DATA_GROUP, "--service-name", SEARCH_SERVICE,
         "--query", "primaryKey", "--output", "tsv", "--only-show-errors"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Search admin-key lookup failed (Azure CLI exit {result.returncode}); command output withheld.")
    key = result.stdout.strip()
    if not key or len(key.splitlines()) != 1:
        raise RuntimeError("Search admin-key lookup returned no single key.")
    return key


def project_token() -> str:
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity import AzureDeveloperCliCredential

    try:
        return AzureDeveloperCliCredential(tenant_id=TENANT).get_token("https://management.azure.com/.default").token
    except ClientAuthenticationError:
        raise RuntimeError("Signed-in azd user credentials are required for the specified tenant.") from None


def _request(client: httpx.Client, method: str, url: str, headers: dict, body: dict | None = None) -> httpx.Response:
    try:
        response = client.request(method, url, headers=headers, json=body)
    except httpx.HTTPError:
        raise RuntimeError("Insurance knowledge request transport failed; sensitive request data withheld.") from None
    if response.status_code not in (200, 201, 404):
        raise RuntimeError(f"Insurance knowledge {method} failed with HTTP {response.status_code}; response body withheld.")
    if response.status_code == 404 and method != "GET":
        raise RuntimeError("Insurance knowledge write failed with HTTP 404.")
    return response


def _preflight(client: httpx.Client, url: str, headers: dict, kind: str) -> dict | None:
    response = _request(client, "GET", url, headers)
    if response.status_code == 404:
        return {"If-None-Match": "*"}
    body = response.json()
    if kind == "index":
        actual = {field["name"]: (field["type"], bool(field.get("key"))) for field in body.get("fields", [])}
        expected = {field["name"]: (field["type"], field["key"]) for field in index_body()["fields"]}
        valid = actual == expected and body.get("name") == INDEX_NAME
    elif kind == "source":
        valid = body.get("kind") == "searchIndex" and body.get("searchIndexParameters", {}).get("searchIndexName") == INDEX_NAME
        valid = valid and body.get("description", "").startswith(OWNER)
    elif kind == "kb":
        valid = [source.get("name") for source in body.get("knowledgeSources", [])] == [SOURCE_NAME]
        valid = valid and body.get("description", "").startswith(OWNER)
    elif kind == "connection":
        properties = body.get("properties", {})
        valid = (
            properties.get("target") == f"{SEARCH_ENDPOINT}/knowledgebases/{KB_NAME}/mcp?api-version={SEARCH_API}"
            and properties.get("authType") == "CustomKeys"
            and properties.get("category") == "RemoteTool"
            and properties.get("metadata", {}).get("NorthwindInsurancePublisher") == OWNER
        )
        if valid:
            return None
    else:
        raise ValueError("Unsupported insurance preflight target.")
    if not valid:
        raise RuntimeError("Existing insurance-named target is incompatible or not publisher-owned; no writes performed.")
    if kind == "index":
        foreign = _request(
            client, "POST", f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/search?api-version={SEARCH_API}",
            headers, {
                "search": "*", "top": 1, "select": "id",
                "filter": f"publisher ne '{OWNER}' or business_domain ne 'insurance' or synthetic ne true or fictional ne true",
            },
        ).json().get("value", [])
        if foreign:
            raise RuntimeError("Insurance-named index contains unowned or nonsynthetic documents; no writes performed.")
    etag = response.headers.get("etag") or body.get("@odata.etag")
    if not etag:
        raise RuntimeError("Existing insurance target has no concurrency token; no writes performed.")
    return {"If-Match": etag}


def publish(*, apply: bool = False, connect_project: bool = False, root: Path = ROOT) -> dict:
    corpus = documents(root)
    summary = {
        "mode": "apply" if apply else "dry-run", "subscription": SUBSCRIPTION,
        "search_resource_group": DATA_GROUP, "search_service": SEARCH_SERVICE,
        "index": INDEX_NAME, "knowledge_source": SOURCE_NAME, "knowledge_base": KB_NAME,
        "documents": len(corpus),
        "policy_wording_sections": sum(document["doc_type"] == "policy_wording" for document in corpus),
        "seed_documents": sum(document["doc_type"] != "policy_wording" for document in corpus),
        "connection": CONNECTION_NAME if connect_project else None,
        "project": PROJECT_ID if connect_project else None,
    }
    if not apply:
        return summary
    key = search_admin_key()
    headers = {"api-key": key, "Content-Type": "application/json", "Prefer": "return=representation"}
    urls = {
        "index": f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}?api-version={SEARCH_API}",
        "source": f"{SEARCH_ENDPOINT}/knowledgesources/{SOURCE_NAME}?api-version={SEARCH_API}",
        "kb": f"{SEARCH_ENDPOINT}/knowledgebases/{KB_NAME}?api-version={SEARCH_API}",
    }
    connection_url = f"https://management.azure.com{PROJECT_ID}/connections/{CONNECTION_NAME}?api-version={ARM_API}"
    connection_headers = {"Authorization": "Bearer " + project_token(), "Content-Type": "application/json"} if connect_project else {}
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        conditions = {kind: _preflight(client, url, headers, kind) for kind, url in urls.items()}
        connection_condition = _preflight(client, connection_url, connection_headers, "connection") if connect_project else None
        _request(client, "PUT", urls["index"], {**headers, **conditions["index"]}, index_body())
        uploaded = _request(
            client, "POST", f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/index?api-version={SEARCH_API}",
            headers, {"value": [{"@search.action": "upload", **document} for document in corpus]},
        ).json().get("value", [])
        expected_ids = {document["id"] for document in corpus}
        if (
            len(uploaded) != len(corpus)
            or {item.get("key") for item in uploaded} != expected_ids
            or any(item.get("status") is not True or item.get("statusCode") not in (200, 201) for item in uploaded)
        ):
            raise RuntimeError("Insurance document upload was incomplete or failed; KB and connection writes stopped.")
        _request(client, "PUT", urls["source"], {**headers, **conditions["source"]}, source_body())
        _request(client, "PUT", urls["kb"], {**headers, **conditions["kb"]}, kb_body())
        if connect_project and connection_condition is not None:
            _request(client, "PUT", connection_url, {**connection_headers, **connection_condition}, {
                "name": CONNECTION_NAME,
                "type": "Microsoft.CognitiveServices/accounts/projects/connections",
                "properties": {
                    "authType": "CustomKeys", "category": "RemoteTool", "isSharedToAll": True,
                    "target": f"{SEARCH_ENDPOINT}/knowledgebases/{KB_NAME}/mcp?api-version={SEARCH_API}",
                    "credentials": {"keys": {"api-key": key}},
                    "metadata": {"ApiType": "Azure", "NorthwindInsurancePublisher": OWNER},
                },
            })
    summary["mode"] = "applied"
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Explicitly write only the named insurance targets.")
    parser.add_argument("--connect-project", action="store_true", help="Also create the insurance-specific MCP connection using signed-in azd credentials; its Azure CustomKeys credential holds the Search key, never a local file.")
    args = parser.parse_args()
    try:
        summary = publish(apply=args.apply, connect_project=args.connect_project)
    except (RuntimeError, ValueError, FileNotFoundError, subprocess.TimeoutExpired) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
