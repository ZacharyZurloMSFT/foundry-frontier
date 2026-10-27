"""Shared constants and helpers for the AI Gateway tier rollout scripts.

Values here were observed on the live ``ai-gateway-northwind`` gateway (SKU
``AIGateway``, East US 2). Model calls use ``<provider>/<registration>``
aliases; MCP tools are exposed as ``<namespace>_<tool>``.

The control-plane scripts (release, grants, enforce) reach Azure and the gateway only through one
injectable runner: ``runner(method, url, *, params=None, headers=None, body=None, scope=None)``
returning a ``Response`` and raising ``RunnerError`` on any failure. ``AzRestRunner`` is the real
implementation: it shells out to ``az rest`` so tokens stay inside the Azure CLI, and request bodies
(which may hold secrets) travel through a short-lived temp file, never argv. Tests pass a fake.
"""
from __future__ import annotations

import hashlib
import http
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
RESOURCE_GROUP = "rg-northwind-ai"
GATEWAY_NAME = "ai-gateway-northwind"
GATEWAY_URL = "https://ai-gateway-northwind.azure-api.net"
MODEL_PROVIDER = "aisvc-zurlo-northwind"
WORKSPACE = "default"
ARM_API_VERSION = "2025-09-01-preview"
TOKEN_SCOPE = "https://azure-api.net/aigateway/.default"
MODELS_BASE_PATH = f"/{WORKSPACE}/models/openai/v1/"
TOOLSERVER_PATH = f"/{WORKSPACE}/toolservers/{{name}}/mcp"

ROLE_MODELS_USER = "a6b68e3d-a1f0-4c60-8678-237c050cf4ea"
ROLE_TOOLS_USER = "dce03f8d-857f-4e40-a90d-67770fa6ecd2"
ROLE_GATEWAY_USER = "caad7a21-134e-43f4-a550-c5ffc17e051c"
ROLE_FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
ROLE_OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"

ARM_ENDPOINT = "https://management.azure.com"
ROLE_ASSIGNMENT_API_VERSION = "2022-04-01"
FOUNDRY_ACCOUNT_NAME = "aisvc-zurlo-northwind"
GATEWAY_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
    f"/providers/Microsoft.ApiManagement/service/{GATEWAY_NAME}"
)
FOUNDRY_ACCOUNT_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
    f"/providers/Microsoft.CognitiveServices/accounts/{FOUNDRY_ACCOUNT_NAME}"
)
APP_UAMI_PRINCIPAL_ID = "4345f6c9-3c1a-4f4f-9d58-e75d7fa6ee17"
GATEWAY_IDENTITY_PRINCIPAL_ID = "47816336-79ee-4e60-9ab4-7c03e5e4d2b0"
GATEWAY_ROLES = {"models": ROLE_MODELS_USER, "tools": ROLE_TOOLS_USER}
ROLE_NAMES = {
    ROLE_MODELS_USER: "AI Gateway Models User",
    ROLE_TOOLS_USER: "AI Gateway Tools User",
    ROLE_GATEWAY_USER: "AI Gateway User",
    ROLE_FOUNDRY_USER: "Foundry User",
    ROLE_OPENAI_USER: "Cognitive Services OpenAI User",
}
ROOT = Path(__file__).resolve().parents[1]


def registration_name(deployment: str) -> str:
    """Gateway registration name for a Foundry deployment (dots and other characters become dashes)."""
    name = re.sub(r"[^a-z0-9-]", "-", deployment.strip().lower())
    if not name or name.strip("-") == "":
        raise ValueError("deployment name is required")
    return name


def model_alias(deployment: str, provider: str = MODEL_PROVIDER) -> str:
    """The ``model`` value the gateway routes on: ``<provider>/<registration>``."""
    if not provider.strip():
        raise ValueError("provider is required")
    return f"{provider.strip()}/{registration_name(deployment)}"


def models_base_url(gateway_url: str = GATEWAY_URL) -> str:
    return gateway_url.rstrip("/") + MODELS_BASE_PATH


def toolserver_url(name: str, gateway_url: str = GATEWAY_URL) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        raise ValueError("tool server names are lowercase letters, digits and dashes")
    return gateway_url.rstrip("/") + TOOLSERVER_PATH.format(name=name)


def prefixed_tool(namespace: str, tool: str) -> str:
    """Tool name as exposed by the gateway for a backend registered under ``namespace``."""
    return f"{namespace}_{tool}"


class GatewayError(RuntimeError):
    """A failure whose message is safe to show: it never carries secrets, tokens or response bodies."""


