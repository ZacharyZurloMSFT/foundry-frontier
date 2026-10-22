"""Create the per-project Foundry connection that holds the AI Gateway runtime key for provider-executed MCP.

Hosted `orderlookup` / `policylookup` agents reach their MCP server through a
gateway tool server (`ORDERS_MCP_GATEWAY`). The agent reads the gateway runtime
key once at startup, under its own identity, from a project connection, exactly
like the Search knowledge-base connection created by
`scripts/connect-knowledge-base.py`:

  category  RemoteTool
  authType  CustomKeys
  target    https://ai-gateway-northwind.azure-api.net/default/toolservers/<server>/mcp
  keys      {"api-key": <gateway runtime key>}

The agent checks that the connection `target` equals the `url` of its
`ORDERS_MCP_GATEWAY` entry, so use the target this script prints. The key is
read only from the environment variable named by --key-env (default
AI_GATEWAY_RUNTIME_KEY); it is never printed, logged or written to the repo, and
it does not appear on the `az` command line. Create the runtime key itself with
the gateway's key management, not here.

Dry run is the default: the request is printed with the key redacted and
nothing is sent. `--apply` performs the PUT once through `az rest` (needs
`az login`); the runner is injectable so tests never touch Azure.

Example (dry run, then apply):

  python scripts/connect-mcp-gateway.py \\
      --project-arm-id /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project> \\
      --toolserver northwind-insurance-lookup --connection-name nw-insurance-claims-gateway-mcp
  AI_GATEWAY_RUNTIME_KEY=... python scripts/connect-mcp-gateway.py <same arguments> --apply
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
REDACTED = "<redacted>"

# ARM projects/connections names: 3-33 characters.
_CONNECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,32}")
_SUBSCRIPTION = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_PROJECT_ID = re.compile(
    rf"/subscriptions/{_SUBSCRIPTION}/resourceGroups/[A-Za-z0-9_().-]{{1,90}}"
    r"/providers/Microsoft\.CognitiveServices/accounts/[A-Za-z0-9][A-Za-z0-9._-]{1,63}"
    r"/projects/[A-Za-z0-9][A-Za-z0-9._-]{1,63}",
    re.IGNORECASE,
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# Characters cmd.exe treats specially; `az` is a .cmd launcher on Windows.
_UNSAFE_ARGUMENT = re.compile(r'[&|<>^%!"\r\n\x00]')

Runner = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]


def validate_connection_name(name: str) -> str:
    if not _CONNECTION_NAME.fullmatch(name or ""):
        raise ValueError("connection name must be 3-33 letters, digits, '-' or '_', starting with a letter or digit")
    return name


def connection_url(project_arm_id: str, connection_name: str, api_version: str = ARM_API_VERSION) -> str:
    project_arm_id = project_arm_id.strip().rstrip("/")
    if not _PROJECT_ID.fullmatch(project_arm_id):
        raise ValueError("--project-arm-id must be the ARM id of a Foundry project "
                         "(.../Microsoft.CognitiveServices/accounts/<account>/projects/<project>)")
    validate_connection_name(connection_name)
    return f"{ARM_HOST}{project_arm_id}/connections/{connection_name}?api-version={api_version}"


def gateway_target(toolserver: str, gateway_url: str = gw.GATEWAY_URL) -> str:
    """Gateway tool server MCP endpoint (no trailing slash)."""
    return gw.toolserver_url(toolserver, gateway_url)


def build_connection_body(connection_name: str, target: str, api_key: str) -> dict[str, Any]:
    """Same shape as the knowledge-base connection: RemoteTool with the key under `api-key`."""
    if not api_key:
        raise ValueError("the gateway runtime key is required")
    return {
        "name": connection_name,
        "type": "Microsoft.CognitiveServices/accounts/projects/connections",
        "properties": {
            "authType": "CustomKeys",
            "category": "RemoteTool",
            "target": target,
            "isSharedToAll": True,
            "credentials": {"keys": {"api-key": api_key}},
            "metadata": {"ApiType": "Azure"},
        },
    }


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
    returning, so the key never appears on the command line or in a process list.
    """
    az = shutil.which("az")
    if az is None:
        raise RuntimeError("Azure CLI (az) is required for --apply but was not found on PATH")
    # A relative body path keeps the command line free of quoted paths (az is a .cmd file on Windows).
    arguments = ["rest", "--method", method, "--url", url, "--body", "@body.json",
                 "--headers", "Content-Type=application/json", "--output", "json"]
    if any(_UNSAFE_ARGUMENT.search(argument) for argument in arguments):
        raise RuntimeError("A request argument contains characters that are unsafe for the Azure CLI launcher")
    directory = tempfile.mkdtemp(prefix="aigw-connection-")
    body_file = Path(directory) / "body.json"
    try:
        body_file.write_text(json.dumps(body), encoding="utf-8")
        try:
            completed = subprocess.run(
                [az, *arguments], capture_output=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, check=False, timeout=180, cwd=directory,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"az rest {method} timed out; check the connection in Foundry before retrying") from None
        except OSError as exc:
            raise RuntimeError(f"Azure CLI could not be started: {exc.strerror or type(exc).__name__}") from None
    finally:
        # The body holds the key, so delete it first. On Windows the Azure CLI's telemetry process can still hold
        # the directory as its working directory, so removing the directory is best effort and never an error.
        body_file.unlink(missing_ok=True)
        shutil.rmtree(directory, ignore_errors=True)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the project connection that holds the AI Gateway runtime key for a gateway MCP tool "
                    "server (dry run unless --apply).")
    parser.add_argument("--project-arm-id", required=True, help="ARM id of the Foundry project")
    parser.add_argument("--toolserver", required=True, help="gateway tool server name")
    parser.add_argument("--connection-name", required=True, help="project connection name (3-33 characters)")
    parser.add_argument("--key-env", type=_env_name, default="AI_GATEWAY_RUNTIME_KEY",
                        help="environment variable holding the gateway runtime key")
    parser.add_argument("--gateway-url", default=gw.GATEWAY_URL)
    parser.add_argument("--api-version", default=ARM_API_VERSION, help="ARM api-version for the connection PUT")
    parser.add_argument("--apply", action="store_true", help="send the PUT (default is a dry run)")
    return parser


def plan(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[str, str, dict[str, Any]]:
    """Request URL, connection target and ARM body (with the real key) for the parsed arguments."""
    target = gateway_target(args.toolserver, args.gateway_url)
    url = connection_url(args.project_arm_id, args.connection_name, args.api_version)
    key = str(env.get(args.key_env) or "").strip()
    if not key and args.apply:
        raise ValueError(f"environment variable {args.key_env} is not set or empty")
    return url, target, build_connection_body(args.connection_name, target, key or REDACTED)


def main(argv: Sequence[str] | None = None, *, runner: Runner = az_rest,
         env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    try:
        url, target, body = plan(args, env)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    secrets = secret_values(body)
    if not args.apply:
        print("DRY RUN: no request sent. Re-run with --apply to create or update the connection.")
        print(f"PUT {url}")
        print(json.dumps(redact(body), indent=2))
        print("Matching ORDERS_MCP_GATEWAY entry (set tool_prefix to the endpoint namespace plus '_'):")
        print(json.dumps({"url": target, "tool_prefix": "<namespace>_", "connection": args.connection_name}))
        if not env.get(args.key_env):
            print(f"Note: {args.key_env} is not set; --apply will refuse to run without it.")
        return 0
    try:
        response = runner("PUT", url, body)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {scrub(str(exc), secrets)}", file=sys.stderr)
        return 1
    print("Connection applied: " + json.dumps(summarize(response)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
