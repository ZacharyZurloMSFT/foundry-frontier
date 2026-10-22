"""Agent Framework execution with request isolation and fail-closed tools."""
from __future__ import annotations

import json
import re
from typing import Literal, Mapping
from urllib.parse import urlsplit

from agent_framework import Agent, AgentMiddleware, AgentResponse, ChatResponse, ChatResponseUpdate, Content, ResponseStream
from agent_framework.foundry import FoundryChatClient
from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient
from azure.identity.aio import get_bearer_token_provider
from openai import AsyncOpenAI
from openai.types.responses import Response as ProviderResponse
from pydantic import BaseModel, ConfigDict

from .config import Settings, https_url
from .scope import current_scope
from .tools import proposal_tool, specialist_tools
from ..insurance.models import InsuranceTriageResult


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str | None
    customer_hint: str | None
    intent: Literal["ORDER_STATUS", "RETURN", "CANCEL", "MODIFY", "COMPLAINT", "REFUND_REQUEST", "OTHER"]
    sentiment: Literal["calm", "frustrated", "angry"]
    urgency: Literal["P1", "P2", "P3", "P4"]
    problem_summary: str


def check_retrieval_result(output):
    if isinstance(output, Content):
        if output.type == "error" or output.error_code or output.exception:
            raise RuntimeError("MCP retrieval returned an error")
        check_retrieval_result(output.text)
        return
    if isinstance(output, list):
        for part in output:
            check_retrieval_result(part)
        return
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return
    if isinstance(output, dict):
        if output.get("isError") or output.get("error") or output.get("status") in ("error", "failed"):
            raise RuntimeError("MCP retrieval returned an error")
        for part in output.get("content", []):
            if isinstance(part, dict) and part.get("type") == "text":
                check_retrieval_result(part.get("text"))


class RequestMiddleware(AgentMiddleware):
    def __init__(self, settings: Settings):
        self.settings = settings

    async def process(self, context, call_next):
        scope = current_scope.get()
        if scope.failures:
            raise ValueError(scope.failures[0])
        if self.settings.role == "resolutiondraft":
            scope.require_workflow()
        if context.session is not None:
            bound = context.session.state.get("orders_workflow_id")
            if bound is not None and scope.workflow_id != bound:
                raise ValueError("Workflow metadata cannot change within an agent session")
            if scope.workflow_id:
                context.session.state["orders_workflow_id"] = scope.workflow_id
        # Caller options cannot add tools, change model, or remove grounding.
        context.options = {
            **(context.options or {}),
            "model": self.settings.inference_model,
            "store": False,
            "allow_multiple_tool_calls": False,
        }
        if self.settings.role == "triage":
            context.options["response_format"] = (
                InsuranceTriageResult if self.settings.business_domain == "insurance" else TriageResult)
        scope.relay_enabled = context.stream
        await call_next()
        original = context.result
        if not context.stream:
            self.capture_provider(scope, original.raw_representation)
            self.validate(scope, original.text)
            return

        async def merged():
            text: list[str] = []
            held = []
            # Keep framework iteration in its original task: telemetry scope tokens
            # must be created and reset in the same context. Completed child tool
            # events are drained before the manager's tool result, never discarded.
            async for update in original:
                self.capture_provider(scope, update.raw_representation)
                while not scope.events.empty():
                    yield scope.events.get_nowait()
                if scope.failures:
                    raise RuntimeError(scope.failures[0])
                for content in update.contents:
                    if content.type == "mcp_server_tool_result":
                        if content.output is None or content.exception or content.error_code:
                            raise RuntimeError("MCP retrieval did not return a successful result")
                        check_retrieval_result(content.output)
                        scope.tool_results += 1
                    if content.type == "text" and content.text:
                        text.append(content.text)
                if self.settings.role == "triage" or (
                    self.settings.role in ("orderlookup", "policylookup", "resolutiondraft")
                    and scope.tool_results == 0 and any(c.type == "text" for c in update.contents)
                ):
                    held.append(update)
                else:
                    yield update
            self.validate(scope, "".join(text))
            for update in held:
                yield update

        context.result = ResponseStream(merged(), finalizer=lambda updates: AgentResponse.from_updates(updates))

    def capture_provider(self, scope, response):
        if ((self.settings.role != "triage" and not self.settings.uses_model_gateway)
                or not isinstance(response, (ChatResponse, ChatResponseUpdate))):
            return
        if self.settings.role == "triage":
            model, response_id = response.model, response.response_id
        else:
            # Responses deltas default their framework model to the configured alias.
            # Only native response envelopes identify the actual provider model.
            native = response.raw_representation
            native = getattr(native, "response", native)
            if not isinstance(native, ProviderResponse):
                return
            model, response_id = native.model, native.id
        if model:
            if scope.provider_model and scope.provider_model != model:
                raise RuntimeError("Provider model changed within an inference response")
            scope.provider_model = model
        if response_id:
            scope.provider_response_id = response_id

    def validate(self, scope, text: str):
        if scope.failures:
            raise RuntimeError(scope.failures[0])
        if self.settings.role == "triage":
            (InsuranceTriageResult if self.settings.business_domain == "insurance" else TriageResult).model_validate_json(text)
        if self.settings.role in ("orderlookup", "policylookup") and not scope.tool_results:
            raise RuntimeError("Specialist did not use its required grounding tool")
        if self.settings.role == "resolutiondraft" and scope.approval is None:
            raise RuntimeError("No persisted resolution draft was confirmed")


