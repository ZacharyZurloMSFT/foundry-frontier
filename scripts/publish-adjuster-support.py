"""Preview isolated fictional employee/adjuster Search content; only --apply writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
RESOURCE_GROUP = "rg-northwind-data"
SERVICE = "srch-zurlo-northwind"
ENDPOINT = f"https://{SERVICE}.search.windows.net"
INDEX = "nw-insurance-adjuster-support"
OWNER = "northwind-adjuster-support-v1"
API = "2026-05-01-preview"


def documents(root: Path = ROOT) -> list[dict]:
    seed = root / "data" / "northwind" / "departments" / "employee-help-desk" / "seed"
    common = {"publisher": OWNER, "business_domain": "insurance", "synthetic": True}
    result = []
    for path in sorted(seed.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        text = path.read_text(encoding="utf-8").strip()
        title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("# ")), "")
        if not title or not text:
            raise ValueError("Employee source requires a title and content.")
        version = "snapshot-" + hashlib.sha256(text.encode()).hexdigest()[:16]
        result.append({
            **common, "id": path.stem, "title": "Fictional Northwind employee guidance: " + title,
            "version": version, "effective_from": "not specified in source",
            "source_uri": f"urn:northwind:synthetic:employee-support:{path.name}:{version}",
            "category": "it" if path.stem.startswith(("it-", "security-")) else "hr",
            "content": text,
        })
    corpus = json.loads((seed / "adjuster-procedures.json").read_text(encoding="utf-8"))
    if (corpus.get("synthetic") is not True or corpus.get("business_domain") != "insurance"
            or not re.fullmatch(r"\d+(?:\.\d+)*", corpus.get("version", ""))
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", corpus.get("effective_from", ""))
            or not corpus.get("documents")):
        raise ValueError("Adjuster sources require versioned synthetic insurance provenance.")
    for doc in corpus["documents"]:
        if (not re.fullmatch(r"ADJ-[A-Z]+-\d+", doc.get("id", ""))
                or "fictional" not in doc.get("title", "").lower() or not doc.get("content", "").strip()):
            raise ValueError("Invalid fictional adjuster source.")
        result.append({
            **common, **doc, "version": corpus["version"], "effective_from": corpus["effective_from"],
            "source_uri": f"urn:northwind:synthetic:employee-support:{doc['id']}:v{corpus['version']}",
            "category": "adjuster",
        })
    if (not result or len({doc["id"] for doc in result}) != len(result)
            or any(len(doc["content"]) > 20000 for doc in result)):
        raise ValueError("Support corpus must have unique bounded documents.")
    return result


def index_body():
    fields = [{"name": "id", "type": "Edm.String", "key": True, "filterable": True}]
    for name in ("title", "content", "source_uri", "version", "category", "effective_from",
                 "publisher", "business_domain"):
        fields.append({"name": name, "type": "Edm.String",
                       "searchable": name in ("title", "content"),
                       "filterable": name in ("publisher", "business_domain", "category")})
    fields.append({"name": "synthetic", "type": "Edm.Boolean", "filterable": True})
    return {"name": INDEX, "description": OWNER, "fields": fields}


def _request(client, method, path, headers, body=None):
    response = client.request(method, ENDPOINT + path + "?api-version=" + API, headers=headers, json=body)
    if response.status_code not in (200, 201, 204):
        # Never expose an SDK/request repr, response body, key or token.
        raise RuntimeError(f"Adjuster Search {method} {path} failed (HTTP {response.status_code}); no secret details logged.")
    return response


def publish(*, apply=False, root=ROOT):
    corpus = documents(root)
    summary = {
        "mode": "apply" if apply else "preview", "subscription": SUBSCRIPTION,
        "resource_group": RESOURCE_GROUP, "search_service": SERVICE, "index": INDEX,
        "documents": len(corpus), "categories": sorted({d["category"] for d in corpus}),
        "content_sha256": hashlib.sha256(json.dumps(corpus, sort_keys=True).encode()).hexdigest(),
    }
    if not apply:
        return summary
    credential = None
    headers = {"Content-Type": "application/json"}
    auth = os.environ.get("ADJUSTER_SUPPORT_PUBLISH_AUTH", "entra_id")
    if auth == "admin_key":
        key = os.environ.get("ADJUSTER_SUPPORT_SEARCH_ADMIN_KEY", "")
        if not key:
            raise ValueError("Explicit publisher admin key is missing; no automatic key lookup or fallback.")
        headers["api-key"] = key
    elif auth == "entra_id":
        from azure.identity import AzureDeveloperCliCredential
        credential = AzureDeveloperCliCredential(tenant_id=TENANT)
        headers["Authorization"] = "Bearer " + credential.get_token("https://search.azure.com/.default").token
    else:
        raise ValueError("Publisher auth must explicitly be entra_id or admin_key.")
    try:
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            path = f"/indexes/{INDEX}"
            response = client.get(ENDPOINT + path + "?api-version=" + API, headers=headers)
            if response.status_code == 404:
                _request(client, "PUT", path, {**headers, "If-None-Match": "*"}, index_body())
            elif response.status_code == 200:
                existing = response.json()
                fields = {field["name"]: field for field in existing.get("fields", [])}
                if (existing.get("name") != INDEX or existing.get("description") != OWNER
                        or set(fields) != {f["name"] for f in index_body()["fields"]}
                        or any(any(fields.get(f["name"], {}).get(k, False) != value for k, value in f.items())
                               for f in index_body()["fields"])):
                    raise RuntimeError("Existing support index is not compatible and publisher-owned; no writes performed.")
                foreign = _request(client, "POST", path + "/docs/search", headers, {
                    "search": "*", "top": 1, "select": "id",
                    "filter": f"publisher ne '{OWNER}' or business_domain ne 'insurance' or synthetic ne true",
                }).json().get("value", [])
                if foreign:
                    raise RuntimeError("Existing support index contains foreign documents; no writes performed.")
            else:
                raise RuntimeError(f"Support index ownership preflight GET failed (HTTP {response.status_code}); "
                                   "no writes performed.")
            uploaded = _request(client, "POST", path + "/docs/index", headers,
                                {"value": [{"@search.action": "upload", **d} for d in corpus]}).json().get("value", [])
            if (len(uploaded) != len(corpus) or {d.get("key") for d in uploaded} != {d["id"] for d in corpus}
                    or any(d.get("status") is not True or d.get("statusCode") not in (200, 201) for d in uploaded)):
                raise RuntimeError("Support upload was incomplete; do not enable retrieval until verified.")
    finally:
        if credential:
            credential.close()
    summary["mode"] = "applied"
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write only the isolated publisher-owned support index.")
    args = parser.parse_args()
    try:
        print(json.dumps(publish(apply=args.apply), sort_keys=True))
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        print(f"ERROR [{type(error).__name__}]: {error}", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as error:
        print(f"ERROR [{type(error).__name__}]: Search request failed (HTTP {error.response.status_code}); "
              "remote response and request details omitted.", file=sys.stderr)
        return 1
    except httpx.RequestError as error:
        print(f"ERROR [{type(error).__name__}]: Search transport failed; check endpoint connectivity. "
              "Request details omitted.", file=sys.stderr)
        return 1
    except Exception as error:
        status = getattr(error, "status_code", None)
        if status is None:
            status = getattr(getattr(error, "response", None), "status_code", None)
        context = f" (HTTP {status})" if type(status) is int and 100 <= status <= 599 else ""
        print(f"ERROR [{type(error).__name__}]{context}: Adjuster support publishing failed; "
              "sensitive exception details omitted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
