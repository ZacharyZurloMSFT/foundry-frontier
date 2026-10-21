"""Release existing Northwind apps without changing their configuration or capabilities."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.app.insurance_config import (
    ALIASES, INSURANCE_ACCOUNT_HOST, INSURANCE_AGENT_NAMES, INSURANCE_PROJECTS,
    configuration_hash, insurance_routing, validate_fleet_receipt, valid_project_endpoint,
)
SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
RESOURCE_GROUP = "rg-northwind-apps"
REGISTRY = "acrzurlonorthwind.azurecr.io"
REPOSITORY = "ZacharyZurloMSFT/foundry-frontier"
SERVICES = ("api", "mcp-tech-lookup", "mcp-work-order", "web-tech", "web-voice")
APPS = {f"ca-{service}": service for service in SERVICES}
BUILD_NAMES = ("GIT_SHA", "BUILD_ENV", "DEPLOYED_AT", "GITHUB_REPOSITORY")
API_VERSION = {"api-version": "2024-03-01"}
SHA = re.compile(r"[0-9a-f]{40}")
ROUTING_NAMES = {
    *insurance_routing(), "NORTHWIND_INSURANCE_AGENTS_READY",
    *(alias for primary in ("INSURANCE_PROJECT_NAME", "INSURANCE_PROJECT_ENDPOINT", "AGENTS_HOSTED")
      for alias in ALIASES[primary]),
}


class ReleaseError(RuntimeError):
    """A safe-to-display release failure (never includes an ARM response body)."""


def changed_services(paths: list[str]) -> list[str]:
    selected = set()
    for path in paths:
        path = path.replace("\\", "/")
        if path.startswith((".github/workflows/", "scripts/", "agents/copilot-harness/")):
            return list(SERVICES)
        if path in ("azure.yaml", ".dockerignore"):
            return list(SERVICES)
        if path.startswith(("data/northwind/", "data/seed/generated/")):
            selected.update(("api", "mcp-tech-lookup", "mcp-work-order"))
        if path == "apps/api/app/insurance_domain.py":
            selected.update(("mcp-tech-lookup", "mcp-work-order"))
        for service in SERVICES:
            if path.startswith(f"apps/{service}/"):
                selected.add(service)
    return [service for service in SERVICES if service in selected]


def plan(source_sha: str, base_sha: str | None, service: str | None = None) -> list[str]:
    if not SHA.fullmatch(source_sha):
        raise ValueError("Source must be a full Git commit SHA")
    if service is not None:
        if service not in (*SERVICES, "all"):
            raise ValueError("Unknown service")
        return list(SERVICES) if service == "all" else [service]
    if not base_sha:
        return list(SERVICES)
    if not SHA.fullmatch(base_sha):
        raise ValueError("Base must be a full Git commit SHA")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, source_sha],
        capture_output=True, check=False,
    )
    if ancestor.returncode:
        return list(SERVICES)
    result = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", "-z", base_sha, source_sha, "--"],
        capture_output=True, check=True,
    )
    return changed_services(result.stdout.decode("utf-8", errors="strict").split("\0"))


def validate_frontend_environment(env: dict[str, str]) -> None:
    if env.get("VITE_INSURANCE_BUSINESS_ENABLED", "false") not in ("true", "false"):
        raise ValueError("Business frontend flag must be true or false")
    if env.get("VITE_INSURANCE_BUSINESS_ENABLED") != "true":
        return
    uuid = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    for name in ("VITE_INSURANCE_AUTH_TENANT_ID", "VITE_INSURANCE_AUTH_CLIENT_ID"):
        if not re.fullmatch(uuid, env.get(name, "")):
            raise ValueError("Enabled business frontend requires explicit tenant/client UUIDs")
    if not re.fullmatch(r"(?:api|https)://[^\s]+/[^\s/]+", env.get("VITE_INSURANCE_AUTH_SCOPE", "")):
        raise ValueError("Enabled business frontend requires a full delegated API scope URI")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", env.get("VITE_INSURANCE_RUN_ID", "")):
        raise ValueError("Enabled business frontend requires an explicit valid run ID")


def _valid_metadata(name: str, value: str) -> bool:
    if not isinstance(value, str):
        return False
    if name == "GIT_SHA":
        return value in ("", "local") or SHA.fullmatch(value) is not None
    if name == "BUILD_ENV":
        return value in ("", "local", "github-actions", "azure-dev", "azd")
    if name == "GITHUB_REPOSITORY":
        return value in ("", REPOSITORY)
    if not value:
        return True
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def verification_receipt(args: argparse.Namespace) -> dict:
    path = getattr(args, "verification_receipt", None)
    if path is None:
        raise ValueError("Activation requires --verification-receipt and its independently recorded SHA-256")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != getattr(args, "verification_sha256", None):
        raise ValueError("Verification receipt SHA-256 does not match")
    receipt = json.loads(data)
    validate_fleet_receipt(receipt, args.git_sha)
    return receipt


def insurance_changes(args: argparse.Namespace) -> dict[str, str | None]:
    mode = getattr(args, "insurance_mode", None)
    if mode is None:
        return {}
    if args.app != "ca-api":
        raise ValueError("Insurance routing changes are restricted to ca-api")
    if mode == "disable":
        return {"NORTHWIND_INSURANCE_AGENTS_READY": "false"}
    if mode not in ("configure", "activate"):
        raise ValueError("Unknown insurance release mode")
    if mode == "activate":
        verification_receipt(args)
    changes: dict[str, str | None] = {name: None for name in ROUTING_NAMES}
    changes.update(insurance_routing())
    changes["NORTHWIND_INSURANCE_AGENTS_READY"] = "true" if mode == "activate" else "false"
    return changes


def validate_release(args: argparse.Namespace) -> dict[str, str | None]:
    if getattr(args, "insurance_mode", None) is not None and args.app != "ca-api":
        raise ValueError("Insurance routing changes are restricted to ca-api")
    if (args.subscription, args.tenant, args.resource_group) != (SUBSCRIPTION, TENANT, RESOURCE_GROUP):
        raise ValueError("Release scope is restricted to the existing Northwind apps resource group")
    if args.app not in APPS:
        raise ValueError("Unknown Container App")
    image_pattern = rf"{re.escape(REGISTRY)}/{APPS[args.app]}@sha256:[0-9a-f]{{64}}"
    if not re.fullmatch(image_pattern, args.image):
        raise ValueError("Image must be the app's approved ACR repository pinned by SHA-256 digest")
    if not re.fullmatch(r"gha-[1-9][0-9]*-[1-9][0-9]*-[0-9a-f]{12}", args.revision):
        raise ValueError("Revision must contain the GitHub run ID, attempt and source SHA prefix")
    if len(args.revision) > 54:
        raise ValueError("Revision suffix is too long")
    if not re.fullmatch(re.escape(args.app) + r"--[a-z0-9][a-z0-9-]*", args.expected_revision):
        raise ValueError("Expected revision must belong to the selected app")
    if args.expected_revision == f"{args.app}--{args.revision}":
        raise ValueError("Refusing to reuse the currently deployed revision suffix")
    metadata = (args.git_sha, args.deployed_at, args.github_repository)
    if args.app != "ca-api":
        if any(value is not None for value in metadata):
            raise ValueError("Only the API may receive build metadata environment changes")
        return {}
    changes = dict(zip(BUILD_NAMES, (args.git_sha, "github-actions", args.deployed_at, args.github_repository)))
    if (not args.git_sha or not SHA.fullmatch(args.git_sha)
            or args.github_repository != REPOSITORY or not args.deployed_at
            or not _valid_metadata("DEPLOYED_AT", args.deployed_at)):
        raise ValueError("API releases require validated source SHA, timestamp and repository")
    if not args.revision.endswith(args.git_sha[:12]):
        raise ValueError("API revision does not match its build SHA")
    return {**changes, **insurance_changes(args)}


def _request(http, method: str, url: str, token, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token()}", **kwargs.pop("headers", {})}
    try:
        response = http.request(method, url, params=API_VERSION, headers=headers, **kwargs)
    except httpx.RequestError:
        raise ReleaseError(f"ARM {method} request failed or timed out; do not blindly retry a mutation") from None
    if response.status_code == 404 and method == "GET" and "/revisions/" in url:
        return {}
    if not response.is_success:
        raise ReleaseError(f"ARM {method} failed (HTTP {response.status_code}); rollback record retained") from None
    if not response.content:
        return {}
    try:
        body = response.json()
    except ValueError:
        raise ReleaseError("ARM returned an invalid JSON response") from None
    if not isinstance(body, dict):
        raise ReleaseError("ARM returned an invalid resource response")
    if response.headers.get("etag"):
        body["etag"] = response.headers["etag"]
    return body


def _guard(current: dict, args: argparse.Namespace) -> None:
    properties = current.get("properties", {})
    if (properties.get("latestReadyRevisionName") != args.expected_revision
            or properties.get("latestRevisionName") != args.expected_revision
            or properties.get("provisioningState") != "Succeeded"):
        raise ReleaseError("App revision changed or another deployment is in progress")
    if properties.get("configuration", {}).get("activeRevisionsMode") != "Single":
        raise ReleaseError("Only existing single-revision apps are supported")


def _previous_metadata(container: dict, changes: dict) -> dict:
    previous = {}
    for name in changes:
        entries = [entry for entry in container.get("env", []) if entry.get("name") == name]
        if len(entries) > 1:
            raise ReleaseError("Duplicate build metadata environment entries")
        entry = entries[0] if entries else None
        if entry is not None and (
            set(entry) - {"name", "value", "secretRef"} or entry.get("secretRef")
            or not (_valid_insurance_value(name, entry.get("value")) if name in ROUTING_NAMES
                    else _valid_metadata(name, entry.get("value")))
        ):
            raise ReleaseError("Existing build metadata is not safe for a nonsecret rollback record")
        previous[name] = entry["value"] if entry else None
    return previous


def _valid_insurance_value(name: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return True
    if name == "NORTHWIND_BUSINESS_DOMAIN":
        return value in ("retail", "insurance")
    if name in ("AGENTS_HOSTED", "ORDER_AGENTS_HOSTED", "NORTHWIND_INSURANCE_AGENTS_READY"):
        return value in ("true", "false")
    projects = {*INSURANCE_PROJECTS.values(), "northwind-orders", "nw-employee-help-desk",
                "nw-claims-warranty", "nw-contact-center"}
    if name in ("INSURANCE_PROJECT_NAME", "ORDER_PROJECT_NAME"):
        return value in projects
    if name == "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID":
        return value in (INSURANCE_AGENT_NAMES["orchestrator"], "northwind-orchestrator")
    if name == "FOUNDRY_PROJECT_ENDPOINTS":
        try:
            parsed = json.loads(value)
        except ValueError:
            return False
        return (isinstance(parsed, dict) and set(parsed) == set(INSURANCE_PROJECTS)
                and all(_valid_insurance_value("INSURANCE_PROJECT_ENDPOINT", item) for item in parsed.values()))
    try:
        endpoint = urlsplit(value)
        project = endpoint.path.rstrip("/").rsplit("/", 1)[-1]
        return (
            project in projects and endpoint.hostname in (
                INSURANCE_ACCOUNT_HOST, INSURANCE_ACCOUNT_HOST.replace(".services.", ".cognitiveservices."),
            ) and valid_project_endpoint(value, project)
        )
    except ValueError:
        return False


def _fqdn(properties: dict) -> str:
    fqdn = properties.get("configuration", {}).get("ingress", {}).get("fqdn", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.azurecontainerapps\.io", fqdn):
        raise ReleaseError("App must have an existing Azure Container Apps ingress hostname")
    return fqdn


def release(args: argparse.Namespace, http, token, *, sleep=time.sleep, clock=time.monotonic,
            pre_activation=None) -> dict:
    changes = validate_release(args)
    resource = (f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}"
                f"/providers/Microsoft.App/containerApps/{args.app}")
    url = f"https://management.azure.com{resource}"
    before = _request(http, "GET", url, token)
    _guard(before, args)
    properties = before["properties"]
    _fqdn(properties)
    template = copy.deepcopy(properties["template"])
    if len(template.get("containers", [])) != 1:
        raise ReleaseError("Refusing to modify a multi-container app")
    container = template["containers"][0]
    previous_image = container.get("image", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9./_-]*(?::[A-Za-z0-9_.-]+|@sha256:[0-9a-f]{64})", previous_image):
        raise ReleaseError("Previous image is not a safe nonsecret image reference")
    previous_metadata = _previous_metadata(container, changes)
    container["image"] = args.image
    if changes:
        container["env"] = [
            entry for entry in container.get("env", []) if entry.get("name") not in changes
        ] + [{"name": name, "value": value} for name, value in changes.items() if value is not None]
    template["revisionSuffix"] = args.revision
    expected = f"{args.app}--{args.revision}"
    result = {"app": args.app, "revision": expected, "image": args.image}
    if not args.apply:
        return {**result, "state": "dry-run", "changed_environment_names": list(changes)}
    rollback = {
        "app": args.app, "previous_revision": args.expected_revision,
        "previous_image": previous_image,
    }
    if changes:
        rollback["previous_build_metadata"] = previous_metadata
    if getattr(args, "insurance_mode", None):
        rollback["insurance_mode"] = args.insurance_mode
        rollback["restore_readiness"] = False
    args.rollback_file.parent.mkdir(parents=True, exist_ok=True)
    # Never persist the ARM template: even environment values can contain inline secrets.
    descriptor = os.open(args.rollback_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(rollback, stream, indent=2)
        stream.write("\n")
    current = _request(http, "GET", url, token)
    _guard(current, args)
    if (current.get("etag") != before.get("etag")
            or current["properties"]["template"] != properties["template"]
            or current["properties"]["configuration"] != properties["configuration"]
            or current.get("identity") != before.get("identity")):
        raise ReleaseError("App configuration drifted before mutation; rollback record retained")
    headers = {"If-Match": before["etag"]} if before.get("etag") else {}
    if getattr(args, "insurance_mode", None) == "activate":
        if pre_activation is None:
            raise ReleaseError("Activation requires a fresh independent serving-version check")
        pre_activation()
    _request(http, "PATCH", url, token, headers=headers, json={"properties": {"template": template}})
    deadline = clock() + args.timeout
    while clock() < deadline:
        current = _request(http, "GET", url, token)["properties"]
        if current.get("latestRevisionName") not in (args.expected_revision, expected):
            raise ReleaseError("A different release superseded this one; no rollback was attempted")
        if current.get("provisioningState") in ("Failed", "Canceled"):
            raise ReleaseError("Release provisioning failed; rollback record retained")
        revision = _request(http, "GET", f"{url}/revisions/{expected}", token).get("properties", {})
        if revision.get("provisioningState") == "Failed" or revision.get("runningState") in ("Failed", "Degraded"):
            raise ReleaseError("New revision failed; rollback record retained")
        if (current.get("latestReadyRevisionName") == expected
                and current.get("latestRevisionName") == expected
                and current.get("provisioningState") == "Succeeded"
                and current.get("configuration", {}).get("activeRevisionsMode") == "Single"
                and current.get("template", {}).get("containers", [{}])[0].get("image") == args.image
                and revision.get("active") is True and revision.get("trafficWeight") == 100
                and revision.get("healthState") == "Healthy"
                and revision.get("provisioningState") == "Provisioned"
                and revision.get("runningState") == "Running"):
            path = "/health" if args.app == "ca-api" else "/healthz" if "mcp-" in args.app else "/"
            try:
                smoke = http.get(f"https://{_fqdn(current)}{path}", follow_redirects=False)
            except httpx.RequestError:
                raise ReleaseError("Smoke request failed or timed out; rollback record retained") from None
            if not smoke.is_success:
                raise ReleaseError(f"Smoke failed (HTTP {smoke.status_code}); rollback record retained")
            return {**result, "state": "ready", "smoke": "passed"}
        sleep(5)
    raise ReleaseError("Timed out waiting for ready revision and 100% traffic; rollback record retained")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    planner = commands.add_parser("plan", help="Select changed services using a known successful deployment ancestor")
    planner.add_argument("--source-sha", required=True)
    planner.add_argument("--base-sha")
    planner.add_argument("--service", choices=(*SERVICES, "all"))
    commands.add_parser("check-frontend", help="Reject invalid enabled business frontend build configuration")
    deploy = commands.add_parser("release", help="Dry-run by default; no automatic rollback or mutation retries")
    deploy.add_argument("--subscription", required=True)
    deploy.add_argument("--tenant", required=True)
    deploy.add_argument("--resource-group", required=True)
    deploy.add_argument("--app", choices=APPS, required=True)
    deploy.add_argument("--image", required=True)
    deploy.add_argument("--revision", required=True)
    deploy.add_argument("--expected-revision", required=True)
    deploy.add_argument("--rollback-file", type=Path, required=True)
    deploy.add_argument("--git-sha")
    deploy.add_argument("--deployed-at")
    deploy.add_argument("--github-repository")
    deploy.add_argument("--insurance-mode", choices=("disable", "configure", "activate"))
    deploy.add_argument("--verification-receipt", type=Path)
    deploy.add_argument("--verification-sha256")
    deploy.add_argument("--timeout", type=int, default=600)
    deploy.add_argument("--apply", action="store_true")
    return result


def authenticated_release(args: argparse.Namespace) -> dict:
    from azure.core.exceptions import AzureError
    from azure.identity import AzureCliCredential, get_bearer_token_provider

    try:
        with AzureCliCredential(tenant_id=args.tenant) as credential, httpx.Client(timeout=30) as http:
            token = get_bearer_token_provider(credential, "https://management.azure.com/.default")
            def check_serving_versions():
                receipt = verification_receipt(args)
                ai_token = credential.get_token("https://ai.azure.com/.default").token
                base = insurance_routing()["INSURANCE_PROJECT_ENDPOINT"]
                for entry in receipt["agents"].values():
                    url = f"{base}/agents/{entry['name']}"
                    responses = [
                        http.get(
                            target, params={"api-version": "2025-11-15-preview"},
                            headers={"Authorization": f"Bearer {ai_token}"}, follow_redirects=False,
                        ) for target in (url, f"{url}/versions/{entry['version']}")
                    ]
                    if any(not response.is_success for response in responses):
                        raise ReleaseError("Cannot independently verify current insurance serving versions")
                    detail, version = (response.json() for response in responses)
                    rules = (detail.get("agent_endpoint") or {}).get("version_selector", {}).get("version_selection_rules", [])
                    definition = version.get("definition") or {}
                    if (len(rules) != 1 or rules[0].get("agent_version") != entry["version"]
                            or rules[0].get("traffic_percentage") != 100 or version.get("status") != "active"
                            or definition.get("code_configuration", {}).get("content_hash") != entry["source_sha256"]
                            or configuration_hash(definition.get("environment_variables") or {}) != entry["config_sha256"]
                            or any(version.get("metadata", {}).get(key) != entry[key] for key in (
                                "prompt_sha256", "source_manifest_sha256",
                            ))):
                        raise ReleaseError("Insurance fleet drifted after verification; activation refused")
            return release(args, http, token, pre_activation=check_serving_versions)
    except AzureError:
        raise ReleaseError("Azure authentication failed; review the federated app identity") from None


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "plan":
            print(json.dumps(plan(args.source_sha, args.base_sha, args.service)))
        elif args.command == "check-frontend":
            validate_frontend_environment(dict(os.environ))
        else:
            validate_release(args)
            if not 10 <= args.timeout <= 1800:
                raise ValueError("Timeout must be between 10 and 1800 seconds")
            print(json.dumps(authenticated_release(args)))
        return 0
    except (ReleaseError, ValueError) as error:
        print(f"Release refused: {error}", file=sys.stderr)
    except (OSError, httpx.HTTPError, subprocess.SubprocessError, ImportError, KeyError, TypeError) as error:
        print(f"Release failed ({type(error).__name__}); inspect the run, do not blindly retry or roll back",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
