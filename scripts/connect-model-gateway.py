"""Create or update the Foundry model connection that routes prompt agents through the AI Gateway.

A Foundry prompt agent reaches a gateway-hosted model through a connection of
category `ApiManagement`; the agent's model is then `<connection>/<model name>`.
This script builds that connection for `ai-gateway-northwind`:

  target    https://ai-gateway-northwind.azure-api.net/default/models/openai/v1
            (no trailing slash; Foundry appends /chat/completions)
  metadata  deploymentInPath "false" (the gateway routes on the request `model`),
            a static `models` list whose names are the gateway aliases
            `<provider>/<registration>` (the exact `model` values the gateway
            accepts) and, for --auth api-key, an `authConfig` that sends the key
            in the `api-key` header.
  ARM       PUT https://management.azure.com{account-or-project-id}/connections/{name}

Dry run is the default: the request is printed with secrets redacted and nothing
is sent. `--apply` performs the PUT once through `az rest` (needs `az login`); the
runner is injectable so tests never touch Azure. The runtime key for --auth
api-key is read only from the environment variable named by --api-key-env
(default AI_GATEWAY_RUNTIME_KEY); it is never printed, logged or written to the
repo, and it does not appear on the `az` command line.

NOT VERIFIED LIVE. Foundry's bring-your-own-model documentation describes the
classic API Management shape (Standard v2 and Premium tiers); the exact
connection payload for the `AIGateway` SKU has not been confirmed. It must be
confirmed with ONE canary prompt agent before any bulk use: create the
connection, run one agent through it (model `<connection>/<alias>`), confirm a
response and the request in the gateway log, and only then create the other
agents. Open points to settle with the canary:

  1. Category and API version. `ApiManagement` is listed in the ARM enum from
     2025-10-01-preview (not in 2025-06-01), hence that default --api-version.
     `--category ModelGateway` is the documented alternative (Foundry accepts an
     API key or OAuth 2.0 there; this script sends an API key).
  2. Entra auth. `--auth aad` writes authType `AAD`, documented for
     ApiManagement connections but without an audience. The Foundry sample for
     Entra auth uses `ProjectManagedIdentity` plus `audience`; try
     `--entra-auth-type ProjectManagedIdentity` (audience
     https://azure-api.net/aigateway, the gateway token audience). The identity
     Foundry calls with needs the "AI Gateway Models User" role on the gateway;
     this script does not grant it.
  3. Aliases contain a slash, so the agent model is
     `<connection>/<provider>/<registration>`; Foundry has to split it at the
     first slash.
  4. `inferenceAPIVersion` is omitted unless --inference-api-version is set;
     Foundry may then append its default `api-version` query, which the gateway
     has to tolerate.
  5. BYOM connections serve prompt agents only; hosted agents keep the
     client-side gateway path (ORDERS_MODEL_GATEWAY_*).

Environment (all optional): AI_GATEWAY_MODEL_CONNECTION (default --name),
AI_GATEWAY_MODEL_PROVIDER (default aisvc-zurlo-northwind) and
AI_GATEWAY_MODEL_ALIASES (JSON map deployment -> full alias) behave exactly as
in scripts/create-prompt-agents.py so the connection and the agents agree.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
gw = importlib.import_module("ai_gateway_common")

ARM_HOST = "https://management.azure.com"
ARM_API_VERSION = "2025-10-01-preview"
DEFAULT_ACCOUNT = "aisvc-zurlo-northwind"
GATEWAY_AUDIENCE = "https://azure-api.net/aigateway"
DEFAULT_DEPLOYMENTS = ("gpt-4o", "gpt-5.4", "gpt-5.4-mini", "gpt-5.6-luna", "Kimi-K2.6")
CATEGORIES = ("ApiManagement", "ModelGateway")
ENTRA_AUTH_TYPES = ("AAD", "ProjectManagedIdentity")
REDACTED = "<redacted>"
UNVERIFIED_NOTE = (
    "NOT VERIFIED LIVE: the connection payload for the AI Gateway tier is unconfirmed. Create it, then run ONE "
    "canary prompt agent through it before bulk use (see the docstring of scripts/connect-model-gateway.py)."
)
IDENTITY_NOTE = (
    "Entra auth: the identity Foundry calls the gateway with (the project or account managed identity) needs the "
    "'AI Gateway Models User' role on the gateway; this script does not grant it."
)

# ARM accounts/connections names: 3-33 characters.
_CONNECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,32}")
_PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_SUBSCRIPTION = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_ACCOUNT_ID = re.compile(
    rf"/subscriptions/{_SUBSCRIPTION}/resourceGroups/[A-Za-z0-9_().-]{{1,90}}"
    r"/providers/Microsoft\.CognitiveServices/accounts/[A-Za-z0-9][A-Za-z0-9._-]{1,63}",
    re.IGNORECASE,
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_FULL_ALIAS = re.compile(r"[^/\s]+/[^/\s]+")
# Characters cmd.exe treats specially; `az` is a .cmd launcher on Windows.
_UNSAFE_ARGUMENT = re.compile(r'[&|<>^%!"\r\n\x00]')

Runner = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]


def validate_connection_name(name: str) -> str:
    if not _CONNECTION_NAME.fullmatch(name or ""):
        raise ValueError("connection name must be 3-33 letters, digits, '-' or '_', starting with a letter or digit")
    return name


def account_resource_id(subscription: str = gw.SUBSCRIPTION, resource_group: str = gw.RESOURCE_GROUP,
                        account: str = DEFAULT_ACCOUNT) -> str:
    return (f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{account}")


def connection_resource_id(account_id: str, name: str, project: str | None = None) -> str:
    """ARM id of the connection: account scope by default, project scope when `project` is given."""
    account_id = account_id.strip().rstrip("/")
    if not _ACCOUNT_ID.fullmatch(account_id):
        raise ValueError("--account-id must be the ARM id of a Microsoft.CognitiveServices account")
    validate_connection_name(name)
    if project is None:
        return f"{account_id}/connections/{name}"
    if not _PROJECT_NAME.fullmatch(project):
        raise ValueError("--project must be a Foundry project name")
    return f"{account_id}/projects/{project}/connections/{name}"


def request_url(resource_id: str, api_version: str = ARM_API_VERSION) -> str:
    return f"{ARM_HOST}{resource_id}?api-version={api_version}"


def gateway_target(gateway_url: str = gw.GATEWAY_URL) -> str:
    """OpenAI v1 base without the trailing slash; Foundry appends /chat/completions."""
    return gw.models_base_url(gateway_url).rstrip("/")


def parse_aliases(raw: str) -> dict[str, str]:
    """JSON map deployment -> full alias (same contract as AI_GATEWAY_MODEL_ALIASES elsewhere)."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    message = "aliases must be a JSON object mapping deployment names to <provider>/<registration> aliases"
    try:
        document = json.loads(raw)
    except ValueError:
        raise ValueError(message) from None
    if not isinstance(document, dict) or not all(isinstance(alias, str) for alias in document.values()):
        raise ValueError(message)
    aliases: dict[str, str] = {}
    for deployment, alias in document.items():
        if not (deployment.strip() and _FULL_ALIAS.fullmatch(alias.strip())):
            raise ValueError(f"{message}; got {deployment!r}: {alias!r}")
        aliases[deployment.strip()] = alias.strip()
    return aliases