def account_openai_client(settings: Settings, credential, *, http_client=None) -> AsyncOpenAI:
    variable = "ORDERS_MODEL_GATEWAY_ENDPOINT" if settings.uses_model_gateway else "ORDERS_OPENAI_ENDPOINT"
    endpoint = https_url(settings.env.get(variable, ""), variable)
    if not endpoint.rstrip("/").endswith("/openai/v1"):
        raise ValueError(f"{variable} must end in /openai/v1/")
    return AsyncOpenAI(
        base_url=f"{endpoint.rstrip('/')}/",
        api_key=get_bearer_token_provider(
            credential, settings.gateway_scope if settings.uses_model_gateway
            else "https://cognitiveservices.azure.com/.default"),
        http_client=http_client,
        max_retries=0,
    )


def knowledge_connection_headers(settings: Settings, connection: Mapping) -> dict[str, str]:
    spec = settings.knowledge_spec()
    credentials = connection.get("credentials")
    if connection.get("type") != "RemoteTool" or connection.get("target") != spec["url"]:
        raise ValueError("Knowledge connection must match the canonical RemoteTool endpoint")
    if not isinstance(credentials, Mapping) or credentials.get("type") != "CustomKeys":
        raise ValueError("Knowledge connection requires CustomKeys credentials")
    key = credentials.get("api-key")
    if not isinstance(key, str) or not key:
        raise ValueError("Knowledge connection did not supply its api-key header")
    return {"api-key": key}


GATEWAY_KEY_HEADER = "Ocp-Apim-Subscription-Key"


def connection_covers(target, url: str) -> bool:
    """The MCP URL itself, or an ancestor of it on the same origin, so that one gateway connection can
    serve both MCP servers. A different host, port, scheme or sibling path never matches."""
    if target == url:
        return True
    try:
        covering, endpoint = urlsplit(target), urlsplit(url)
        same_origin = (covering.scheme, covering.hostname, covering.port) == (
            endpoint.scheme, endpoint.hostname, endpoint.port)
    except (AttributeError, TypeError, ValueError):
        return False
    if not same_origin or covering.username or covering.password or covering.query or covering.fragment:
        return False
    base = covering.path.rstrip("/")
    return endpoint.path.rstrip("/") == base or endpoint.path.startswith(base + "/")


