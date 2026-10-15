"""Import existing Azure datasets into fine-tuning files, without local uploads."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential, get_bearer_token_provider
from azure.storage.blob import ContainerClient

ACCOUNT = "aisvc-zurlo-northwind"
PROJECT = "nw-claims-warranty"
DATASETS = ("01-triage-training", "02-triage-validation")


def records(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode("utf-8-sig").splitlines() if line.strip()]


def semantic_hash(rows: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def blob_url(container_sas: str, name: str) -> str:
    parsed = urlsplit(container_sas)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".blob.core.windows.net")
            or not parsed.query or parsed.username or parsed.password):
        raise ValueError("Expected a credentialed Azure Blob URL for the existing uploaded dataset")
    return urlunsplit((parsed.scheme, parsed.netloc,
                       parsed.path.rstrip("/") + "/" + quote(name, safe="/"), parsed.query, ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--local-pack", required=True, type=Path, help="Read-only source for integrity comparison")
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    credential = AzureCliCredential(tenant_id=args.tenant, process_timeout=120)
    token = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
    receipt = {"schema_version": 1, "account": ACCOUNT, "project": PROJECT,
               "source": "existing user-uploaded Azure datasets",
               "local_file_uploaded": False, "training_job_started": False,
               "files": [], "state": "dry_run"}
    if args.receipt.exists():
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        if receipt.get("account") != ACCOUNT or receipt.get("project") != PROJECT:
            raise ValueError("Receipt belongs to another training location")
        if any(item.get("state") == "import_started" and not item.get("id") for item in receipt["files"]):
            raise ValueError("Prior import outcome is uncertain; reconcile fine-tuning files before retrying")
    args.receipt.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    try:
        with AIProjectClient(
            endpoint=f"https://{ACCOUNT}.services.ai.azure.com/api/projects/{PROJECT}",
            credential=credential,
        ) as project, httpx.Client(timeout=120) as http:
            for name in DATASETS:
                existing = next((item for item in receipt["files"] if item["dataset"] == name), None)
                if existing and existing.get("id"):
                    continue
                dataset = project.datasets.get(name, "1")
                reference = project.datasets.get_credentials(name, "1").as_dict()["blobReferenceForConsumption"]
                sas = reference["credential"]["sasUri"]
                with ContainerClient.from_container_url(sas) as container:
                    candidates = [blob for blob in container.list_blobs() if blob.name.endswith(".jsonl")]
                    if len(candidates) != 1:
                        raise ValueError(f"Expected one JSONL file in {name}")
                    remote_bytes = container.download_blob(candidates[0].name).readall()
                expected = records((args.local_pack / (name + ".jsonl")).read_bytes())
                actual = records(remote_bytes)
                if actual != expected:
                    raise ValueError(f"Uploaded records differ from the supplied pack: {name}")
                item = {"dataset": name, "dataset_id": dataset.id, "version": "1",
                        "records": len(actual), "semantic_sha256": semantic_hash(actual),
                        "state": "verified", "id": None}
                receipt["files"].append(item)
                if args.apply:
                    item["state"] = "import_started"
                    receipt["state"] = "importing"
                    save()
                    # The service reads the existing Azure blob. SAS stays in memory.
                    response = http.post(
                        f"https://{ACCOUNT}.openai.azure.com/openai/files/import",
                        params={"api-version": "2024-10-21"},
                        headers={"Authorization": f"Bearer {token()}"},
                        json={"purpose": "fine-tune", "filename": name + ".jsonl",
                              "content_url": blob_url(sas, candidates[0].name)},
                    )
                    if response.status_code >= 400:
                        item["state"] = "import_rejected"
                        receipt["state"] = "blocked"
                        item["http_status"] = response.status_code
                        save()
                        raise RuntimeError(f"Native server import rejected for {name}: HTTP {response.status_code}")
                    result = response.json()
                    if not result.get("id"):
                        raise RuntimeError("Import returned no file ID; reconcile before retrying")
                    item.update(id=result["id"], state=result.get("status", "pending"))
                    save()
                print(json.dumps({"dataset": name, "rows": len(actual), "state": item["state"],
                                  "file_id": item["id"]}), flush=True)
            receipt["state"] = "imported" if args.apply else "dry_run"
            save()
            print(json.dumps(receipt, indent=2))
    finally:
        credential.close()


if __name__ == "__main__":
    main()