def model_entries(deployments: Sequence[str], provider: str = gw.MODEL_PROVIDER,
                  aliases: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Static model list. `name` is the gateway alias (what Foundry sends as `model`);
    `properties.model.name` is the underlying deployment."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for deployment in (d.strip() for d in deployments):
        alias = (aliases or {}).get(deployment) or gw.model_alias(deployment, provider)
        if alias in seen:
            raise ValueError(f"models map to the same gateway alias {alias!r}")
        seen.add(alias)
        entries.append({"name": alias,
                        "properties": {"model": {"name": deployment, "version": "", "format": "OpenAI"}}})
    if not entries:
        raise ValueError("at least one model is required")
    return entries


def build_connection_body(
    name: str,
    models: Sequence[Mapping[str, Any]],
    *,
    target: str,
    project: str | None = None,
    auth: str = "aad",
    api_key: str | None = None,
    category: str = "ApiManagement",
    entra_auth_type: str = "AAD",
    audience: str = GATEWAY_AUDIENCE,
    inference_api_version: str = "",
) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    # ARM connection metadata values are strings, so structured values are JSON text.
    metadata: dict[str, str] = {"deploymentInPath": "false"}
    if inference_api_version:
        metadata["inferenceAPIVersion"] = inference_api_version
    metadata["models"] = json.dumps(list(models), separators=(",", ":"))
    properties: dict[str, Any] = {
        "category": category, "target": target, "isSharedToAll": True, "metadata": metadata,
    }
    if auth == "api-key":
        if not api_key:
            raise ValueError("--auth api-key needs the runtime key in the environment variable named by --api-key-env")
        properties["authType"] = "ApiKey"
        properties["credentials"] = {"key": api_key}
        metadata["authConfig"] = json.dumps(
            {"type": "api_key", "name": "api-key", "format": "{api_key}"}, separators=(",", ":"))
    elif auth == "aad":
        if category == "ModelGateway":
            raise ValueError("ModelGateway connections here support only API key authentication (use --auth api-key)")
        if entra_auth_type not in ENTRA_AUTH_TYPES:
            raise ValueError(f"entra auth type must be one of {', '.join(ENTRA_AUTH_TYPES)}")
        properties["authType"] = entra_auth_type
        if entra_auth_type == "ProjectManagedIdentity":
            properties["audience"] = audience
    else:
        raise ValueError("auth must be aad or api-key")
    resource_type = ("Microsoft.CognitiveServices/accounts/connections" if project is None
                     else "Microsoft.CognitiveServices/accounts/projects/connections")
    return {"name": name, "type": resource_type, "properties": properties}


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _mask(item) for key, item in value.items()}
    return REDACTED