def mcp_gateway_headers(settings: Settings, connection: Mapping) -> dict[str, str]:
    spec = settings.mcp_spec()
    credentials = connection.get("credentials")
    if connection.get("type") != "RemoteTool":
        raise ValueError("MCP gateway connection must be a RemoteTool connection")
    if not connection_covers(connection.get("target"), spec["server_url"]):
        raise ValueError("MCP gateway connection target must be the MCP endpoint or its gateway base URL")
    if not isinstance(credentials, Mapping) or credentials.get("type") != "CustomKeys":
        raise ValueError("MCP gateway connection requires CustomKeys credentials")
    # Header names are case-insensitive and Foundry may return custom key names lower-cased.
    keys = [value for name, value in credentials.items()
            if isinstance(name, str) and name.lower() == GATEWAY_KEY_HEADER.lower()]
    if len(keys) != 1 or not isinstance(keys[0], str) or not keys[0]:
        raise ValueError("MCP gateway connection did not supply its Ocp-Apim-Subscription-Key header")
    # Never let a malformed secret reach an HTTP client, whose errors may echo header values.
    if not re.fullmatch(r"[\x21-\x7e]{1,512}", keys[0]):
        raise ValueError("MCP gateway connection supplied an invalid Ocp-Apim-Subscription-Key header")
    return {GATEWAY_KEY_HEADER: keys[0]}


def require_gateway_headers(settings: Settings, headers: Mapping[str, str] | None) -> dict[str, str] | None:
    if not settings.mcp_gateway_connection:
        return None
    if not headers:
        raise ValueError("MCP gateway authentication requires gateway connection headers")
    return dict(headers)


def build_agent(settings: Settings, project, *, account_client=None, knowledge_headers=None,
                gateway_headers=None) -> Agent:
    if settings.uses_model_gateway and account_client is None:
        raise ValueError("Gateway inference requires an identity-authenticated gateway client")
    if settings.role == "triage":
        if account_client is None:
            raise ValueError("Triage requires an identity-authenticated ORDERS_OPENAI_ENDPOINT client")
        client = OpenAIChatCompletionClient(model=settings.inference_model, async_client=account_client)
    elif settings.uses_model_gateway:
        client = OpenAIChatClient(model=settings.inference_model, async_client=account_client)
    else:
        client = FoundryChatClient(project_client=project, model=settings.model)
    tools = []
    if settings.role == "manager":
        tools = specialist_tools(settings, project)
    elif settings.role == "orderlookup":
        spec = settings.mcp_spec()
        gateway = require_gateway_headers(settings, gateway_headers)
        tools = [client.get_mcp_tool(
            name=spec["server_label"], url=spec["server_url"],
            allowed_tools=spec["allowed_tools"], approval_mode="never_require",
            **({"headers": gateway} if gateway else {}),
        )]
    elif settings.role == "policylookup":
        spec = settings.knowledge_spec()
        if settings.uses_model_gateway:
            # Account-level Responses cannot resolve a project-scoped connection.
            # Resolve its credential once under the agent identity; keep it out of env/config.
            if not knowledge_headers:
                raise ValueError("Gateway policy inference requires knowledge connection headers")
            spec.pop("project_connection_id")
            spec["headers"] = knowledge_headers
        tools = [client.get_mcp_tool(**spec)]
    elif settings.role == "resolutiondraft":
        tools = [proposal_tool(settings, require_gateway_headers(settings, gateway_headers))]
    return Agent(
        client=client,
        name=settings.prompt["name"],
        description=settings.prompt.get("description", ""),
        instructions=settings.prompt["instructions"],
        tools=tools,
        middleware=[RequestMiddleware(settings)],
        default_options={"store": False, "max_tokens": 4096, "allow_multiple_tool_calls": False,
                         **({"response_format": InsuranceTriageResult if settings.business_domain == "insurance"
                             else TriageResult} if settings.role == "triage" else {})},
        additional_properties=settings.metadata,
    )