class RunnerError(GatewayError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class Response:
    """One runner result. The repr omits the payload so it cannot leak into logs or test output."""

    __slots__ = ("body", "etag", "status", "text")

    def __init__(self, status: int = 200, body=None, text: str = "", etag: str | None = None):
        self.status = status
        self.body = body
        self.text = text
        self.etag = etag

    def __repr__(self) -> str:
        return f"Response(status={self.status})"


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def pretty_json(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def is_guid(value) -> bool:
    return isinstance(value, str) and _GUID.fullmatch(value) is not None


def role_definition_id(role: str, subscription: str = SUBSCRIPTION) -> str:
    return f"/subscriptions/{subscription}/providers/Microsoft.Authorization/roleDefinitions/{role}"


def assignment_name(scope: str, principal: str, role: str) -> str:
    """Deterministic role assignment name; the same scheme as ``grant()`` in deploy-hosted-agents.py."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.lower()}|{principal}|{role}"))


def scope_covers(parent: str, child: str) -> bool:
    """True when an assignment at ``parent`` applies at ``child`` (same scope or an ancestor)."""
    parent, child = parent.rstrip("/").lower(), child.rstrip("/").lower()
    return child == parent or child.startswith(parent + "/")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_receipt_path(kind: str, moment: datetime) -> Path:
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / ".release-state" / f"ai-gateway-{kind}-{stamp}.json"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GatewayError(f"File not found: {path}") from None
    except ValueError:
        raise GatewayError(f"File is not valid JSON: {path}") from None


def write_json_exclusive(path, payload) -> None:
    """Create a new 0600 JSON file; an existing file is never overwritten."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise GatewayError(f"Refusing to overwrite an existing file: {target}") from None
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(pretty_json(payload))


def replace_json(path, payload) -> None:
    """Atomically replace a JSON file, keeping owner-only permissions on the staging file."""
    target = Path(path)
    staging = target.with_name(target.name + ".tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(pretty_json(payload))
    os.replace(staging, target)


PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]{0,63})\}")


def placeholder_names(value: str) -> list[str]:
    return PLACEHOLDER.findall(value)


def resolve_placeholders(value: str, environ) -> str:
    """Substitute ``${NAME}`` from the environment; an unset or empty variable is a clear error."""
    missing = sorted({name for name in placeholder_names(value) if not environ.get(name)})
    if missing:
        raise GatewayError("Required environment variable(s) not set: " + ", ".join(missing))
    return PLACEHOLDER.sub(lambda match: environ[match.group(1)], value)


_UNSAFE_ARGUMENT = re.compile(r'[&|<>^%!"\r\n\x00]')
# az.cmd re-parses its arguments inside parenthesised batch blocks, so even `)` corrupts a request.
_SHIM_UNSAFE_ARGUMENT = re.compile(r'[&|<>^%!"()\r\n\x00]')
_REASON_STATUS = {status.phrase.lower(): int(status) for status in http.HTTPStatus}
_NOT_FOUND_CODES = {"resourcenotfound", "notfound", "parentresourcenotfound", "resourcegroupnotfound"}
_METHODS = {"GET", "PUT", "POST", "PATCH", "DELETE"}


def cli_command(executable: str, *, windows: bool | None = None) -> tuple[list[str], bool]:
    """Command prefix that starts the Azure CLI, and whether it still goes through a cmd.exe shim.

    The Windows `az.cmd` shim only runs `<CLI2>\\python.exe -IBm azure.cli %*`, so when that
    interpreter exists it is launched directly and arguments reach the CLI untouched.
    """
    on_windows = os.name == "nt" if windows is None else windows
    if on_windows and executable.lower().endswith((".cmd", ".bat")):
        cli_python = Path(executable).resolve().parent.parent / "python.exe"
        if cli_python.is_file():
            return [str(cli_python), "-IBm", "azure.cli"], False
        return [executable], True
    return [executable], False


def parse_cli_failure(stderr: str) -> tuple[int | None, str | None, str | None]:
    """Best-effort (HTTP status, error code, service message) from `az rest` stderr."""
    text = stderr or ""
    code_match = re.search(r'"code"\s*:\s*"([A-Za-z0-9_.-]+)"', text)
    code = code_match.group(1) if code_match else None
    status = None
    for reason in re.finditer(r"^\s*(?:ERROR:\s*)?([A-Za-z][A-Za-z ]{2,40}?)\s*(?:\(|$)", text, re.MULTILINE):
        status = _REASON_STATUS.get(reason.group(1).strip().lower())
        if status:
            break
    if status is None and code and code.lower() in _NOT_FOUND_CODES:
        status = 404
    if code is None and "az login" in text.lower():
        code = "AuthenticationRequired"
    message = None
    message_match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if message_match:
        try:
            message = json.loads('"' + message_match.group(1) + '"')
        except ValueError:
            message = None
    return status, code, message


