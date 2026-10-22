"""Run one independently deployed Order Concierge role on the GA Responses host."""
from __future__ import annotations

import json
import logging

from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.responses import ResponsesServerOptions
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects import AIProjectClient as ConnectionProjectClient
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.identity import DefaultAzureCredential as ConnectionCredential

from .config import Settings
from .runtime import account_openai_client, build_agent, mcp_connection_headers, mcp_gateway_headers
from .scope import RequestScope, current_scope


class OrdersResponsesHost(ResponsesHostServer):
    def __init__(self, agent, settings: Settings, *, resources=(), **kwargs):
        self.orders_settings = settings
        self.orders_resources = resources
        super().__init__(
            agent, options=ResponsesServerOptions(default_model=settings.model), **kwargs,
        )

    async def _handle_response(self, request, context, cancellation_signal):
        try:
            scope = RequestScope.from_metadata(request.get("metadata"))
        except ValueError as exc:
            scope = RequestScope(failures=[str(exc)])
        token = current_scope.set(scope)
        try:
            async for event in super()._handle_response(request, context, cancellation_signal):
                if "response" in event:
                    metadata = dict(self.orders_settings.metadata)
                    if scope.provider_model:
                        metadata["provider_model"] = scope.provider_model
                    if scope.provider_response_id:
                        metadata["provider_response_id"] = scope.provider_response_id
                    if scope.workflow_id:
                        metadata["workflow_id"] = scope.workflow_id
                    event["response"]["metadata"] = metadata
                    event["response"]["model"] = self.orders_settings.model
                yield event
        finally:
            current_scope.reset(token)

    async def _cleanup_agent(self):
        try:
            await super()._cleanup_agent()
        finally:
            for resource in self.orders_resources:
                await resource.close()


def load_mcp_headers(settings: Settings) -> dict[str, str] | None:
    """Resolve a role's gateway or Search runtime key once, under the agent identity, never from config."""
    connection_name = settings.mcp_connection
    if connection_name is None:
        return None
    with ConnectionCredential(
        managed_identity_client_id=settings.env.get("AZURE_CLIENT_ID") or None,
        exclude_interactive_browser_credential=True,
    ) as connection_credential:
        with ConnectionProjectClient(
            endpoint=settings.project_endpoint, credential=connection_credential, allow_preview=True,
        ) as connection_project:
            connection = connection_project.connections.get(connection_name, include_credentials=True)
            return mcp_connection_headers(settings, connection.as_dict())


def load_gateway_headers(settings: Settings) -> dict[str, str] | None:
    """Resolve the API Management gateway key once under the agent identity; it is kept in memory only."""
    if not settings.mcp_gateway_connection:
        return None
    with ConnectionCredential(
        managed_identity_client_id=settings.env.get("AZURE_CLIENT_ID") or None,
        exclude_interactive_browser_credential=True,
    ) as connection_credential:
        with ConnectionProjectClient(
            endpoint=settings.project_endpoint, credential=connection_credential, allow_preview=True,
        ) as connection_project:
            connection = connection_project.connections.get(
                settings.mcp_gateway_connection, include_credentials=True,
            )
            headers = mcp_gateway_headers(settings, connection.as_dict())
    logging.getLogger(__name__).info(
        "MCP gateway authentication enabled through project connection %s", settings.mcp_gateway_connection)
    return headers


def main():
    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info("Northwind %s runtime configuration: %s",
                                   settings.business_domain, json.dumps(settings.metadata))
    # One connection-resolved header set per role: the AI Gateway tool server key or the Search knowledge key.
    knowledge_headers = load_mcp_headers(settings)
    gateway_headers = load_gateway_headers(settings)
    credential = DefaultAzureCredential(
        managed_identity_client_id=settings.env.get("AZURE_CLIENT_ID") or None,
        exclude_interactive_browser_credential=True,
    )
    project = AIProjectClient(
        endpoint=settings.project_endpoint, credential=credential, allow_preview=True,
    )
    account_client = account_openai_client(settings, credential) if (
        settings.role == "triage" or settings.uses_model_gateway
    ) else None
    # The in-process work-order client reaches the AI Gateway as the agent identity, not with a stored key.
    gateway_token_provider = (
        get_bearer_token_provider(credential, settings.gateway_scope)
        if settings.role == "resolutiondraft" and settings.mcp_entry is not None else None
    )
    agent = build_agent(settings, project, account_client=account_client, knowledge_headers=knowledge_headers,
                        gateway_headers=gateway_headers, gateway_token_provider=gateway_token_provider)
    resources = (account_client, project, credential) if account_client else (project, credential)
    server = OrdersResponsesHost(agent, settings, resources=resources)
    server.run(host="0.0.0.0", port=8088)


if __name__ == "__main__":
    main()
