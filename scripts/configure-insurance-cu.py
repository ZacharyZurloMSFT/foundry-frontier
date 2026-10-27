"""Offline preview; --apply configures one owned CU analyzer.

CU model-role defaults require separate --configure-defaults consent.
No resource/model creation, Foundry defaultProject change, worker restart or networking.
Analysis uses request-scoped modelDeployments; registration can require CU defaults.
The base analyzer's inherited embedding role is retained, not relabelled as another model.
Data-plane authentication defaults to tenant-pinned azd, with no fallback.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureDeveloperCliCredential

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
GROUP = "rg-northwind-ai"
ACCOUNT = "aisvc-zurlo-northwind"
ANALYZER_ID = "northwindInsuranceClaimsV1"
OWNER = "northwind-insurance-cu-v1"
API_VERSION = "2025-11-01"
SCOPE = "https://cognitiveservices.azure.com/.default"
ACCOUNT_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/Microsoft.CognitiveServices/accounts/{ACCOUNT}"
DEFINITION = ROOT / "data" / "northwind" / "departments" / "claims-documents" / "cu-analyzers" / "claim-intake.json"
DOCS = "https://learn.microsoft.com/rest/api/contentunderstanding/content-analyzers/create-or-replace?view=rest-contentunderstanding-2025-11-01"


def definition(model: str = "gpt-4.1") -> dict:
    if model not in ("gpt-4.1", "gpt-4o"):
        raise ValueError("Select the published supported gpt-4.1 or gpt-4o completion model.")
    body = json.loads(DEFINITION.read_text(encoding="utf-8"))
    if body.get("tags", {}).get("owner") != OWNER or body.get("fieldSchema", {}).get("name") != "NorthwindInsuranceClaimsV1":
        raise ValueError("Unexpected local analyzer ownership or schema.")
    body["models"] = {"completion": model}
    return body


def az_json(*args: str):
    executable = shutil.which("az")
    if not executable:
        raise ValueError("Azure CLI is required only for --apply.")
    completed = subprocess.run(
        [executable, *args, "--subscription", SUBSCRIPTION, "--output", "json", "--only-show-errors"],
        capture_output=True, text=True, timeout=60,
    )
    if completed.returncode:
        raise ValueError("Azure CLI read/auth preflight failed; no credentials or CLI error content are displayed.")
    try:
        return json.loads(completed.stdout)
    except ValueError:
        raise ValueError("Azure CLI returned invalid JSON.") from None


def data_plane_token(credential_source: str = "azd") -> str:
    if credential_source == "cli":
        token = az_json("account", "get-access-token", "--resource", "https://cognitiveservices.azure.com/").get("accessToken")
    elif credential_source == "azd":
        try:
            with AzureDeveloperCliCredential(tenant_id=TENANT, process_timeout=30) as credential:
                token = credential.get_token(SCOPE).token
        except ClientAuthenticationError:
            raise ValueError("Selected azd credential could not authenticate in the allowlisted tenant; no CLI fallback was attempted.") from None
    else:
        raise ValueError("Select exactly one credential source: azd or cli.")
    if not isinstance(token, str) or not token:
        raise ValueError(f"Selected {credential_source} credential returned no Cognitive Services access token; no fallback was attempted.")
    return token


def endpoint_from_account(account: dict) -> str:
    if str(account.get("id", "")).lower() != ACCOUNT_ID.lower() or account.get("kind") != "AIServices":
        raise ValueError("Refusing any resource except the existing allowlisted Foundry AIServices account.")
    endpoint = account.get("properties", {}).get("endpoint", "")
    parts = urlsplit(endpoint)
    if (
        parts.scheme != "https" or parts.hostname not in (
            f"{ACCOUNT}.cognitiveservices.azure.com", f"{ACCOUNT}.services.ai.azure.com",
        ) or parts.port not in (None, 443) or parts.path not in ("", "/")
        or parts.username or parts.password or parts.query or parts.fragment
    ):
        raise ValueError("Unexpected resource endpoint; no request was sent.")
    return endpoint.rstrip("/")


def deployment_model(deployments: list[dict], name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name):
        raise ValueError("Supply a plain existing deployment name.")
    match = next((d for d in deployments if d.get("name") == name), None)
    properties = match.get("properties", {}) if match else {}
    model = properties.get("model", {}).get("name")
    if not isinstance(model, str) or not model or properties.get("provisioningState") != "Succeeded":
        raise ValueError("The selected existing deployment is missing model metadata or is not provisioned.")
    return model


def existing_deployment(deployments: list[dict], name: str, model: str) -> None:
    if deployment_model(deployments, name) != model:
        raise ValueError("The selected existing deployment does not match the requested supported model or is not provisioned.")


def check_existing(current: dict | None, desired: dict, reconcile: bool) -> bool:
    if current is None:
        return True
    tags, schema = current.get("tags"), current.get("fieldSchema")
    if (
        not isinstance(tags, dict) or not isinstance(schema, dict)
        or tags.get("owner") != OWNER or tags.get("schema") != "insurance-claims-v1"
        or schema.get("name") != desired["fieldSchema"]["name"]
    ):
        raise ValueError("Refusing to replace an analyzer with different ownership/schema.")
    old_fields = schema.get("fields", {})
    new_fields = desired["fieldSchema"]["fields"]
    def compatible(old, new):
        if not isinstance(old, dict) or not isinstance(new, dict) or old.get("type") != new.get("type"):
            return False
        if old.get("type") == "object":
            props = old.get("properties", {})
            return isinstance(props, dict) and all(key in new.get("properties", {}) and compatible(value, new["properties"][key]) for key, value in props.items())
        if old.get("type") == "array":
            return compatible(old.get("items"), new.get("items"))
        return True
    if not isinstance(old_fields, dict) or not old_fields or any(
        key not in new_fields or not compatible(value, new_fields[key]) for key, value in old_fields.items()
    ):
        raise ValueError("Existing schema contains foreign fields or incompatible types; use a separately reviewed version.")
    if str(current.get("status", "")).lower() != "ready":
        raise ValueError("The existing owned analyzer is not ready; investigate its existing operation before retrying.")
    def contains(actual, expected):
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(key in actual and contains(actual[key], value) for key, value in expected.items())
        return actual == expected
    if contains(current, desired):
        return False
    if not reconcile:
        raise ValueError("Owned analyzer differs. Review the preview, then explicitly use --reconcile with --apply.")
    return True


def request(client: httpx.Client, method: str, url: str, token: str, *, allow_missing=False, content_type=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    response = client.request(method, url, headers=headers, **kwargs)
    if response.status_code == 404 and allow_missing:
        return None, response.headers
    if not response.is_success:
        raise ValueError(f"CU {method} failed (HTTP {response.status_code}); response content suppressed.")
    try:
        body = response.json()
    except ValueError:
        raise ValueError("CU returned invalid JSON.") from None
    if not isinstance(body, dict):
        raise ValueError("CU returned an invalid response shape.")
    return body, response.headers


def ensure_cu_defaults(client: httpx.Client, endpoint: str, token: str, required: dict[str, str], *, configure: bool = False) -> bool:
    url = f"{endpoint}/contentunderstanding/defaults?api-version={API_VERSION}"
    def mappings(body):
        mapping = body.get("modelDeployments")
        if not isinstance(mapping, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in mapping.items()):
            raise ValueError("CU defaults returned an invalid model mapping; no replacement was attempted.")
        return mapping
    current, _ = request(client, "GET", url, token)
    existing = mappings(current)
    missing = {}
    for model, deployment in required.items():
        if model in existing and existing[model] != deployment:
            raise ValueError("CU defaults already map the selected model to a different deployment; refusing to overwrite it.")
        if model not in existing:
            missing[model] = deployment
    if not missing:
        return False
    if not configure:
        raise ValueError("The selected CU model-role defaults mapping is missing. Registration requires explicit --configure-defaults; nothing was written.")
    latest, _ = request(client, "GET", url, token)
    if mappings(latest) != existing:
        raise ValueError("CU defaults changed during preflight; no defaults write was attempted.")
    # Merge only missing validated model names/inherited role aliases, never other keys.
    request(client, "PATCH", url, token, json={"modelDeployments": missing},
            content_type="application/merge-patch+json")
    verified, _ = request(client, "GET", url, token)
    actual = mappings(verified)
    if any(actual.get(key) != value for key, value in required.items()) or any(actual.get(key) != value for key, value in existing.items()):
        raise ValueError("CU defaults PATCH was submitted, but the selected mapping/preservation of existing mappings was not verified. Inspect CU defaults before retrying.")
    return True


def apply(model: str, deployment: str, *, reconcile: bool = False, timeout: float = 120, credential_source: str = "azd", configure_defaults: bool = False, embedding_deployment: str = "") -> dict:
    desired = definition(model)
    context = az_json("account", "show")
    if context.get("id") != SUBSCRIPTION or context.get("tenantId") != TENANT:
        raise ValueError("Subscription/tenant does not match the allowlisted insurance environment.")
    account = az_json("cognitiveservices", "account", "show", "--name", ACCOUNT, "--resource-group", GROUP)
    endpoint = endpoint_from_account(account)
    deployments = az_json("cognitiveservices", "account", "deployment", "list", "--name", ACCOUNT, "--resource-group", GROUP)
    existing_deployment(deployments, deployment, model)
    token = data_plane_token(credential_source)
    url = f"{endpoint}/contentunderstanding/analyzers/{ANALYZER_ID}?api-version={API_VERSION}"
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        prebuilt, _ = request(client, "GET", f"{endpoint}/contentunderstanding/analyzers/prebuilt-document?api-version={API_VERSION}", token)
        if model not in prebuilt.get("supportedModels", {}).get("completion", []):
            raise ValueError("The live prebuilt-document analyzer does not advertise this completion model; nothing was written.")
        base_models = prebuilt.get("models")
        if not isinstance(base_models, dict):
            raise ValueError("The live base analyzer did not return model roles; no defaults or analyzer write was attempted.")
        embedding_role = base_models.get("embedding")
        required_mappings = {model: deployment}
        embedding_info = None
        if embedding_role:
            if not embedding_deployment:
                raise ValueError("The live base analyzer requires an inherited embedding role. Supply --embedding-deployment naming an existing advertised embedding deployment; nothing was written.")
            embedding_model = deployment_model(deployments, embedding_deployment)
            if embedding_model not in prebuilt.get("supportedModels", {}).get("embedding", []):
                raise ValueError("The actual embedding deployment model is not advertised by this live base analyzer; no defaults or analyzer write was attempted.")
            if embedding_role not in ("prebuilt-analyzer-embedding", embedding_model):
                raise ValueError("The live base analyzer returned an unexpected embedding role; refusing to guess a model mapping.")
            required_mappings[embedding_role] = embedding_deployment
            embedding_info = {"role": embedding_role, "model": embedding_model, "deployment": embedding_deployment}
        elif embedding_deployment:
            raise ValueError("The base analyzer did not declare an embedding role; refusing to guess where to map the supplied deployment.")
        current, _ = request(client, "GET", url, token, allow_missing=True)
        analyzer_change = check_existing(current, desired, reconcile)
        defaults_updated = ensure_cu_defaults(client, endpoint, token, required_mappings, configure=configure_defaults)
        if not analyzer_change:
            return {
                "status": "analyzer_ready" if defaults_updated else "unchanged", "analyzer_id": ANALYZER_ID,
                "credential_source": credential_source, "smoke_required": True,
                "analyzer_changed": False, "cu_defaults_updated": defaults_updated,
                "embedding": embedding_info,
            }
        # Re-read immediately before replacement; never reconcile a concurrently changed definition.
        latest, _ = request(client, "GET", url, token, allow_missing=True)
        if latest != current:
            raise ValueError("Analyzer changed during preflight; no analyzer write was attempted. Any opt-in CU defaults update may already have completed.")
        _, headers = request(
            client, "PUT", url + f"&allowReplace={'true' if current else 'false'}",
            token, json=desired,
        )
        location = headers.get("Operation-Location")
        if location:
            parts, origin = urlsplit(location), urlsplit(endpoint)
            if (
                parts.scheme != origin.scheme or parts.netloc != origin.netloc
                or parts.username or parts.password or parts.fragment
                or not re.fullmatch(f"/contentunderstanding/analyzers/{ANALYZER_ID}/operations/[A-Za-z0-9_-]+", parts.path)
                or parse_qs(parts.query) != {"api-version": [API_VERSION]}
            ):
                raise ValueError("Analyzer submitted, but unsafe operation location refused. Inspect the exact owned analyzer before retrying.")
            deadline = time.monotonic() + timeout
            for _ in range(120):
                if time.monotonic() >= deadline:
                    raise ValueError("Analyzer creation timed out; it may still be running. No retry/replacement was attempted.")
                operation, _ = request(client, "GET", location, token)
                status = str(operation.get("status", "")).lower()
                if status == "succeeded":
                    break
                if status not in ("running", "notstarted", "pending"):
                    raise ValueError("Analyzer creation failed or returned an unknown state. Inspect its configuration and any opt-in CU defaults update; no model deployment, Foundry defaultProject or network change was attempted.")
                time.sleep(1)
            else:
                raise ValueError("Analyzer creation polling limit reached.")
        actual, _ = request(client, "GET", url, token)
        if check_existing(actual, desired, False):
            raise ValueError("Created analyzer did not match the requested schema.")
    return {
        "status": "analyzer_ready", "analyzer_id": ANALYZER_ID, "credential_source": credential_source, "smoke_required": True,
        "analyzer_changed": True, "cu_defaults_updated": defaults_updated,
        "embedding": embedding_info,
        "runtime_environment": {
            "NORTHWIND_CU_ENABLED": "false",
            "NORTHWIND_CU_ENDPOINT": endpoint,
            "NORTHWIND_CU_ANALYZER_ID": ANALYZER_ID,
            "NORTHWIND_CU_COMPLETION_MODEL": model,
            "NORTHWIND_CU_COMPLETION_DEPLOYMENT": deployment,
            "NORTHWIND_CU_EMBEDDING_DEPLOYMENT": embedding_deployment,
        },
        "message": "Insurance analyzer and any explicitly requested CU model-role mapping only; Foundry defaultProject unchanged. Enable the API only for a bounded live smoke before claiming extraction readiness.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--configure-defaults", action="store_true",
                        help="With --apply, add only a missing selected CU model-role mapping; preserve other mappings and reject conflicts. Never changes Foundry defaultProject.")
    parser.add_argument("--completion-model", choices=("gpt-4.1", "gpt-4o"), default="gpt-4.1")
    parser.add_argument("--completion-deployment", default="")
    parser.add_argument("--embedding-deployment", default="",
                        help="Existing embedding deployment for the live base analyzer's inherited role; its actual model must be advertised by supportedModels.embedding.")
    parser.add_argument("--credential-source", choices=("cli", "azd"), default="azd",
                        help="Exact data-plane credential source, without fallback; metadata reads always use subscription-pinned Azure CLI.")
    args = parser.parse_args(argv)
    try:
        if not args.apply:
            result = {
                "mode": "offline_preview", "target": ACCOUNT_ID, "tenant": TENANT,
                "credential_source": args.credential_source,
                "configure_defaults": args.configure_defaults,
                "embedding_deployment": args.embedding_deployment,
                "analyzer_id": ANALYZER_ID, "api_version": API_VERSION,
                "definition": definition(args.completion_model), "reference": DOCS,
                "analysis_contract": "Base64 document bytes in inputs[].data with mimeType; explicit request-scoped modelDeployments.",
                "embedding_contract": "Keep the base analyzer's inherited embedding role. Read its role/supportedModels and the actual existing deployment model before mapping that role; never label a small deployment as a large model.",
                "cloud_writes": [
                    "PUT the owned versioned insurance analyzer only with --apply",
                    "PATCH missing selected completion/inherited embedding CU model-role defaults mappings only with --apply --configure-defaults; preserve existing mappings and reject conflicts",
                ],
                "not_performed": ["model/resource creation", "Foundry defaultProject changes", "CU defaults changes without --configure-defaults", "analysis calls", "worker restart", "network changes"],
            }
        else:
            if not args.completion_deployment:
                raise ValueError("--apply requires --completion-deployment naming an existing supported deployment.")
            result = apply(args.completion_model, args.completion_deployment, reconcile=args.reconcile,
                           credential_source=args.credential_source, configure_defaults=args.configure_defaults,
                           embedding_deployment=args.embedding_deployment)
        print(json.dumps(result, indent=2))
        return 0
    except ValueError as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}))
        return 1
    except (OSError, subprocess.SubprocessError, httpx.HTTPError):
        # The exception text from an HTTP client/CLI can contain credentials or URLs.
        print(json.dumps({"status": "failed", "message": "CU configuration was not verified. Inspect the owned analyzer and any opt-in CU model-role defaults update before retrying. No model/resource creation, Foundry defaultProject, worker or network change was requested."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