class AzRestRunner:
    """Sends every request through `az rest`, so tokens stay inside the Azure CLI.

    Bodies travel through a short-lived temp file (`--body @file`) that is always removed, never
    through argv. Failure messages omit response content for requests that carried a body.
    """

    def __init__(self, *, az: str | None = None, temp_dir=None, timeout: int = 120, run=None):
        self._az = az
        self._temp_dir = temp_dir
        self._timeout = timeout
        self._run = run or subprocess.run

    def _executable(self) -> str:
        executable = self._az or shutil.which("az")
        if not executable:
            raise GatewayError("Azure CLI (az) is required to reach the gateway control plane.")
        return executable

    def __call__(self, method: str, url: str, *, params=None, headers=None, body=None, scope=None) -> Response:
        method = method.upper()
        if method not in _METHODS:
            raise ValueError("Unsupported HTTP method")
        if not url.startswith("https://"):
            raise GatewayError("Only HTTPS requests are sent.")
        command, via_shim = cli_command(self._executable())
        argv = [*command, "rest", "--method", method.lower(), "--url", url,
                "--output", "json", "--only-show-errors"]
        if params:
            argv += ["--url-parameters", *[f"{key}={value}" for key, value in params.items()]]
        if headers:
            argv += ["--headers", *[f"{key}={value}" for key, value in headers.items()]]
        if scope:
            argv += ["--resource", scope.removesuffix("/.default")]
        body_path = None
        try:
            if body is not None:
                descriptor, body_path = tempfile.mkstemp(prefix="aigw-", suffix=".json", dir=self._temp_dir)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(body, stream)
                argv += ["--body", f"@{body_path}"]
            unsafe = _SHIM_UNSAFE_ARGUMENT if via_shim else _UNSAFE_ARGUMENT
            if any(unsafe.search(argument) for argument in argv):
                raise GatewayError("A request argument has characters that are unsafe for the Azure CLI launcher.")
            try:
                completed = self._run(
                    argv, capture_output=True, encoding="utf-8", errors="replace",
                    timeout=self._timeout, check=False, stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                raise GatewayError(f"Azure request {method} {_target(url)} timed out; do not blindly retry a mutation.") from None
            except OSError:
                raise GatewayError("Azure CLI could not be started.") from None
        finally:
            if body_path:
                try:
                    os.unlink(body_path)
                except FileNotFoundError:
                    pass
        if completed.returncode:
            status, code, message = parse_cli_failure(completed.stderr)
            detail = f"Azure request {method} {_target(url)} failed"
            if status:
                detail += f" (HTTP {status})"
            if code:
                detail += f" [{code}]"
            if message and body is None:
                detail += f": {message[:240]}"
            if code == "AuthenticationRequired":
                detail += ": sign in with `az login` and retry"
            raise RunnerError(detail, status=status, code=code)
        stripped = (completed.stdout or "").strip()
        parsed, text = None, ""
        if stripped:
            try:
                parsed = json.loads(stripped)
            except ValueError:
                text = completed.stdout
        etag = parsed.get("etag") if isinstance(parsed, dict) and isinstance(parsed.get("etag"), str) else None
        return Response(200, parsed, text, etag)


def _target(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def list_arm(runner, url: str, params=None, *, max_pages: int = 20) -> list[dict]:
    """GET an ARM collection, following ``nextLink`` on the same host."""
    items: list[dict] = []
    page_url, page_params = url, dict(params or {})
    host = urlsplit(url).netloc.lower()
    for _ in range(max_pages):
        body = runner("GET", page_url, params=page_params).body
        if not isinstance(body, dict) or not isinstance(body.get("value"), list):
            raise GatewayError(f"Unexpected list response from {_target(url)}.")
        items.extend(item for item in body["value"] if isinstance(item, dict))
        link = body.get("nextLink")
        if not link:
            return items
        parts = urlsplit(link)
        if parts.scheme != "https" or parts.netloc.lower() != host:
            raise GatewayError("Refusing to follow a list continuation to another host.")
        page_url = f"https://{parts.netloc}{parts.path}"
        page_params = dict(parse_qsl(parts.query, keep_blank_values=True))
    raise GatewayError("List response exceeded the page limit.")


_ASSIGNMENT_ID = re.compile(r"(?P<scope>.+)/providers/Microsoft\.Authorization/roleAssignments/[^/]+", re.IGNORECASE)


def role_assignments_url(scope: str) -> str:
    return f"{ARM_ENDPOINT}{scope}/providers/Microsoft.Authorization/roleAssignments"


def normalize_role_assignment(item: dict) -> dict | None:
    properties = item.get("properties") or {}
    name, principal = item.get("name"), properties.get("principalId")
    definition, scope = properties.get("roleDefinitionId"), properties.get("scope")
    if not scope and isinstance(item.get("id"), str):
        match = _ASSIGNMENT_ID.fullmatch(item["id"])
        scope = match.group("scope") if match else None
    if not all(isinstance(value, str) and value for value in (name, principal, definition, scope)):
        return None
    return {
        "name": name, "scope": scope, "principalId": principal.lower(),
        "principalType": properties.get("principalType"),
        "roleDefinitionId": definition, "roleId": definition.rsplit("/", 1)[-1].lower(),
    }


def list_role_assignments(runner, scope: str) -> list[dict]:
    """Assignments at ``scope`` plus those inherited from ancestor scopes."""
    items = list_arm(runner, role_assignments_url(scope),
                     {"api-version": ROLE_ASSIGNMENT_API_VERSION, "$filter": "atScope()"})
    return [row for row in map(normalize_role_assignment, items) if row]


def get_role_assignment(runner, scope: str, name: str) -> dict | None:
    try:
        body = runner("GET", f"{role_assignments_url(scope)}/{name}",
                      params={"api-version": ROLE_ASSIGNMENT_API_VERSION}).body
    except RunnerError as error:
        if error.status == 404:
            return None
        raise
    row = normalize_role_assignment(body) if isinstance(body, dict) else None
    if row is None:
        raise GatewayError("Unexpected role assignment response.")
    return row


def put_role_assignment(runner, scope: str, name: str, principal_id: str, principal_type: str, role: str) -> str:
    """Create one assignment; returns ``created`` or ``exists`` (same name already present)."""
    body = {"properties": {"roleDefinitionId": role_definition_id(role), "principalId": principal_id,
                           "principalType": principal_type}}
    try:
        runner("PUT", f"{role_assignments_url(scope)}/{name}",
               params={"api-version": ROLE_ASSIGNMENT_API_VERSION}, body=body)
    except RunnerError as error:
        if error.status == 409 and error.code == "RoleAssignmentExists":
            return "exists"
        raise
    return "created"


def delete_role_assignment(runner, scope: str, name: str) -> bool:
    """Delete one assignment by name; False when it was already gone."""
    try:
        runner("DELETE", f"{role_assignments_url(scope)}/{name}",
               params={"api-version": ROLE_ASSIGNMENT_API_VERSION})
    except RunnerError as error:
        if error.status == 404:
            return False
        raise
    return True


PRINCIPAL_TYPES = ("ServicePrincipal", "User")
PRESETS = ("app-uami",)
_PRINCIPAL_KEYS = {"id", "type", "label", "roles"}
_LABEL = re.compile(r"[\w .:@/+-]{1,120}")


def validate_principals(data) -> list[dict]:
    """Normalize ``[{"id","type","label","roles"}]``; ids are lower-cased, roles ordered models then tools."""
    if not isinstance(data, list) or not data:
        raise GatewayError("Principals must be a non-empty JSON list.")
    principals, seen = [], set()
    for index, entry in enumerate(data):
        where = f"principals[{index}]"
        if not isinstance(entry, dict) or set(entry) - _PRINCIPAL_KEYS or not {"id", "type", "roles"} <= set(entry):
            raise GatewayError(f"{where} must be an object with id, type, roles and an optional label.")
        if not is_guid(entry["id"]):
            raise GatewayError(f"{where}.id must be a GUID.")
        if entry["type"] not in PRINCIPAL_TYPES:
            raise GatewayError(f"{where}.type must be one of {', '.join(PRINCIPAL_TYPES)}.")
        roles = entry["roles"]
        if not isinstance(roles, list) or not roles or any(role not in GATEWAY_ROLES for role in roles):
            raise GatewayError(f"{where}.roles must be a non-empty list drawn from {', '.join(GATEWAY_ROLES)}.")
        label = entry.get("label", entry["id"])
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise GatewayError(f"{where}.label must be 1-120 plain characters.")
        principal = entry["id"].lower()
        if principal in seen:
            raise GatewayError(f"{where} repeats principal {principal}.")
        seen.add(principal)
        principals.append({"id": principal, "type": entry["type"], "label": label,
                           "roles": [role for role in GATEWAY_ROLES if role in roles]})
    return principals


def load_principals(path) -> list[dict]:
    return validate_principals(read_json(path))


def preset_principals(name: str) -> list[dict]:
    if name != "app-uami":
        raise GatewayError(f"Unknown preset: {name}")
    return validate_principals([{
        "id": APP_UAMI_PRINCIPAL_ID, "type": "ServicePrincipal",
        "label": "id-app-zurlo-northwind", "roles": ["models"],
    }])
