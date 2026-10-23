"""Preview or apply gateway configuration on the three Northwind Insurance Container Apps; only --apply changes anything.

release-app.py intentionally changes nothing but the image and build metadata, so the gateway settings need their
own guarded path. Allow-listed per app: the NORTHWIND_GATEWAY_* guard variables on the API and both MCP apps, and on
the API app also MCP_POLICY_LOOKUP_URL / MCP_CLAIM_REVIEW_URL (re-pointed at the gateway) plus the gateway subscription
key as a Container App secret referenced by MCP_GATEWAY_SUBSCRIPTION_KEY. The deprecated MCP_TECH_LOOKUP_URL /
MCP_WORK_ORDER_URL names are never written; they are accepted only when restoring an older rollback record. Secret
values are read from the environment and never printed or recorded. The rollback record contains previous non-secret
values only. No automatic rollback.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
GROUP = "rg-northwind-apps"
API_VERSION = {"api-version": "2024-03-01"}
APPS = {
    "ca-insurance-api": ("insurance", "api"),
    "ca-insurance-lookup": ("insurance", "tech-lookup"),
    "ca-insurance-review": ("insurance", "work-order"),
}
# The MCP kinds are the live API Management identifiers (insurance-mcp-tech-lookup, /insurance/mcp/work-order).
HEALTH = {"api": "/health", "tech-lookup": "/healthz", "work-order": "/healthz"}
GUARD = ("NORTHWIND_GATEWAY_AUTH_REQUIRED", "NORTHWIND_GATEWAY_TENANT_ID", "NORTHWIND_GATEWAY_AUDIENCE",
         "NORTHWIND_GATEWAY_ALLOWED_OIDS")
API_ONLY = ("MCP_POLICY_LOOKUP_URL", "MCP_CLAIM_REVIEW_URL", "MCP_GATEWAY_SUBSCRIPTION_KEY")
# Deprecated aliases that older images and rollback records still use: restore-only, never written.
LEGACY_API_ONLY = ("MCP_TECH_LOOKUP_URL", "MCP_WORK_ORDER_URL")
SECRET_NAME = "mcp-gateway-key"
GUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


class ConfigError(RuntimeError):
    """A safe-to-display failure (never includes an ARM response body)."""


@dataclass(frozen=True)
class SecretRef:
    name: str


def allowed_names(app: str, *, restoring: bool = False) -> tuple[str, ...]:
    if APPS[app][1] != "api":
        return GUARD
    return GUARD + API_ONLY + (LEGACY_API_ONLY if restoring else ())


def validate(app: str, changes: dict, *, restoring: bool = False) -> None:
    if app not in APPS:
        raise ConfigError("Unknown Container App")
    stack, kind = APPS[app]
    for name, value in changes.items():
        if name not in allowed_names(app, restoring=restoring):
            raise ConfigError(f"{name} may not be changed on {app}")
        if value is None or isinstance(value, SecretRef):
            continue
        if restoring:
            if not isinstance(value, str) or len(value) > 2048 or "\n" in value:
                raise ConfigError(f"Invalid previous value for {name}")
            continue
        ok = {
            "NORTHWIND_GATEWAY_AUTH_REQUIRED": lambda v: v in ("true", "false"),
            "NORTHWIND_GATEWAY_TENANT_ID": lambda v: re.fullmatch(GUID, v),
            "NORTHWIND_GATEWAY_AUDIENCE": lambda v: re.fullmatch(rf"(?:api://)?{GUID}", v),
            "NORTHWIND_GATEWAY_ALLOWED_OIDS": lambda v: re.fullmatch(rf"{GUID}(?:,{GUID})*", v),
            "MCP_POLICY_LOOKUP_URL": lambda v: re.fullmatch(rf"https://[a-z0-9-]+\.azure-api\.net/{stack}/mcp/tech-lookup", v),
            "MCP_CLAIM_REVIEW_URL": lambda v: re.fullmatch(rf"https://[a-z0-9-]+\.azure-api\.net/{stack}/mcp/work-order", v),
        }.get(name)
        if ok is None or not ok(value):
            raise ConfigError(f"Invalid value for {name}")
    if not restoring and changes.get("NORTHWIND_GATEWAY_AUTH_REQUIRED") == "true":
        missing = [n for n in GUARD[1:] if not changes.get(n)]
        if missing:
            raise ConfigError("Enabling the guard also requires " + ", ".join(missing))
    if kind != "api" and any(name in API_ONLY + LEGACY_API_ONLY for name in changes):
        raise ConfigError("MCP settings apply to the API apps only")


def build_changes(args: argparse.Namespace, app: str, environ=os.environ) -> dict:
    stack, kind = APPS[app]
    changes: dict = {}
    if args.guard == "enable":
        changes.update(NORTHWIND_GATEWAY_AUTH_REQUIRED="true", NORTHWIND_GATEWAY_TENANT_ID=(args.tenant_id or "").lower(),
                       NORTHWIND_GATEWAY_AUDIENCE=(args.gateway_audience or "").lower(),
                       NORTHWIND_GATEWAY_ALLOWED_OIDS=",".join(o.lower() for o in args.allowed_oid or []))
    elif args.guard == "disable":
        changes["NORTHWIND_GATEWAY_AUTH_REQUIRED"] = "false"
    if args.mcp_gateway_base and kind == "api":
        base = args.mcp_gateway_base.rstrip("/")
        if not base.endswith(f"/{stack}"):
            raise ConfigError(f"--mcp-gateway-base must end with /{stack} for {app}")
        changes.update(MCP_POLICY_LOOKUP_URL=f"{base}/mcp/tech-lookup", MCP_CLAIM_REVIEW_URL=f"{base}/mcp/work-order")
        if not environ.get(args.mcp_key_env):
            raise ConfigError(f"Set {args.mcp_key_env} to the {stack} agents subscription key (it is never printed)")
        changes["MCP_GATEWAY_SUBSCRIPTION_KEY"] = SecretRef(SECRET_NAME)
    validate(app, changes)
    return changes


def apply_env(container: dict, changes: dict) -> None:
    env = [entry for entry in container.get("env", []) if entry.get("name") not in changes]
    for name, value in changes.items():
        if isinstance(value, SecretRef):
            env.append({"name": name, "secretRef": value.name})
        elif value is not None:
            env.append({"name": name, "value": value})
    container["env"] = env


def previous_values(container: dict, changes: dict) -> dict:
    result = {}
    for name in changes:
        entries = [entry for entry in container.get("env", []) if entry.get("name") == name]
        if len(entries) > 1:
            raise ConfigError(f"Duplicate {name} environment entries")
        entry = entries[0] if entries else None
        result[name] = None if entry is None else ("<secret>" if entry.get("secretRef") else entry.get("value"))
    return result


def request(http: httpx.Client, method: str, url: str, token, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token()}", **kwargs.pop("headers", {})}
    try:
        response = http.request(method, url, params=API_VERSION, headers=headers, **kwargs)
    except httpx.RequestError:
        raise ConfigError(f"ARM {method} failed or timed out; do not blindly retry a mutation") from None
    if response.status_code == 404 and method == "GET":
        return {}
    if not response.is_success:
        raise ConfigError(f"ARM {method} failed (HTTP {response.status_code})")
    body = response.json() if response.content else {}
    if response.headers.get("etag"):
        body["etag"] = response.headers["etag"]
    return body


def configure_app(app: str, changes: dict, http: httpx.Client, token, *, apply: bool, rollback_dir: Path,
                  secret_value: str | None = None, restoring: bool = False, timeout: int = 600,
                  sleep=time.sleep, clock=time.monotonic) -> dict:
    validate(app, changes, restoring=restoring)
    url = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/Microsoft.App/containerApps/{app}"
    before = request(http, "GET", url, token)
    properties = before.get("properties", {})
    if not properties or properties.get("provisioningState") != "Succeeded":
        raise ConfigError(f"{app} is missing or not in a stable state")
    revision = properties.get("latestReadyRevisionName")
    if (properties.get("latestRevisionName") != revision
            or properties.get("configuration", {}).get("activeRevisionsMode") != "Single"):
        raise ConfigError(f"{app} has a deployment in progress or is not single-revision")
    template = copy.deepcopy(properties["template"])
    if len(template.get("containers", [])) != 1:
        raise ConfigError("Refusing to modify a multi-container app")
    container = template["containers"][0]
    previous = previous_values(container, changes)
    apply_env(container, changes)
    secrets = [s.get("name") for s in properties.get("configuration", {}).get("secrets") or []]
    wants_secret = any(isinstance(v, SecretRef) for v in changes.values())
    if wants_secret and (set(secrets) - {SECRET_NAME}):
        raise ConfigError("The app has other secrets whose values cannot be preserved; refusing to replace the secret list")
    template["revisionSuffix"] = f"gw{int(time.time()) % 10**8}"
    result = {"app": app, "changes": {n: ("<secret>" if isinstance(v, SecretRef) else v) for n, v in changes.items()},
              "previous": previous, "state": "dry-run"}
    if not apply:
        return result
    if wants_secret and not secret_value:
        raise ConfigError("The gateway subscription key is not available in the environment")
    rollback_dir.mkdir(parents=True, exist_ok=True)
    record = rollback_dir / f"gateway-rollback-{app}-{template['revisionSuffix']}.json"
    descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"app": app, "previous_revision": revision, "previous": previous,
                   "had_secret": SECRET_NAME in secrets}, stream, indent=2)
        stream.write("\n")
    body: dict = {"properties": {"template": template}}
    if wants_secret:
        body["properties"]["configuration"] = {"secrets": [{"name": SECRET_NAME, "value": secret_value}]}
    request(http, "PATCH", url, token, headers={"If-Match": before["etag"]} if before.get("etag") else {}, json=body)
    expected = f"{app}--{template['revisionSuffix']}"
    deadline = clock() + timeout
    while clock() < deadline:
        current = request(http, "GET", url, token).get("properties", {})
        if current.get("provisioningState") in ("Failed", "Canceled"):
            raise ConfigError(f"{app} provisioning failed; rollback record {record.name} retained")
        state = request(http, "GET", f"{url}/revisions/{expected}", token).get("properties", {})
        if (current.get("latestReadyRevisionName") == expected and current.get("provisioningState") == "Succeeded"
                and state.get("healthState") == "Healthy" and state.get("runningState") == "Running"
                and state.get("trafficWeight") == 100):
            fqdn = current["configuration"]["ingress"]["fqdn"]
            probe = http.get(f"https://{fqdn}{HEALTH[APPS[app][1]]}", follow_redirects=False)
            if not probe.is_success:
                raise ConfigError(f"Health probe failed (HTTP {probe.status_code}); rollback record {record.name} retained")
            verify_guard(http, app, fqdn, changes, record.name)
            return {**result, "state": "ready", "revision": expected}
        sleep(5)
    raise ConfigError(f"Timed out waiting for {expected}; rollback record {record.name} retained")


def verify_guard(http: httpx.Client, app: str, fqdn: str, changes: dict, record: str) -> None:
    """An image that predates the guard ignores the variables, so confirm the requested state is really served."""
    wanted = changes.get("NORTHWIND_GATEWAY_AUTH_REQUIRED")
    if wanted not in ("true", "false"):
        return
    kind = APPS[app][1]
    url = f"https://{fqdn}{'/company' if kind == 'api' else '/mcp/'}"
    response = http.request("GET" if kind == "api" else "POST", url, json=None if kind == "api" else {}, follow_redirects=False)
    if (response.status_code == 401) != (wanted == "true"):
        raise ConfigError(f"Guard state not confirmed on {app} (HTTP {response.status_code}); does the deployed image "
                          f"include the gateway guard? Rollback record {record} retained")


def targets(args: argparse.Namespace) -> list[str]:
    selected = list(args.app or [])
    if args.stack:
        selected += [name for name, (stack, _) in APPS.items() if args.stack in ("all", stack)]
    if not selected:
        raise ConfigError("Select --app or --stack")
    return list(dict.fromkeys(selected))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--app", action="append", choices=sorted(APPS))
    result.add_argument("--stack", choices=("insurance", "all"), help="every managed app (insurance is the only stack)")
    result.add_argument("--guard", choices=("enable", "disable"))
    result.add_argument("--tenant-id", default=TENANT)
    result.add_argument("--gateway-audience", help="the northwind-gateway application (client) ID")
    result.add_argument("--allowed-oid", action="append", help="object ID allowed to call, e.g. the APIM managed identity")
    result.add_argument("--mcp-gateway-base", help="e.g. https://apim-northwind.azure-api.net/insurance (API apps only)")
    result.add_argument("--mcp-key-env", default="NORTHWIND_MCP_KEY", help="environment variable holding the agents subscription key")
    result.add_argument("--rollback-dir", type=Path, default=Path("release"))
    result.add_argument("--revert-from", type=Path, help="restore the previous values recorded in a rollback file")
    result.add_argument("--apply", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        plans = []
        if args.revert_from:
            record = json.loads(args.revert_from.read_text(encoding="utf-8"))
            plans = [(record["app"], {n: (None if v in (None, "<secret>") else v) for n, v in record["previous"].items()})]
        else:
            plans = [(app, build_changes(args, app)) for app in targets(args)]
        from azure.identity import AzureCliCredential

        with AzureCliCredential(tenant_id=TENANT, process_timeout=120) as credential, httpx.Client(timeout=60) as http:
            def token() -> str:
                return credential.get_token("https://management.azure.com/.default").token
            results = [configure_app(app, changes, http, token, apply=args.apply, rollback_dir=args.rollback_dir,
                                     secret_value=os.environ.get(args.mcp_key_env), restoring=bool(args.revert_from))
                       for app, changes in plans]
    except (ConfigError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