def redact(body: Mapping[str, Any]) -> dict[str, Any]:
    """Deep copy of an ARM body with every credential value masked."""
    redacted = json.loads(json.dumps(body))
    properties = redacted.get("properties")
    if isinstance(properties, dict) and "credentials" in properties:
        properties["credentials"] = _mask(properties["credentials"])
    return redacted


def secret_values(body: Mapping[str, Any]) -> list[str]:
    """Literal credential strings carried by an ARM body (for scrubbing tool output)."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, str) and value:
            found.append(value)

    walk((body.get("properties") or {}).get("credentials"))
    return found


def scrub(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            for variant in {secret, json.dumps(secret)[1:-1]}:
                text = text.replace(variant, REDACTED)
    return text


def az_rest(method: str, url: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Send one ARM request with `az rest`.

    The body goes through a file in a private temporary directory, removed before
    returning, so credentials never appear on the command line or in a process list.
    """
    az = shutil.which("az")
    if az is None:
        raise RuntimeError("Azure CLI (az) is required for --apply but was not found on PATH")
    # A relative body path keeps the command line free of quoted paths (az is a .cmd file on Windows).
    arguments = ["rest", "--method", method, "--url", url, "--body", "@body.json",
                 "--headers", "Content-Type=application/json", "--output", "json"]
    if any(_UNSAFE_ARGUMENT.search(argument) for argument in arguments):
        raise RuntimeError("A request argument contains characters that are unsafe for the Azure CLI launcher")
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "body.json").write_text(json.dumps(body), encoding="utf-8")
        try:
            completed = subprocess.run(
                [az, *arguments], capture_output=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, check=False, timeout=180, cwd=directory,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"az rest {method} timed out; check the connection in Foundry before retrying") from None
        except OSError as exc:
            raise RuntimeError(f"Azure CLI could not be started: {exc.strerror or type(exc).__name__}") from None
    if completed.returncode != 0:
        detail = scrub(completed.stderr.strip(), secret_values(body))[:800]
        raise RuntimeError(f"az rest {method} failed with exit code {completed.returncode}: {detail}")
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def summarize(response: Mapping[str, Any]) -> dict[str, Any]:
    properties = response.get("properties") or {}
    return {"id": response.get("id"), "category": properties.get("category"),
            "target": properties.get("target"), "authType": properties.get("authType")}


def _env_name(value: str) -> str:
    if not _ENV_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("must be the NAME of an environment variable that holds the key, not the key")
    return value


