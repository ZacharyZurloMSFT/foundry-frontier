"""Plan, apply and verify the AI Gateway tool servers owned by this repository.

The desired state lives in infra/live/ai-gateway.json. `plan` reads the live gateway and emits a
deterministic, secret-free JSON plan. `apply` re-reads the gateway, refuses when the diff no longer
matches the reviewed plan, and only creates or updates the managed tool servers named in the manifest.
`verify` lists the tools each managed server publishes through the gateway. Models and every other tool
server are checked or reported, never modified, and this script has no delete operation.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _load_common():
    module = sys.modules.get("ai_gateway_common")
    if module is None:
        spec = importlib.util.spec_from_file_location(
            "ai_gateway_common", Path(__file__).resolve().with_name("ai_gateway_common.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["ai_gateway_common"] = module
        spec.loader.exec_module(module)
    return module


common = _load_common()

DEFAULT_MANIFEST = common.ROOT / "infra" / "live" / "ai-gateway.json"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_ACCEPT = "application/json, text/event-stream"
MCP_PAGE_LIMIT = 10
PLAN_KIND = "ai-gateway-release-plan"
RECEIPT_KIND = "ai-gateway-release-receipt"
HASHED_KEYS = ("schemaVersion", "kind", "gateway", "manifestSha256", "refreshSecrets", "actions")
TOP_LEVEL_KEYS = {"schemaVersion", "description", "gateway", "modelProvider", "expectedModels", "toolServers",
                  "observed", "stale", "deferred"}
SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
NAMESPACE = re.compile(r"[a-z][a-z0-9]{0,31}")
TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
HEADER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
SECRET_VALUE = re.compile(r"\$\{[A-Z][A-Z0-9_]{0,63}\}")
ENDPOINT_NAMES = ("chat/completions", "responses", "embeddings")
FAILURE_MODES = ("failClosed", "failOpen")
TRANSPORTS = ("streamableHttp", "sse")
SERVER_KEYS = {"displayName", "description", "failureMode", "endpoints", "expectedTools"}
ENDPOINT_KEYS = {"kind", "namespace", "required", "mcp", "credentials"}
NAMED_SECTIONS = {
    "observed": ({"reason", "concern", "forbiddenToolPatterns"}, {"reason"}),
    "stale": ({"reason", "retiredHostFragment", "decision"}, {"reason", "retiredHostFragment", "decision"}),
    "deferred": ({"reason", "backend", "revisit"}, {"reason"}),
}

EPILOG = """\
required access (no step needs Owner or Microsoft.Authorization/roleAssignments/write):
  plan    Reader on the gateway.
  apply   API Management Service Contributor on the gateway
          (Microsoft.ApiManagement/service/workspaces/toolServers/write). Secret header values written as
          environment placeholders in the manifest are read from the environment of this process at apply
          time only; they are never printed, logged, put in argv, or stored in the plan or the receipt.
  verify  AI Gateway Tools User (dce03f8d-857f-4e40-a90d-67770fa6ecd2) on the gateway. Only the MCP
          initialize and tools/list requests are sent: no tool is invoked and no model is called.
Sign in with `az login` first. The Azure CLI acquires every token; this script never sees one.

typical flow:
  python scripts/ai-gateway-release.py plan --out plan.json
  (set SEARCH_QUERY_KEY and INSURANCE_AGENTS_KEY in the environment of the next command only)
  python scripts/ai-gateway-release.py apply --plan plan.json --yes
  python scripts/ai-gateway-release.py verify
