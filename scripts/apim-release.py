"""Plan or explicitly apply only the approved API Management configuration boundary for apim-northwind."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
GROUP = "rg-northwind-apps"
APIM_NAME = "apim-northwind"
APP_INSIGHTS_NAME = "appi-zurlo-northwind"
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "live" / "apim-apis.bicep"
# Adding a stack is a deliberate boundary change: it must be reviewed here, not only in Bicep.
STACKS = ("northwind", "insurance")
API_SUFFIXES = ("api", "voice", "mcp-tech-lookup", "mcp-work-order")
PRODUCT_SUFFIXES = ("web", "agents")
LOGGER = "northwind-appinsights"
API_NAMES = tuple(f"{stack}-{suffix}" for stack in STACKS for suffix in API_SUFFIXES)
PRODUCT_NAMES = tuple(f"{stack}-{suffix}" for stack in STACKS for suffix in PRODUCT_SUFFIXES)
AGENT_SUBSCRIPTIONS = tuple(f"{stack}-agents" for stack in STACKS)
# A product only ever exposes APIs of its own stack.
PRODUCT_LINKS = frozenset(
    (f"{stack}-{product}", f"{stack}-{api}")
    for stack in STACKS for product in PRODUCT_SUFFIXES for api in API_SUFFIXES
)
TOP_LEVEL = {
    "apis": API_NAMES, "products": PRODUCT_NAMES, "subscriptions": AGENT_SUBSCRIPTIONS, "loggers": (LOGGER,),
}
# WebSocket APIs accept policies only on their system-created onHandshake operation, never at API scope.
VOICE_APIS = tuple(f"{stack}-voice" for stack in STACKS)
API_POLICY_APIS = tuple(name for name in API_NAMES if name not in VOICE_APIS)
HANDSHAKE = "onhandshake"
STACK_KEYS = ("name", "apiApp", "techLookupApp", "workOrderApp", "webApp", "voiceApp")
SERVICE_TYPE = "Microsoft.ApiManagement/service"
# Adding a resource type is a deliberate boundary change: update this allow-list, the apim-apply role
# (APIM_WRITES in configure-github-oidc.py) and their tests together, never just the Bicep template.
RESOURCE_TYPES = frozenset(f"{SERVICE_TYPE}/{child}".lower() for child in (
    "apis", "apis/operations", "apis/operations/policies", "apis/policies", "apis/diagnostics",
    "products", "products/apis", "subscriptions", "loggers",
))
SERVICE_SCOPE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/{SERVICE_TYPE}/{APIM_NAME}/".lower()
APIS_URL = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
            f"/providers/{SERVICE_TYPE}/{APIM_NAME}/apis")
APIS_API_VERSION = "2024-05-01"
SHA = re.compile(r"[0-9a-f]{40}")
OPERATION = re.compile(r"[a-z0-9_][a-z0-9_.-]{0,79}")
GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
AUDIENCE = re.compile(rf"(?:api://)?{GUID.pattern}")
ORIGIN = re.compile(r"https?://[A-Za-z0-9.-]+(?::[0-9]+)?")
SCOPE = re.compile(r"[A-Za-z0-9._-]{1,64}")
APP_NAME = re.compile(r"ca-[a-z0-9-]+")


def allowed_change(resource_id: object) -> bool:
    """True only for the exact API Management children this boundary may create or modify."""
    if not isinstance(resource_id, str) or not resource_id.isascii():
        return False
    identifier = resource_id.lower()
    if not identifier.startswith(SERVICE_SCOPE):
        return False
    parts = identifier[len(SERVICE_SCOPE):].split("/")
    if len(parts) == 2:
        collection, name = parts
        return name in TOP_LEVEL.get(collection, ())
    if len(parts) == 6:
        return (parts[0] == "apis" and parts[1] in VOICE_APIS
                and parts[2:] == ["operations", HANDSHAKE, "policies", "policy"])
    if len(parts) != 4:
        return False
    if parts[0] == "apis" and parts[1] in API_NAMES:
        kind, name = parts[2:]
        if kind == "policies":
            return name == "policy" and parts[1] in API_POLICY_APIS
        if kind == "diagnostics":
            return name == "applicationinsights"
        # REST operations are created by the OpenAPI import, so any safe operation id is acceptable.
        return kind == "operations" and OPERATION.fullmatch(name) is not None
    return parts[0] == "products" and parts[2] == "apis" and (parts[1], parts[3]) in PRODUCT_LINKS


def command(*args: str) -> dict:
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI is required for an API Management plan.")
    result = subprocess.run([executable, *args, "--output", "json", "--only-show-errors"],
                            check=False, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("Azure operation failed. No successful deployment is implied.")
    return json.loads(result.stdout)


def _exact(expected: str):
    def check(value: object) -> None:
        if value != expected:
            raise ValueError("Live target parameters differ from the approved resource boundary.")
    return check


def _check_stacks(value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(stack, dict) for stack in value):
        raise ValueError("stacks must be a list of stack objects.")
    names = [stack.get("name") for stack in value]
    if not all(isinstance(name, str) for name in names) or sorted(names) != sorted(STACKS):
        raise ValueError("Stacks differ from the approved list; adding one requires editing the release boundary.")
    for stack in value:
        if set(stack) != set(STACK_KEYS):
            raise ValueError(f"Each stack must carry exactly these keys: {', '.join(STACK_KEYS)}.")
        if not all(isinstance(stack[key], str) and APP_NAME.fullmatch(stack[key]) for key in STACK_KEYS[1:]):
            raise ValueError("Stack Container App names must match ca-[a-z0-9-]+.")


def _check_origins(value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(o, str) and ORIGIN.fullmatch(o) for o in value):
        raise ValueError("extraAllowedOrigins must be plain http(s) origins without paths or wildcards.")


def _check_audience(value: object) -> None:
    if value != "" and not (isinstance(value, str) and AUDIENCE.fullmatch(value)):
        raise ValueError("gatewayAudience must be empty, a GUID or api://<GUID>.")


def _check_business_auth(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("businessAuth must be an object.")
    if not value:
        return
    identifiers = value.get("clientIds")
    if (set(value) != {"tenantId", "audience", "clientIds", "scope"}
            or not all(isinstance(value[key], str) and GUID.fullmatch(value[key]) for key in ("tenantId", "audience"))
            or not isinstance(identifiers, list) or not identifiers
            or not all(isinstance(item, str) and GUID.fullmatch(item) for item in identifiers)
            or not isinstance(value["scope"], str) or not SCOPE.fullmatch(value["scope"])):
        raise ValueError("businessAuth must carry only GUID tenant/audience/client ids and a simple scope.")


def _check_limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100000:
        raise ValueError("Rate limits must be integers between 1 and 100000 calls per minute.")


# Every template parameter needs a reviewed default here; the deployment never receives overrides.
PARAMETER_CHECKS = {
    "apimName": _exact(APIM_NAME),
    "appInsightsName": _exact(APP_INSIGHTS_NAME),
    "stacks": _check_stacks,
    "extraAllowedOrigins": _check_origins,
    "gatewayAudience": _check_audience,
    "businessAuth": _check_business_auth,
    "webRateLimitPerMinute": _check_limit,
    "agentsRateLimitPerMinute": _check_limit,
}


def _declared(template: dict) -> list:
    # `existing` is only meaningful (a read-only reference) in symbolic-name templates.
    resources = template.get("resources")
    if str(template.get("languageVersion", "")).startswith("2.") and isinstance(resources, dict):
        resources = [r for r in resources.values() if not (isinstance(r, dict) and r.get("existing") is True)]
    if not isinstance(resources, list):
        raise ValueError("Expected a resource-group scoped template with a resources collection.")
    return resources


def _check_resource(resource: object) -> int:
    if not isinstance(resource, dict):
        raise ValueError("Template resources must be objects.")
    kind = str(resource.get("type", "")).lower()
    if kind == SERVICE_TYPE.lower():
        raise ValueError("The API Management service must be referenced as existing, never declared.")
    if kind not in RESOURCE_TYPES:
        raise ValueError(f"Template resource type {resource.get('type')!r} is outside the API Management boundary.")
    if {"scope", "subscriptionId", "resourceGroup"} & resource.keys():
        raise ValueError("Cross-scope template resources are not permitted.")
    nested = resource.get("resources", [])
    if not isinstance(nested, list):
        raise ValueError("Template resources must be objects.")
    return 1 + sum(_check_resource(child) for child in nested)


def validate_template(template: dict) -> None:
    schema = str(template.get("$schema", "")).lower() if isinstance(template, dict) else ""
    if not schema.endswith("/deploymenttemplate.json#"):
        raise ValueError("Expected a resource-group deployment template.")
    if not sum(_check_resource(resource) for resource in _declared(template)):
        raise ValueError("Template declares no API Management resources.")
    parameters = template.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("Template parameters must be an object.")
    unknown, missing = sorted(set(parameters) - set(PARAMETER_CHECKS)), sorted(set(PARAMETER_CHECKS) - set(parameters))
    if unknown or missing:
        raise ValueError(
            "Template parameters must be exactly the approved set "
            f"(unexpected: {', '.join(unknown) or 'none'}; missing: {', '.join(missing) or 'none'})."
        )
    for name, definition in parameters.items():
        if not isinstance(definition, dict) or "defaultValue" not in definition:
            raise ValueError(f"Parameter {name} must carry a reviewed default value.")
        PARAMETER_CHECKS[name](definition["defaultValue"])


def safe_plan(result: dict) -> dict:
    potential = result.get("potentialChanges") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or result.get("status") not in (None, "Succeeded") or result.get("error")
            or not isinstance(result.get("changes"), list) or not isinstance(potential or [], list)):
        raise ValueError("API Management plan did not complete successfully.")
    changes = []
    # Changes What-If could only predict as possible are held to the same allow-list.
    for change in [*result["changes"], *(potential or [])]:
        change = change if isinstance(change, dict) else {}
        kind = change.get("changeType")
        if kind in {"Ignore", "NoChange"}:
            continue
        identifier = change.get("resourceId", "")
        if kind not in {"Create", "Modify"} or not allowed_change(identifier):
            raise ValueError(
                f"Plan contains an unsupported change or resource ({kind!r} {str(identifier)[:300]!r}); "
                "no apply is permitted."
            )
        changes.append({"resource_id": identifier.lower(), "change_type": kind})
    return {"changes": sorted(changes, key=lambda change: change["resource_id"])}


def make_plan(compiled: Path) -> dict:
    validate_template(json.loads(compiled.read_text(encoding="utf-8")))
    return command(
        "deployment", "group", "what-if", "--subscription", SUBSCRIPTION,
        "--resource-group", GROUP, "--name", "northwind-apim-plan",
        "--template-file", str(compiled), "--mode", "Incremental", "--no-pretty-print",
        "--validation-level", "ProviderNoRbac",
    )


def plan_digest(result: dict) -> str:
    # What-if does not promise a stable listing order; compare the reviewed content, not its order.
    stable = dict(result)
    for key in ("changes", "potentialChanges"):
        if isinstance(stable.get(key), list):
            stable[key] = sorted(stable[key], key=lambda change: json.dumps(change, sort_keys=True))
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def envelope(compiled: Path, result: dict, sha: str) -> dict:
    plan = safe_plan(result)
    return {
        "source_sha": sha, "subscription": SUBSCRIPTION, "resource_group": GROUP,
        "template_sha256": hashlib.sha256(compiled.read_bytes()).hexdigest(),
        "plan_sha256": plan_digest(result),
        **plan,
    }


def verify_apis() -> list[str]:
    names: set[str] = set()
    url, extra = APIS_URL, ["--url-parameters", f"api-version={APIS_API_VERSION}"]
    for _ in range(20):
        page = command("rest", "--method", "get", "--url", url, *extra)
        if not isinstance(page, dict):
            raise RuntimeError("API Management returned an unexpected API listing.")
        names.update(str(item.get("name", "")).lower() for item in page.get("value", []) if isinstance(item, dict))
        url = page.get("nextLink")
        if not url:
            break
        if not isinstance(url, str) or not url.startswith(APIS_URL):
            raise RuntimeError("API Management returned an unexpected continuation link.")
        extra = []
    missing = [name for name in API_NAMES if name not in names]
    if missing:
        raise RuntimeError(f"Expected API Management APIs are missing after deployment: {', '.join(missing)}.")
    return sorted(API_NAMES)


def render_summary(summary: dict) -> str:
    applied = summary.get("applied") is True
    lines = [
        f"## API Management {'apply' if applied else 'plan'}: `{APIM_NAME}`", "",
        f"- Source commit: `{summary['source_sha']}`",
        f"- Resource group: `{summary['resource_group']}`",
        f"- Template SHA-256: `{summary['template_sha256']}`",
        f"- Plan SHA-256: `{summary['plan_sha256']}`",
        "- Result: " + ("applied" if applied else "plan only; nothing was changed"), "",
    ]
    if summary["changes"]:
        lines += [f"| Change | Resource under `{APIM_NAME}` |", "| --- | --- |"]
        lines += [
            f"| {change['change_type']} | `{change['resource_id'].removeprefix(SERVICE_SCOPE)}` |"
            for change in summary["changes"]
        ]
    else:
        lines.append("No API Management changes.")
    if applied:
        verified = ", ".join(f"`{name}`" for name in summary.get("verified_apis", []))
        lines += ["", f"Verified APIs: {verified or 'not confirmed'}"]
    return "\n".join(lines) + "\n\n"


def publish(summary: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            stream.write(render_summary(summary))
    print(json.dumps(summary, indent=2))


def load_approved(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("The approved API Management plan is missing or unreadable.") from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiled-template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--approved-plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not SHA.fullmatch(args.source_sha):
        raise ValueError("An exact reviewed Git commit SHA is required.")
    if args.apply and (not args.approved_plan or os.environ.get("NORTHWIND_APIM_CONFIRM") != args.source_sha):
        raise ValueError("Apply requires explicit confirmation of the reviewed commit and plan.")
    result = make_plan(args.compiled_template)
    summary = envelope(args.compiled_template, result, args.source_sha)
    if args.apply:
        if summary != load_approved(args.approved_plan):
            raise ValueError("API Management plan or source changed after review. Generate and review a new plan.")
        command(
            "deployment", "group", "create", "--subscription", SUBSCRIPTION,
            "--resource-group", GROUP, "--name", f"northwind-apim-{args.source_sha[:12]}",
            "--template-file", str(args.compiled_template), "--mode", "Incremental",
        )
        summary["applied"] = True
        try:
            summary["verified_apis"] = verify_apis()
        except RuntimeError:
            publish(summary, args.output)  # Keep the evidence that the deployment itself ran.
            raise
    publish(summary, args.output)


if __name__ == "__main__":
    main()