def build_parser(env: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or update the Foundry model connection that routes prompt agents through the AI "
                    "Gateway. Dry run unless --apply. " + UNVERIFIED_NOTE)
    parser.add_argument("--name", default=env.get("AI_GATEWAY_MODEL_CONNECTION", "").strip() or None,
                        help="connection name (default: AI_GATEWAY_MODEL_CONNECTION)")
    parser.add_argument("--account-id", default=account_resource_id(),
                        help="ARM id of the Foundry (AI Services) account; default is the live account")
    parser.add_argument("--project", help="create the connection in this project instead of the account")
    parser.add_argument("--auth", choices=("aad", "api-key"), default="aad",
                        help="aad (default): Entra token of the project/account identity; "
                             "api-key: gateway runtime key sent as the api-key header")
    parser.add_argument("--api-key-env", type=_env_name, default="AI_GATEWAY_RUNTIME_KEY",
                        help="environment variable holding the runtime key for --auth api-key")
    parser.add_argument("--entra-auth-type", choices=ENTRA_AUTH_TYPES, default="AAD",
                        help="ARM authType used by --auth aad (ProjectManagedIdentity also sends --audience)")
    parser.add_argument("--audience", default=GATEWAY_AUDIENCE,
                        help="token audience the gateway expects (sent with ProjectManagedIdentity)")
    parser.add_argument("--category", choices=CATEGORIES, default="ApiManagement")
    parser.add_argument("--models", help="comma-separated deployment names (default: "
                                         + ", ".join(DEFAULT_DEPLOYMENTS) + ")")
    parser.add_argument("--provider", default=env.get("AI_GATEWAY_MODEL_PROVIDER", "").strip() or gw.MODEL_PROVIDER,
                        help="gateway model provider (default: AI_GATEWAY_MODEL_PROVIDER or aisvc-zurlo-northwind)")
    parser.add_argument("--aliases", default=env.get("AI_GATEWAY_MODEL_ALIASES", ""),
                        help="JSON map deployment -> full alias (default: AI_GATEWAY_MODEL_ALIASES)")
    parser.add_argument("--gateway-url", default=gw.GATEWAY_URL)
    parser.add_argument("--inference-api-version", default="",
                        help="inferenceAPIVersion metadata; omitted when empty")
    parser.add_argument("--api-version", default=ARM_API_VERSION, help="ARM api-version for the connection PUT")
    parser.add_argument("--apply", action="store_true", help="send the PUT (default is a dry run)")
    return parser


def plan(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Request URL, ARM body (with the real key) and model entries for the parsed arguments."""
    if not args.name:
        raise ValueError("--name (or AI_GATEWAY_MODEL_CONNECTION) is required")
    deployments = ([d.strip() for d in args.models.split(",") if d.strip()]
                   if args.models else list(DEFAULT_DEPLOYMENTS))
    models = model_entries(deployments, args.provider.strip(), parse_aliases(args.aliases))
    api_key = None
    if args.auth == "api-key":
        api_key = str(env.get(args.api_key_env) or "").strip() or None
        if api_key is None and args.apply:
            raise ValueError(f"environment variable {args.api_key_env} is not set or empty")
    body = build_connection_body(
        args.name, models, target=gateway_target(args.gateway_url), project=args.project, auth=args.auth,
        api_key=api_key or REDACTED, category=args.category, entra_auth_type=args.entra_auth_type,
        audience=args.audience, inference_api_version=args.inference_api_version,
    )
    url = request_url(connection_resource_id(args.account_id, args.name, args.project), args.api_version)
    return url, body, models


def main(argv: Sequence[str] | None = None, *, runner: Runner = az_rest,
         env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    args = build_parser(env).parse_args(argv)
    try:
        url, body, models = plan(args, env)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    secrets = secret_values(body)
    if not args.apply:
        print("DRY RUN: no request sent. Re-run with --apply to create or update the connection.")
        print(f"PUT {url}")
        print(json.dumps(redact(body), indent=2))
        print("Prompt-agent model strings (AI_GATEWAY_MODEL_CONNECTION=" + args.name + "):")
        for entry in models:
            print(f"  {entry['properties']['model']['name']} -> {args.name}/{entry['name']}")
        if args.auth == "api-key" and not env.get(args.api_key_env):
            print(f"Note: {args.api_key_env} is not set; --apply will refuse to run without it.")
        if args.auth == "aad":
            print(IDENTITY_NOTE)
        print(UNVERIFIED_NOTE)
        return 0
    try:
        response = runner("PUT", url, body)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {scrub(str(exc), secrets)}", file=sys.stderr)
        return 1
    print("Connection applied: " + json.dumps(summarize(response)))
    if args.auth == "aad":
        print(IDENTITY_NOTE)
    print(UNVERIFIED_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
