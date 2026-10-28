"""Stage validated canonical insurance assets inside one Docker build context."""

import argparse
import ast
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("api", "mcp-policy-lookup", "mcp-claim-review")
CLAIMS_DOCUMENTS = {
    "nw-fnol-demo-2001.pdf", "nw-estimate-demo-2001.pdf",
    "nw-record-clm-2001.pdf", "nw-record-clm-2002.pdf",
}


def bundle(service: str, root: Path = ROOT) -> dict[str, str]:
    if service not in SERVICES:
        raise ValueError("Insurance assets can only be staged for the API and MCP services.")
    fixture = root / "data" / "northwind" / "insurance-fixtures.json"
    domain = root / "apps" / "api" / "app" / "insurance_domain.py"
    guard = root / "apps" / "api" / "app" / "gateway_guard.py"
    records = json.loads(fixture.read_text(encoding="utf-8"))
    if (
        not isinstance(records, dict)
        or records.get("synthetic") is not True
        or records.get("business_domain") != "insurance"
    ):
        raise ValueError("Only explicitly synthetic insurance fixtures may be packaged.")
    for collection in ("policyholders", "policies", "claims", "policy_wordings"):
        if not isinstance(records.get(collection), list) or not records[collection]:
            raise ValueError("Canonical insurance fixtures require nonempty " + collection + ".")
    ast.parse(domain.read_text(encoding="utf-8"), filename=str(domain))
    ast.parse(guard.read_text(encoding="utf-8"), filename=str(guard))
    binding = root / "apps" / "api" / "app" / "workflow_binding.py" if service == "mcp-claim-review" else None
    if binding is not None:
        ast.parse(binding.read_text(encoding="utf-8"), filename=str(binding))
    sources = []
    if service == "api":
        manifest = root / "data" / "northwind" / "departments" / "claims-documents" / "claims-documents.json"
        catalog = json.loads(manifest.read_text(encoding="utf-8"))["items"]
        names = [row["source_file"] for row in catalog]
        if len(names) != len(CLAIMS_DOCUMENTS) or set(names) != CLAIMS_DOCUMENTS:
            raise ValueError("CU source catalog must contain only the four reviewed synthetic PDF inputs.")
        for row in catalog:
            if row.get("synthetic") is not True or row.get("media_type") != "application/pdf":
                raise ValueError("CU bundled inputs must be explicitly synthetic PDF documents.")
        for name in names:
            if not re.fullmatch(r"[A-Za-z0-9._-]+\.pdf", name):
                raise ValueError("CU source filenames must not contain a path.")
            source = root / "data" / "seed" / "generated" / name
            data = source.read_bytes()
            if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:] or len(data) > 10485760:
                raise ValueError("CU source must be a bounded valid synthetic PDF.")
            sources.append(source)

    destination = root / "apps" / service / "app"
    fixture_destination = destination / "_northwind" if service == "api" else destination
    fixture_destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, fixture_destination / fixture.name)
    if service != "api":
        shutil.copyfile(domain, destination / domain.name)
        shutil.copyfile(guard, destination / guard.name)
        if binding is not None:
            shutil.copyfile(binding, destination / binding.name)
    else:
        document_destination = destination / "_northwind" / "claims-documents"
        document_destination.mkdir(parents=True, exist_ok=True)
        catalog_destination = destination / "_northwind" / "departments" / "claims-documents"
        catalog_destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest, catalog_destination / manifest.name)
        for source in sources:
            shutil.copyfile(source, document_destination / source.name)
    return {
        "service": service,
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "domain_sha256": hashlib.sha256(domain.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, choices=SERVICES)
    print(json.dumps(bundle(parser.parse_args().service)))
