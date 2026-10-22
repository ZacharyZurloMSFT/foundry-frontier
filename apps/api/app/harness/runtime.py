from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from azure.core.exceptions import AzureError

from ..settings import Settings, get_settings
from .diagnostics import log_failure
from .models import MAX_RESULT_CHARS, Event, HarnessError, Text, Usage, redact
from .store import RunStore
from .tools import NAMES, SpecialistTools
from .insurance_tools import InsuranceTools, NAMES as INSURANCE_NAMES

if TYPE_CHECKING:
    from copilot import CopilotClient

logger = logging.getLogger(__name__)
SDK_VERSION = "1.0.14"


async def sdk_call(stage, operation, run_id=""):
    from copilot._jsonrpc import JsonRpcError, ProcessExitedError
    try:
        return await operation()
    except (JsonRpcError, ProcessExitedError, AzureError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        log_failure(logger, "harness", stage, error, run_id)
        raise
    except Exception as error:
        log_failure(logger, "harness", stage, error, run_id)
        raise


def instructions(settings: Settings) -> tuple[str, str]:
    if settings.harness_prompt_path:
        path = Path(settings.harness_prompt_path)
    else:
        filename = "instructions.prompty" if settings.business_domain == "insurance" else "retail.instructions.prompty"
        bundled = Path(__file__).resolve().parents[1] / "_agent_prompts" / filename
        canonical = next((parent / "agents" / "copilot-harness" / filename
                          for parent in Path(__file__).resolve().parents
                          if (parent / "agents" / "copilot-harness" / filename).is_file()), None)
        path = canonical or bundled
    content = path.read_text(encoding="utf-8")
    _, metadata, body = content.split("---", 2)
    if not metadata.strip() or not body.lstrip().startswith("system:"):
        raise ValueError("Invalid harness prompt; expected Prompty metadata and a system section.")
    if f"business_domain: {settings.business_domain}" not in metadata.splitlines():
        raise ValueError("Harness prompt business domain does not match runtime configuration.")
    return body.lstrip()[len("system:"):].strip(), hashlib.sha256(content.encode()).hexdigest()


def runtime_environment() -> dict[str, str]:
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
        "LANG", "LC_ALL", "SSL_CERT_FILE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
        "COPILOT_CLI_PATH", "COPILOT_CLI_EXTRACT_DIR", "COPILOT_SKIP_CLI_DOWNLOAD",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def deny_permission(_request, _invocation):
    from copilot.generated.rpc import PermissionDecisionReject
    return PermissionDecisionReject(feedback="Only explicitly registered Northwind specialist tools are permitted.")


class HarnessRuntime:
    def __init__(self):
        self.client: CopilotClient | None = None
        self.credential = None
        self.lock = asyncio.Lock()
        self.active = 0
        self.runtime_version: str | None = None
        self.last_error: str | None = None
        self.directory: str | None = None
        self.workspace: tempfile.TemporaryDirectory | None = None
        self.verified_config: tuple | None = None

    @staticmethod
    def configuration(settings: Settings, prompt_sha: str) -> tuple:
        return (settings.business_domain, settings.foundry_project_endpoint, settings.cosmos_endpoint,
                settings.harness_model or settings.planner_deployment, settings.azure_client_id,
                settings.harness_resolution_mcp, settings.order_agents_hosted,
                settings.insurance_orchestrator_agent_id, prompt_sha)

    def status(self) -> dict:
        settings = get_settings()
        reasons = []
        if settings.business_domain == "insurance":
            from ..insurance import insurance_runtime_ready
            from ..insurance_domain import fixtures
            if not insurance_runtime_ready():
                reasons.append("Verified insurance agent configuration is required; retail agents are never a fallback.")
            try:
                mcp = urlsplit(settings.harness_resolution_mcp)
                valid_mcp = (mcp.scheme == "https" and mcp.hostname and not mcp.username
                             and not mcp.password and not mcp.query and not mcp.fragment
                             and mcp.path.rstrip("/").endswith(("/mcp", "/mcp/work-order")))
            except ValueError:
                valid_mcp = False
            if not valid_mcp:
                reasons.append("MCP_WORK_ORDER_URL must identify the HTTPS insurance draft MCP endpoint.")
            try:
                if fixtures().get("dataset_version") != "insurance-synthetic-v2":
                    reasons.append("Canonical insurance v2 records and wording must be packaged.")
            except (OSError, ValueError):
                reasons.append("Canonical insurance records are unavailable.")
        version = None
        prompt_sha = ""
        try:
            version = importlib.metadata.version("github-copilot-sdk")
            if version != SDK_VERSION:
                reasons.append(f"Install github-copilot-sdk=={SDK_VERSION}.")
            _, prompt_sha = instructions(settings)
        except (importlib.metadata.PackageNotFoundError, OSError, ValueError) as exc:
            reasons.append(f"Harness dependencies unavailable: {type(exc).__name__}.")
        if not settings.foundry_project_endpoint or not settings.cosmos_endpoint:
            reasons.append("Foundry project and Cosmos endpoints must be configured.")
        if self.last_error:
            reasons.append(self.last_error)
        enabled = settings.harness_enabled
        verified = (self.client is not None and self.credential is not None
                    and self.verified_config == self.configuration(settings, prompt_sha))
        return {
            "enabled": enabled,
            "state": "disabled" if not enabled else "unavailable" if reasons else "ready" if verified else "configured",
            "message": "; ".join(reasons) or (
                "A complete SDK run verified this configuration; access is rechecked on every run." if verified else
                "Dependencies are configured, not live-verified. Connections will be verified when a run starts."),
            "sdk_version": version, "runtime_version": self.runtime_version,
            "model": settings.harness_model or settings.planner_deployment,
            "provider": "Azure Foundry / managed identity",
            "max_tool_calls": settings.harness_max_tools,
            "max_concurrent_runs": settings.harness_concurrency,
            "timeout_seconds": settings.harness_timeout,
            "auth_scope": "Synthetic public demo; not tenant-isolated.",
            "business_domain": settings.business_domain,
            "tools": list(INSURANCE_NAMES if settings.business_domain == "insurance" else NAMES),
            "review_scope": "draft_only",
        }

    async def start(self) -> None:
        async with self.lock:
            if self.client:
                return
            from copilot import CopilotClient
            settings = get_settings()
            try:
                parent = settings.harness_runtime_dir or None
                if parent:
                    Path(parent).mkdir(parents=True, exist_ok=True)
                self.workspace = tempfile.TemporaryDirectory(prefix="northwind-runtime-", dir=parent)
                self.directory = self.workspace.name
                client = CopilotClient(
                    mode="empty", use_logged_in_user=False, working_directory=self.directory,
                    base_directory=self.directory, env=runtime_environment(), log_level="error",
                )
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                log_failure(logger, "harness", "runtime_prepare", error)
                raise
            try:
                await sdk_call("runtime_start", client.start)
                info = await sdk_call("runtime_status", client.get_status)
                self.runtime_version = info.version
                try:
                    self.credential = (ManagedIdentityCredential(client_id=settings.azure_client_id)
                                       if settings.azure_client_id else DefaultAzureCredential())
                except (AzureError, ValueError, RuntimeError) as error:
                    log_failure(logger, "harness", "identity_init", error)
                    raise
            except BaseException:
                await sdk_call("runtime_start_cleanup", client.stop)
                self.last_error = "SDK runtime startup failed; check server logs and the packaged runtime."
                raise
            self.client = client
            self.last_error = None

    async def stop(self) -> None:
        self.verified_config = None
        if self.client:
            await self.client.stop()
            self.client = None
        if self.credential:
            await self.credential.close()
            self.credential = None
        if self.workspace:
            self.workspace.cleanup()
            self.workspace = None

    async def run(self, run_id: str, wid: str, message: str, store: RunStore) -> AsyncIterator[Event]:
        await self.start()
        assert self.client is not None and self.credential is not None
        settings = get_settings()
        prompt, prompt_sha = instructions(settings)
        self.verified_config = None
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1024)
        overflow = False

        def emit(event: Event) -> None:
            nonlocal overflow
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                overflow = True

        def on_event(event) -> None:
            kind = event.type.value
            if kind == "assistant.message_delta" and settings.business_domain != "insurance":
                emit(Text(kind="assistant_delta", text=redact(event.data.delta_content)))
            elif kind == "assistant.usage":
                emit(Usage(source="manager", model=getattr(event.data, "model", None),
                           input_tokens=event.data.input_tokens, output_tokens=event.data.output_tokens))
            elif kind == "session.error":
                log_failure(logger, "harness", "sdk_session_error_event", event.data, run_id)

        async def token(_args) -> str:
            return (await sdk_call("provider_token", lambda: self.credential.get_token(
                "https://ai.azure.com/.default"), run_id)).token

        insurance = settings.business_domain == "insurance"
        tools = (InsuranceTools(settings, run_id, wid, emit, store, message=message) if insurance else
                 SpecialistTools(settings, run_id, wid, emit, store))
        names = INSURANCE_NAMES if insurance else NAMES
        session = await sdk_call("session_create", lambda: self.client.create_session(
            session_id=run_id, model=settings.harness_model or settings.planner_deployment,
            provider={"type": "azure", "base_url": settings.foundry_project_endpoint,
                      "wire_api": "responses", "bearer_token_provider": token,
                      "max_output_tokens": 4096},
            tools=tools.build(), available_tools=[f"custom:{name}" for name in names],
            on_permission_request=deny_permission, streaming=True, on_event=on_event,
            system_message={"mode": "append", "content": prompt},
            skip_custom_instructions=True, enable_config_discovery=False,
            enable_session_store=False, enable_skills=False, enable_file_hooks=False,
            enable_host_git_operations=False, enable_session_telemetry=False,
            request_extensions=False, manage_schedule_enabled=False,
            include_sub_agent_streaming_events=False,
            mcp_servers={}, plugin_directories=[], skill_directories=[],
            infinite_sessions={"enabled": False}, memory={"enabled": False},
            large_output={"enabled": False}, tool_search={"enabled": False},
        ), run_id)
        task = asyncio.create_task(sdk_call("session_send_wait", lambda: session.send_and_wait(
            message, timeout=settings.harness_timeout), run_id))
        completed = False
        stage = "runtime_events"
        try:
            while not task.done() or not queue.empty():
                if overflow:
                    raise HarnessError("event_limit", "The runtime produced events faster than they could be saved.")
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except TimeoutError:
                    continue
                batch = [event]
                while not queue.empty() and len(batch) < 64:
                    following = queue.get_nowait()
                    if (isinstance(following, Text) and following.kind == "assistant_delta"
                            and isinstance(batch[-1], Text) and batch[-1].kind == "assistant_delta"):
                        batch[-1].text += following.text
                    else:
                        batch.append(following)
                for queued in batch:
                    yield queued
            stage = "session_wait"
            response = await task
            stage = "response_validation"
            if tools.failed:
                raise HarnessError("specialist_failed", "A specialist failed. Inspect the tool timeline and saved approval before retrying.")
            if not response or not response.data.content.strip():
                raise HarnessError("empty_response", "The SDK ended the run without a final response.")
            if len(response.data.content) > MAX_RESULT_CHARS:
                raise HarnessError("output_limit", "The assistant output exceeded the run limit.")
            stage = "response_grounding"
            text = tools.final_text(response.data.content) if insurance else response.data.content
            if len(text) > MAX_RESULT_CHARS:
                raise HarnessError("output_limit", "The grounded assistant output exceeded the run limit.")
            yield Text(kind="assistant_message", text=redact(text))
            completed = True
            self.verified_config = self.configuration(settings, prompt_sha)
        except (HarnessError, AzureError, OSError, RuntimeError, ValueError, TimeoutError) as error:
            log_failure(logger, "harness", stage, error, run_id)
            raise
        except Exception as error:
            log_failure(logger, "harness", stage, error, run_id)
            raise
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            try:
                if not completed:
                    await sdk_call("session_abort", lambda: asyncio.wait_for(session.abort(), timeout=10), run_id)
            finally:
                try:
                    await sdk_call("session_disconnect", lambda: asyncio.wait_for(session.disconnect(), timeout=10), run_id)
                finally:
                    await sdk_call("session_delete", lambda: asyncio.wait_for(
                        self.client.delete_session(run_id), timeout=10), run_id)


runtime = HarnessRuntime()