"""


class ManifestError(common.GatewayError):
    """The manifest is invalid or outside the approved boundary."""


class McpError(common.GatewayError):
    """The gateway answered an MCP request with an error or an unusable payload."""


def _require(condition, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _text(value, where: str) -> str:
    _require(isinstance(value, str) and value.strip(), f"{where} must be a non-empty string.")
    return value


def _https(value, where: str, *, host_only: bool = False) -> None:
    _text(value, where)
    parts = urlsplit(value)
    _require(parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
             and not parts.fragment, f"{where} must be an https URL without credentials or a fragment.")
    _require(not host_only or (parts.path in ("", "/") and not parts.query), f"{where} must be a bare origin.")


def _check_gateway(gateway) -> None:
    expected = {
        "subscriptionId": common.SUBSCRIPTION, "resourceGroup": common.RESOURCE_GROUP, "name": common.GATEWAY_NAME,
        "url": common.GATEWAY_URL, "workspace": common.WORKSPACE, "armApiVersion": common.ARM_API_VERSION,
        "tokenScope": common.TOKEN_SCOPE,
    }
    _require(gateway == expected, "Manifest gateway coordinates differ from the approved AI Gateway; "
                                  "review the release boundary before changing them.")


def _check_provider(provider) -> None:
    _require(isinstance(provider, dict) and set(provider) == {"name", "kind"}
             and provider["name"] == common.MODEL_PROVIDER and provider["kind"] == "Foundry",
             "modelProvider must be the approved Foundry provider.")


def _check_models(models) -> None:
    _require(isinstance(models, dict) and models, "expectedModels must be a non-empty object.")
    for registration, entry in models.items():
        where = f"expectedModels.{registration}"
        _require(isinstance(entry, dict) and set(entry) == {"modelName", "endpoints"},
                 f"{where} must define exactly modelName and endpoints.")
        _text(entry["modelName"], f"{where}.modelName")
        _require(common.registration_name(entry["modelName"]) == registration,
                 f"{where}: the registration name must follow the alias rule for {entry['modelName']}.")
        endpoints = entry["endpoints"]
        _require(isinstance(endpoints, list) and endpoints and len(set(endpoints)) == len(endpoints)
                 and all(item in ENDPOINT_NAMES for item in endpoints),
                 f"{where}.endpoints must be a unique, non-empty subset of {', '.join(ENDPOINT_NAMES)}.")


def _check_credentials(credentials, where: str) -> None:
    _require(isinstance(credentials, dict) and credentials.get("type") in ("none", "header"),
             f"{where}.type must be none or header; managedIdentity and oauth2 are not managed by this unit.")
    if credentials["type"] == "none":
        _require(set(credentials) == {"type"}, f"{where} must not define headers with type none.")
        return
    _require(set(credentials) == {"type", "headers"} and isinstance(credentials["headers"], dict)
             and credentials["headers"], f"{where} must define headers for type header.")
    for name, values in credentials["headers"].items():
        _require(HEADER_NAME.fullmatch(name), f"{where}.headers has an invalid header name.")
        _require(isinstance(values, list) and len(values) == 1 and isinstance(values[0], str),
                 f"{where}.headers.{name} must be a list with one value.")


def _check_endpoint(endpoint, where: str) -> str:
    _require(isinstance(endpoint, dict) and set(endpoint) == ENDPOINT_KEYS,
             f"{where} must define exactly {', '.join(sorted(ENDPOINT_KEYS))}.")
    _require(endpoint["kind"] == "mcp", f"{where}.kind must be mcp; other source kinds are not managed.")
    _require(isinstance(endpoint["namespace"], str) and NAMESPACE.fullmatch(endpoint["namespace"]),
             f"{where}.namespace must be lowercase letters and digits.")
    _require(isinstance(endpoint["required"], bool), f"{where}.required must be true or false.")
    mcp = endpoint["mcp"]
    _require(isinstance(mcp, dict) and set(mcp) == {"transport", "url"} and mcp["transport"] in TRANSPORTS,
             f"{where}.mcp must define a transport ({', '.join(TRANSPORTS)}) and a url.")
    _https(mcp["url"], f"{where}.mcp.url")
    _check_credentials(endpoint["credentials"], f"{where}.credentials")
    return endpoint["namespace"]


def _check_tool_servers(servers) -> None:
    _require(isinstance(servers, dict) and servers, "toolServers must be a non-empty object.")
    for name, entry in servers.items():
        _require(SERVER_NAME.fullmatch(name), f"Tool server name {name!r} must be lowercase letters, digits and dashes.")
        where = f"toolServers.{name}"
        _require(isinstance(entry, dict) and set(entry) == SERVER_KEYS,
                 f"{where} must define exactly {', '.join(sorted(SERVER_KEYS))}.")
        _text(entry["displayName"], f"{where}.displayName")
        _text(entry["description"], f"{where}.description")
        _require(entry["failureMode"] in FAILURE_MODES, f"{where}.failureMode must be one of {', '.join(FAILURE_MODES)}.")
        endpoints = entry["endpoints"]
        _require(isinstance(endpoints, list) and endpoints, f"{where}.endpoints must be a non-empty list.")
        namespaces = [_check_endpoint(endpoint, f"{where}.endpoints[{index}]") for index, endpoint in enumerate(endpoints)]
        _require(len(set(namespaces)) == len(namespaces), f"{where}.endpoints repeats a namespace.")
        tools = entry["expectedTools"]
        _require(isinstance(tools, list) and tools and len(set(tools)) == len(tools)
                 and all(isinstance(tool, str) and TOOL_NAME.fullmatch(tool) for tool in tools),
                 f"{where}.expectedTools must be a unique, non-empty list of tool names.")
        for tool in tools:
            _require(any(tool.startswith(f"{namespace}_") and len(tool) > len(namespace) + 1 for namespace in namespaces),
                     f"{where}.expectedTools: {tool} must be <namespace>_<tool> for one of {', '.join(namespaces)}.")


def _check_named(manifest: dict, section: str) -> set[str]:
    allowed, required = NAMED_SECTIONS[section]
    entries = manifest.get(section)
    _require(isinstance(entries, dict), f"{section} must be an object.")
    for name, entry in entries.items():
        where = f"{section}.{name}"
        _require(SERVER_NAME.fullmatch(name), f"{where}: invalid tool server name.")
        _require(isinstance(entry, dict) and set(entry) <= allowed and required <= set(entry),
                 f"{where} must define {', '.join(sorted(required))} and may add only {', '.join(sorted(allowed - required))}.")
        for key, value in entry.items():
            if key == "forbiddenToolPatterns":
                _require(isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value),
                         f"{where}.forbiddenToolPatterns must be a non-empty list of words.")
            elif key == "backend":
                _https(value, f"{where}.backend")
            else:
                _text(value, f"{where}.{key}")
    return set(entries)


def _walk(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, path + (index,))
    elif isinstance(value, str):
        yield path, value


def _is_secret_slot(path: tuple) -> bool:
    return len(path) >= 7 and path[0] == "toolServers" and path[2] == "endpoints" and path[4:6] == ("credentials", "headers")


def _check_secret_placement(manifest: dict) -> None:
    for path, value in _walk(manifest):
        where = "/".join(str(part) for part in path)
        if _is_secret_slot(path):
            _require(SECRET_VALUE.fullmatch(value),
                     f"{where} must be exactly one environment placeholder; secrets are never stored in the manifest.")
        else:
            _require("${" not in value, f"{where}: environment placeholders are only allowed in credential header values.")


def validate_manifest(manifest) -> dict:
    _require(isinstance(manifest, dict), "Manifest must be a JSON object.")
    _require(manifest.get("schemaVersion") == 1, "Manifest schemaVersion must be 1.")
    unknown = set(manifest) - TOP_LEVEL_KEYS
    _require(not unknown, f"Unknown manifest sections: {', '.join(sorted(unknown))}.")
    _check_gateway(manifest.get("gateway"))
    _check_provider(manifest.get("modelProvider"))
    _check_models(manifest.get("expectedModels"))
    _check_tool_servers(manifest.get("toolServers"))
    groups = {"toolServers": set(manifest["toolServers"])}
    for section in NAMED_SECTIONS:
        groups[section] = _check_named(manifest, section)
    seen: set[str] = set()
    for section, names in groups.items():
        _require(not (seen & names), f"A tool server appears in more than one manifest section ({section}).")
        seen |= names
    _check_secret_placement(manifest)
    return manifest


def load_manifest(path) -> dict:
    return validate_manifest(common.read_json(path))


def _workspace_url(manifest: dict, collection: str) -> str:
    gateway = manifest["gateway"]
    return (f"{common.ARM_ENDPOINT}/subscriptions/{gateway['subscriptionId']}/resourceGroups/{gateway['resourceGroup']}"
            f"/providers/Microsoft.ApiManagement/service/{gateway['name']}/workspaces/{gateway['workspace']}/{collection}")


def read_live(runner, manifest: dict) -> tuple[dict, list]:
    params = {"api-version": manifest["gateway"]["armApiVersion"]}
    servers = common.list_arm(runner, _workspace_url(manifest, "toolServers"), params)
    models = common.list_arm(runner, _workspace_url(manifest, "models"), params)
    return {str(item["name"]).lower(): item for item in servers if item.get("name")}, models


def desired_properties(entry: dict, environ=None) -> dict:
    """ARM properties for a manifest entry; header values stay placeholders unless an environment is given."""
    endpoints = []
    for endpoint in entry["endpoints"]:
        credentials = {"type": endpoint["credentials"]["type"]}
        if credentials["type"] == "header":
            credentials["headers"] = {
                name: [value if environ is None else common.resolve_placeholders(value, environ) for value in values]
                for name, values in endpoint["credentials"]["headers"].items()
            }
        endpoints.append({
            "kind": "mcp", "namespace": endpoint["namespace"], "required": endpoint["required"],
            "mcp": {"transport": endpoint["mcp"]["transport"], "url": endpoint["mcp"]["url"]},
            "credentials": credentials,
        })
    return {"type": "mcp", "displayName": entry["displayName"], "description": entry["description"],
            "failureMode": entry["failureMode"], "endpoints": endpoints}


def comparable(properties: dict) -> dict:
    """The fields that decide drift. Header values are never part of it: GET redacts secrets."""
    endpoints = []
    for endpoint in properties.get("endpoints") or []:
        credentials = endpoint.get("credentials") or {}
        headers = credentials.get("headers")
        mcp = endpoint.get("mcp") or {}
        endpoints.append({
            "namespace": endpoint.get("namespace"), "kind": endpoint.get("kind"),
            "required": endpoint.get("required", True), "url": mcp.get("url"),
            "transport": mcp.get("transport") or "streamableHttp", "credentialType": credentials.get("type"),
            "headerNames": sorted(str(name).lower() for name in headers) if isinstance(headers, dict) else None,
        })
    return {"type": properties.get("type"), "displayName": properties.get("displayName"),
            "description": properties.get("description"), "failureMode": properties.get("failureMode"),
            "endpoints": endpoints}


def _endpoint_differences(want: dict, have: dict) -> list[str]:
    label = f"endpoints[{want['namespace']}]"
    found = [f"{label}.{field}" for field in ("namespace", "kind", "required", "url", "transport", "credentialType")
             if want[field] != have[field]]
    if have["headerNames"] is not None and want["headerNames"] is not None and want["headerNames"] != have["headerNames"]:
        found.append(f"{label}.headerNames")
    return found


def diff_views(desired: dict, live: dict) -> tuple[list[str], list[str]]:
    """Drift between desired and live. Endpoints pair by namespace, then by position (a renamed namespace)."""
    differences = [key for key in ("type", "displayName", "description") if desired[key] != live[key]]
    # The 2025-09-01-preview service accepts failureMode but never returns it (verified with a failOpen canary),
    # so an absent live value is not drift; a returned value that differs still is.
    if live["failureMode"] is not None and live["failureMode"] != desired["failureMode"]:
        differences.append("failureMode")
    unpaired, remaining = [], list(live["endpoints"])
    for want in desired["endpoints"]:
        match = next((have for have in remaining if have["namespace"] == want["namespace"]), None)
        if match is None:
            unpaired.append(want)
            continue
        remaining.remove(match)
        differences += _endpoint_differences(want, match)
    for want in unpaired:
        if remaining:
            differences += _endpoint_differences(want, remaining.pop(0))
        else:
            differences.append(f"endpoints[{want['namespace']}]")
    conflicts = [f"endpoints[{have['namespace']}] exists on the live server but not in the manifest" for have in remaining]
    return sorted(differences), sorted(conflicts)


def _required_environment(entry: dict) -> list[str]:
    return sorted({name for endpoint in entry["endpoints"]
                   for values in (endpoint["credentials"].get("headers") or {}).values()
                   for value in values for name in common.placeholder_names(value)})


def _backends(item: dict) -> list[dict]:
    rows = []
    for endpoint in (item.get("properties") or {}).get("endpoints") or []:
        url = (endpoint.get("mcp") or {}).get("url")
        backend = None
        if isinstance(url, str):
            parts = urlsplit(url)
            backend = f"{parts.scheme}://{parts.hostname}{parts.path}"
        rows.append({"namespace": endpoint.get("namespace"), "kind": endpoint.get("kind"), "backend": backend})
    return rows


def _endpoint_token(value) -> str:
    text = str(value).strip().lower()
    if "/v1/" in text:
        text = text.split("/v1/", 1)[1]
    return re.sub(r"[^a-z0-9]", "", text)


def model_findings(manifest: dict, live_models: list) -> dict:
    live = {item.get("name"): item for item in live_models}
    provider = manifest["modelProvider"]["name"]
    problems = []
    for registration in sorted(manifest["expectedModels"]):
        want = manifest["expectedModels"][registration]
        item = live.get(registration)
        if item is None:
            problems.append({"registration": registration, "problems": ["registration is missing"]})
            continue
        properties = item.get("properties") or {}
        found = []
        model_name = (properties.get("deployment") or {}).get("modelName")
        if model_name != want["modelName"]:
            found.append(f"model name is {model_name!r}, expected {want['modelName']!r}")
        have = {_endpoint_token(value) for value in properties.get("supportedEndpoints") or []}
        expected = {_endpoint_token(value) for value in want["endpoints"]}
        found += [f"endpoint {name} is missing" for name in sorted(expected - have)]
        found += [f"endpoint {name} is not expected" for name in sorted(have - expected)]
        match = re.search(r"/modelproviders/([^/]+)/", str(item.get("id", "")).lower())
        if match and match.group(1) != provider.lower():
            found.append(f"registered under provider {match.group(1)!r}, expected {provider!r}")
        if found:
            problems.append({"registration": registration, "problems": found})
    unexpected = sorted(str(name) for name in live if name not in manifest["expectedModels"])
    return {"checked": len(manifest["expectedModels"]), "problems": problems, "unexpected": unexpected}


def _report_sections(manifest: dict, live_servers: dict) -> dict:
    observed = [{"name": name, "present": name in live_servers, "concern": entry.get("concern"),
                 "endpoints": _backends(live_servers[name]) if name in live_servers else []}
                for name, entry in sorted(manifest["observed"].items())]
    stale = []
    for name, entry in sorted(manifest["stale"].items()):
        endpoints = _backends(live_servers[name]) if name in live_servers else []
        fragment = entry["retiredHostFragment"].lower()
        if name not in live_servers:
            status = "absent"
        elif any(fragment in (row["backend"] or "").lower() for row in endpoints):
            status = "needs-human-decision"
        else:
            status = "backend-changed-review-manifest"
        stale.append({"name": name, "status": status, "decision": entry["decision"], "endpoints": endpoints})
    deferred = [{"name": name, "status": "present-unexpectedly" if name in live_servers else "deferred",
                 "reason": entry["reason"]} for name, entry in sorted(manifest["deferred"].items())]
    listed = set(manifest["toolServers"]) | set(manifest["observed"]) | set(manifest["stale"]) | set(manifest["deferred"])
    return {"observed": observed, "stale": stale, "deferred": deferred,
            "unlisted": sorted(name for name in live_servers if name not in listed)}


def validate_refresh(manifest: dict, names) -> list[str]:
    selected = sorted(set(names))
    for name in selected:
        entry = manifest["toolServers"].get(name)
        _require(entry is not None and any(endpoint["credentials"]["type"] == "header" for endpoint in entry["endpoints"]),
                 f"--refresh-secrets {name}: not a managed tool server with header credentials.")
    return selected


def plan_from_live(manifest: dict, live_servers: dict, live_models: list, *, refresh=()) -> dict:
    refresh = validate_refresh(manifest, refresh)
    actions = []
    for name in sorted(manifest["toolServers"]):
        entry = manifest["toolServers"][name]
        desired = desired_properties(entry)
        environment = _required_environment(entry)
        live = live_servers.get(name)
        if live is None:
            actions.append({"name": name, "action": "create", "differences": ["new tool server"],
                            "conflicts": [], "requiredEnvironment": environment, "desired": desired})
            continue
        differences, conflicts = diff_views(comparable(desired), comparable(live.get("properties") or {}))
        if name in refresh and not conflicts:
            differences = sorted({*differences, "credentials.headers (refresh requested)"})
        if conflicts:
            actions.append({"name": name, "action": "conflict", "differences": differences,
                            "conflicts": conflicts, "requiredEnvironment": [], "desired": desired})
        elif differences:
            actions.append({"name": name, "action": "update", "differences": differences,
                            "conflicts": [], "requiredEnvironment": environment, "desired": desired})
        else:
            actions.append({"name": name, "action": "noop", "differences": [],
                            "conflicts": [], "requiredEnvironment": []})
    gateway = manifest["gateway"]
    plan = {
        "schemaVersion": 1, "kind": PLAN_KIND,
        "gateway": {key: gateway[key] for key in ("subscriptionId", "resourceGroup", "name", "workspace", "armApiVersion")},
        "manifestSha256": common.sha256_json(manifest), "refreshSecrets": refresh, "actions": actions,
    }
    plan["planSha256"] = common.sha256_json({key: plan[key] for key in HASHED_KEYS})
    reports = _report_sections(manifest, live_servers)
    models = model_findings(manifest, live_models)
    counts = {kind: sum(1 for action in actions if action["action"] == kind) for kind in ("create", "update", "noop", "conflict")}
    plan.update(reports)
    plan["models"] = models
    plan["summary"] = {
        **counts, "modelProblems": len(models["problems"]),
        "staleNeedingDecision": sum(1 for row in reports["stale"] if row["status"] == "needs-human-decision"),
        "governanceConcerns": sum(1 for row in reports["observed"] if row["present"] and row["concern"]),
        "deferredButPresent": sum(1 for row in reports["deferred"] if row["status"] == "present-unexpectedly"),
    }
    return plan


def build_plan(runner, manifest: dict, *, refresh=()) -> tuple[dict, dict]:
    live_servers, live_models = read_live(runner, manifest)
    return plan_from_live(manifest, live_servers, live_models, refresh=refresh), live_servers


def check_plan_document(document) -> None:
    ok = isinstance(document, dict) and document.get("kind") == PLAN_KIND and document.get("schemaVersion") == 1
    if not ok or common.sha256_json({key: document.get(key) for key in HASHED_KEYS}) != document.get("planSha256"):
        raise common.GatewayError("The plan file is not a valid, unmodified release plan.")


def put_body(manifest: dict, name: str, environ, live: dict | None) -> dict:
    properties = desired_properties(manifest["toolServers"][name], environ)
    live_policies = ((live or {}).get("properties") or {}).get("policies")
    if live_policies:
        properties["policies"] = live_policies
    return {"properties": properties}


def _scrub(message: str, secrets: list[str]) -> str:
    for secret in secrets:
        message = message.replace(secret, "***")
    return message


def apply_plan(runner, manifest: dict, document: dict, environ, *, now=None) -> dict:
    check_plan_document(document)
    now = now or common.utc_now
    current, live = build_plan(runner, manifest, refresh=document.get("refreshSecrets") or [])
    if current["planSha256"] != document["planSha256"]:
        raise common.GatewayError(
            "The live gateway or the manifest no longer matches the reviewed plan; generate and review a new plan.")
    blocked = [action["name"] for action in current["actions"] if action["action"] == "conflict"]
    if blocked:
        raise common.GatewayError("Refusing to apply while live tool server(s) have endpoints outside the manifest: "
                                  + ", ".join(blocked))
    pending = [action for action in current["actions"] if action["action"] in ("create", "update")]
    bodies = {action["name"]: put_body(manifest, action["name"], environ, live.get(action["name"])) for action in pending}
    secrets = sorted({value for body in bodies.values() for endpoint in body["properties"]["endpoints"]
                      for values in endpoint["credentials"].get("headers", {}).values() for value in values if value})
    results, failed = [], False
    for action in pending:
        name = action["name"]
        item = live.get(name) or {}
        headers = {"If-None-Match": "*"} if action["action"] == "create" else {"If-Match": item.get("etag") or "*"}
        result = {"name": name, "action": action["action"], "differences": action["differences"]}
        if action["action"] == "update":
            result["before"] = comparable(item.get("properties") or {})
        try:
            runner("PUT", f"{_workspace_url(manifest, 'toolServers')}/{name}",
                   params={"api-version": manifest["gateway"]["armApiVersion"]}, headers=headers, body=bodies[name])
        except common.GatewayError as error:
            results.append({**result, "outcome": "failed", "error": _scrub(str(error), secrets)})
            failed = True
            break
        results.append({**result, "outcome": "applied"})
    if pending and not failed:
        after, _ = build_plan(runner, manifest, refresh=())
        remaining = {action["name"]: action["differences"] for action in after["actions"]}
        for result in results:
            result["converged"] = not remaining.get(result["name"])
            result["remainingDifferences"] = remaining.get(result["name"], [])
    return {"schemaVersion": 1, "kind": RECEIPT_KIND, "planSha256": document["planSha256"],
            "appliedAt": common.iso(now()), "complete": not failed, "results": results}


def sse_messages(text: str) -> list:
    """JSON payloads of the `data:` fields in a server-sent-events body."""
    messages, data = [], []
    for raw in [*text.splitlines(), ""]:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                payload, data = "\n".join(data), []
                try:
                    messages.append(json.loads(payload))
                except ValueError:
                    pass
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value.removeprefix(" "))
    return messages


def rpc_message(response, request_id: int) -> dict:
    payload = response.body
    if isinstance(payload, str):
        payload = None
    text = (response.text or (response.body if isinstance(response.body, str) else "")).strip()
    if payload is None:
        if not text:
            raise McpError("The gateway returned an empty MCP response.")
        if text[0] in "{[":
            try:
                payload = json.loads(text)
            except ValueError:
                raise McpError("The gateway returned an invalid MCP JSON response.") from None
        else:
            payload = sse_messages(text)
    for message in payload if isinstance(payload, list) else [payload]:
        if (isinstance(message, dict) and str(message.get("id")) == str(request_id)
                and ("result" in message or "error" in message)):
            return message
    raise McpError("The MCP response did not contain a result for the request.")


def _rpc(runner, url: str, request_id: int, method: str, params: dict) -> dict:
    response = runner(
        "POST", url, scope=common.TOKEN_SCOPE,
        headers={"Accept": MCP_ACCEPT, "Content-Type": "application/json", "MCP-Protocol-Version": MCP_PROTOCOL_VERSION},
        body={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    message = rpc_message(response, request_id)
    if "error" in message:
        error = message["error"] if isinstance(message["error"], dict) else {}
        raise McpError(f"{method} returned a JSON-RPC error ({error.get('code')}): {str(error.get('message', ''))[:160]}")
    return message["result"] if isinstance(message.get("result"), dict) else {}


def list_tools(runner, url: str) -> tuple[dict, list[str]]:
    # The gateway is stateless (no Mcp-Session-Id), so no initialized notification or session is needed.
    initialized = _rpc(runner, url, 1, "initialize", {
        "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "ai-gateway-release-verify", "version": "1"},
    })
    names, cursor = [], None
    for page in range(MCP_PAGE_LIMIT):
        result = _rpc(runner, url, 2 + page, "tools/list", {"cursor": cursor} if cursor else {})
        names += [tool["name"] for tool in result.get("tools") or [] if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
        cursor = result.get("nextCursor")
        if not cursor:
            return initialized, sorted(set(names))
    raise McpError("tools/list exceeded the page limit.")


def verify_servers(runner, manifest: dict, *, include_observed: bool = False, strict: bool = False) -> dict:
    gateway_url = manifest["gateway"]["url"]
    servers = []
    for name in sorted(manifest["toolServers"]):
        expected = manifest["toolServers"][name]["expectedTools"]
        url = common.toolserver_url(name, gateway_url)
        report = {"name": name, "url": url, "ok": False, "tools": [], "missingTools": [], "unexpectedTools": []}
        try:
            initialized, tools = list_tools(runner, url)
        except common.GatewayError as error:
            report["error"] = str(error)
        else:
            report.update(
                protocolVersion=initialized.get("protocolVersion"),
                serverName=(initialized.get("serverInfo") or {}).get("name"), tools=tools,
                missingTools=sorted(set(expected) - set(tools)), unexpectedTools=sorted(set(tools) - set(expected)))
            report["ok"] = not report["missingTools"] and not (strict and report["unexpectedTools"])
        servers.append(report)
    result = {"ok": all(report["ok"] for report in servers), "servers": servers}
    if include_observed:
        observed = []
        for name, entry in sorted(manifest["observed"].items()):
            report = {"name": name, "reachable": False, "toolCount": 0, "governanceFindings": []}
            try:
                _, tools = list_tools(runner, common.toolserver_url(name, gateway_url))
            except common.GatewayError as error:
                report["error"] = str(error)
            else:
                patterns = [pattern.lower() for pattern in entry.get("forbiddenToolPatterns", [])]
                report.update(reachable=True, toolCount=len(tools),
                              governanceFindings=[tool for tool in tools if any(pattern in tool.lower() for pattern in patterns)])
            observed.append(report)
        result["observed"] = observed
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-gateway-release.py", description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Read the live gateway and emit a deterministic plan (read-only).")
    plan.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    plan.add_argument("--out", type=Path, help="Also write the plan JSON to this file.")
    plan.add_argument("--refresh-secrets", nargs="+", default=[], metavar="NAME",
                      help="Force an update that re-sends the secret headers of these managed tool servers "
                           "(for example after rotating a key); secrets alone never schedule an update.")
    apply = commands.add_parser("apply", help="Create or update the managed tool servers of a reviewed plan.")
    apply.add_argument("--plan", type=Path, required=True, help="Plan file produced by `plan`.")
    apply.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    apply.add_argument("--yes", action="store_true", help="Required to change the gateway.")
    apply.add_argument("--receipt", type=Path, help="Also write the redacted receipt here (never overwritten).")
    verify = commands.add_parser("verify", help="List the tools each managed server publishes through the gateway.")
    verify.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    verify.add_argument("--include-observed", action="store_true",
                        help="Also list observed servers and flag forbidden tool names (read-only).")
    verify.add_argument("--strict", action="store_true", help="Also fail when a managed server publishes unexpected tools.")
    return parser


def main(argv=None, *, runner=None, environ=None, now=None) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        if args.command == "apply" and not args.yes:
            raise common.GatewayError("Refusing to change the gateway without --yes; review the plan first.")
        if args.command == "apply" and args.receipt and args.receipt.exists():
            raise common.GatewayError(f"Refusing to overwrite an existing receipt: {args.receipt}")
        manifest = load_manifest(args.manifest)
        runner = runner or common.AzRestRunner()
        if args.command == "plan":
            result, _ = build_plan(runner, manifest, refresh=args.refresh_secrets)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                common.replace_json(args.out, result)
        elif args.command == "apply":
            result = apply_plan(runner, manifest, common.read_json(args.plan), environ, now=now)
            if args.receipt:
                common.write_json_exclusive(args.receipt, result)
        else:
            result = verify_servers(runner, manifest, include_observed=args.include_observed, strict=args.strict)
        print(common.pretty_json(result), end="")
        if args.command == "apply":
            if not result["complete"]:
                print("AI Gateway release stopped after a failed write; see the receipt. "
                      "Nothing was retried or rolled back.", file=sys.stderr)
            return 0 if result["complete"] else 1
        return 0 if args.command == "plan" or result["ok"] else 1
    except (common.GatewayError, OSError, ValueError) as error:
        print(f"AI Gateway release refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
