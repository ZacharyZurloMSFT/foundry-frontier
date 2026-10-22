"""Preview isolated fictional customer Help Center Search content; only --apply writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
RESOURCE_GROUP = "rg-northwind-data"
SERVICE = "srch-zurlo-northwind"
ENDPOINT = f"https://{SERVICE}.search.windows.net"
INDEX = "nw-insurance-help-customer-v1"
OWNER = "northwind-help-center-customer-v1"
API = "2026-05-01-preview"
SEED = Path("data") / "northwind" / "departments" / "employee-help-desk" / "seed" / "customer"
TOPICS = frozenset({"claims", "evidence", "coverage", "rental", "billing", "privacy", "contact", "glossary",
                    "assistants"})
EXPECTED_IDS = frozenset({
    "HELP-CLAIMS-001", "HELP-CLAIMS-002", "HELP-EVIDENCE-001", "HELP-EVIDENCE-002",
    "HELP-COVERAGE-001", "HELP-COVERAGE-002", "HELP-RENTAL-001", "HELP-BILLING-001",
    "HELP-PRIVACY-001", "HELP-CONTACT-001", "HELP-GLOSSARY-001", "HELP-ASSIST-001",
})
FRONT_MATTER_KEYS = frozenset({"id", "topic", "version", "effective_from"})
HEADING_PREFIX = "# Fictional Northwind help: "
MAX_CONTENT = 12000
# The Help Center runtime rejects longer titles or versions, so a corpus that exceeds them is never published.
MAX_TITLE = 80
MAX_VERSION = 32
FORBIDDEN_PHRASES = ("coverage is approved", "claim is approved")
HTML = re.compile(r"<(?:[A-Za-z]|/[A-Za-z]|!|\?)")
LINK = re.compile(r"!?\[[^\]\n]*\]\s*[(\[]|^\s*\[[^\]\n]+\]:|\b[a-z][a-z0-9+.-]*://|\bmailto:|\bwww\.",
                  re.IGNORECASE | re.MULTILINE)


def _iso_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _front_matter(name: str, text: str) -> tuple[dict[str, str], str]:
    """Parse simple `key: value` lines between `---` fences without a YAML dependency."""
    lines = text.strip().split("\n")
    if lines[0].strip() != "---":
        raise ValueError(f"{name}: front matter must start with a --- line.")
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise ValueError(f"{name}: front matter must end with a --- line.")
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[a-z_]+", key) or key in meta:
            raise ValueError(f"{name}: front matter must contain unique 'key: value' lines.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key] = value
    if set(meta) != FRONT_MATTER_KEYS:
        raise ValueError(f"{name}: front matter must define exactly id, topic, version and effective_from.")
    return meta, "\n".join(lines[end + 1:]).strip()


def _article(path: Path) -> dict:
    name = path.name
    meta, content = _front_matter(name, path.read_text(encoding="utf-8-sig"))
    if not re.fullmatch(r"HELP-[A-Z]+-\d{3}", meta["id"]):
        raise ValueError(f"{name}: id must match HELP-TOPIC-000.")
    if meta["topic"] not in TOPICS:
        raise ValueError(f"{name}: topic must be one of the allowed help topics.")
    if not re.fullmatch(r"\d+(?:\.\d+)*", meta["version"]) or len(meta["version"]) > MAX_VERSION:
        raise ValueError(f"{name}: version must be a dotted number of at most {MAX_VERSION} characters, such as 1.2.")
    if not _iso_date(meta["effective_from"]):
        raise ValueError(f"{name}: effective_from must be an ISO date (YYYY-MM-DD).")
    heading, _, body = content.partition("\n")
    if not heading.startswith(HEADING_PREFIX) or not heading[len(HEADING_PREFIX):].strip():
        raise ValueError(f"{name}: the first line must be an H1 starting with '{HEADING_PREFIX}'.")
    title = heading[2:].strip()
    if len(title) > MAX_TITLE:
        raise ValueError(f"{name}: the title must be at most {MAX_TITLE} characters.")
    if re.search(r"^# ", body, re.MULTILINE):
        raise ValueError(f"{name}: only one H1 is allowed.")
    if not body.strip():
        raise ValueError(f"{name}: the article body is empty.")
    if len(content) >= MAX_CONTENT:
        raise ValueError(f"{name}: the article must be under {MAX_CONTENT} characters.")
    if HTML.search(content) or LINK.search(content):
        raise ValueError(f"{name}: HTML, links and images are not allowed.")
    if any(phrase in content.casefold() for phrase in FORBIDDEN_PHRASES):
        raise ValueError(f"{name}: the article must not assert a coverage or claim decision.")
    return {
        "id": meta["id"], "title": title, "content": content,
        "source_uri": f"urn:northwind:synthetic:help-center:{meta['id']}:v{meta['version']}",
        "version": meta["version"], "topic": meta["topic"], "effective_from": meta["effective_from"],
        "audience": "customer", "publisher": OWNER, "business_domain": "insurance", "synthetic": True,
    }


def documents(root: Path = ROOT) -> list[dict]:
    seed = root / SEED
    if not seed.is_dir():
        raise ValueError("The customer help article directory is missing.")
    result = [_article(path) for path in sorted(seed.glob("*.md")) if path.name.lower() != "readme.md"]
    ids = [doc["id"] for doc in result]
    if len(set(ids)) != len(ids):
        raise ValueError("Customer help article ids must be unique.")
    if set(ids) != EXPECTED_IDS:
        raise ValueError(f"Customer help must contain exactly the {len(EXPECTED_IDS)} expected articles; "
                         f"missing {sorted(EXPECTED_IDS - set(ids))}, unexpected {sorted(set(ids) - EXPECTED_IDS)}.")
    return result


def index_body():
    fields = [{"name": "id", "type": "Edm.String", "key": True, "filterable": True}]
    for name in ("title", "content", "source_uri", "version", "topic", "effective_from", "audience",
                 "publisher", "business_domain"):
        fields.append({"name": name, "type": "Edm.String",
                       "searchable": name in ("title", "content"),
                       "filterable": name in ("publisher", "business_domain", "audience", "topic")})
    fields.append({"name": "synthetic", "type": "Edm.Boolean", "filterable": True})
    return {"name": INDEX, "description": OWNER, "fields": fields}


def _compatible(existing) -> bool:
    fields = existing.get("fields") if isinstance(existing, dict) else None
    if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
            or existing.get("name") != INDEX or existing.get("description") != OWNER):
        return False
    actual = {field.get("name"): field for field in fields}
    expected = index_body()["fields"]
    return (len(actual) == len(fields) and set(actual) == {field["name"] for field in expected}
            and all(actual[field["name"]].get(key, False) == value for field in expected
                    for key, value in field.items()))


def _upload_complete(results, corpus) -> bool:
    return (isinstance(results, list) and len(results) == len(corpus)
            and all(isinstance(result, dict) and result.get("status") is True
                    and result.get("statusCode") in (200, 201) for result in results)
            and {result.get("key") for result in results} == {doc["id"] for doc in corpus})


def _request(client, method, path, headers, body=None):
    response = client.request(method, ENDPOINT + path + "?api-version=" + API, headers=headers, json=body)
    if response.status_code not in (200, 201, 204):
        # Never expose an SDK/request repr, response body, key or token.
        raise RuntimeError(f"Help Center Search {method} {path} failed (HTTP {response.status_code}); "
                           "no secret details logged.")
    return response


def publish(*, apply=False, root=ROOT):
    corpus = documents(root)
    summary = {
        "mode": "apply" if apply else "preview", "subscription": SUBSCRIPTION,
        "resource_group": RESOURCE_GROUP, "search_service": SERVICE, "index": INDEX,
        "documents": len(corpus), "topics": sorted({d["topic"] for d in corpus}),
        "audiences": sorted({d["audience"] for d in corpus}),
        "content_sha256": hashlib.sha256(json.dumps(corpus, sort_keys=True).encode()).hexdigest(),
    }
    if not apply:
        return summary
    credential = None
    headers = {"Content-Type": "application/json"}
    try:
        auth = os.environ.get("HELP_CENTER_PUBLISH_AUTH", "entra_id")
        if auth == "admin_key":
            key = os.environ.get("HELP_CENTER_SEARCH_ADMIN_KEY", "")
            if not key:
                raise ValueError("Explicit publisher admin key is missing; no automatic key lookup or fallback.")
            headers["api-key"] = key
        elif auth == "entra_id":
            try:
                from azure.identity import AzureDeveloperCliCredential
            except ImportError as error:
                raise RuntimeError("Entra authentication requires the azure-identity package.") from error
            credential = AzureDeveloperCliCredential(tenant_id=TENANT)
            headers["Authorization"] = "Bearer " + credential.get_token("https://search.azure.com/.default").token
        else:
            raise ValueError("Publisher auth must explicitly be entra_id or admin_key.")
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            path = f"/indexes/{INDEX}"
            response = client.get(ENDPOINT + path + "?api-version=" + API, headers=headers)
            if response.status_code == 404:
                _request(client, "PUT", path, {**headers, "If-None-Match": "*"}, index_body())
            elif response.status_code == 200:
                if not _compatible(response.json()):
                    raise RuntimeError("Existing help index is not compatible and publisher-owned; "
                                       "no writes performed.")
                foreign = _request(client, "POST", path + "/docs/search", headers, {
                    "search": "*", "top": 1, "select": "id",
                    "filter": (f"publisher ne '{OWNER}' or business_domain ne 'insurance' "
                               "or synthetic ne true or audience ne 'customer'"),
                }).json().get("value", [])
                if foreign:
                    raise RuntimeError("Existing help index contains foreign documents; no writes performed.")
            else:
                raise RuntimeError(f"Help index ownership preflight GET failed (HTTP {response.status_code}); "
                                   "no writes performed.")
            uploaded = _request(client, "POST", path + "/docs/index", headers,
                                {"value": [{"@search.action": "upload", **d} for d in corpus]}).json().get("value")
            if not _upload_complete(uploaded, corpus):
                raise RuntimeError("Help upload was incomplete; do not enable retrieval until verified.")
    finally:
        if credential:
            credential.close()
    summary["mode"] = "applied"
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write only the isolated publisher-owned customer help index.")
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
        print(f"ERROR [{type(error).__name__}]{context}: Customer help publishing failed; "
              "sensitive exception details omitted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
