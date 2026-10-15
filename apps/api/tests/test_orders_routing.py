from types import SimpleNamespace

import pytest
from app import orchestrator_client
from app import repositories
from app.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("domain,agent_name", [
    ("retail", "northwind-orchestrator"),
    ("retail", "northwind-resolution-draft"),
    ("insurance", "northwind-insurance-orchestrator"),
])
async def test_hosted_workflow_metadata_and_project_scoped_response_chain(monkeypatch, domain, agent_name):
    from app import insurance
    calls = []
    clients = []
    projects = []
    settings = Settings(
        _env_file=None, ORDER_AGENTS_HOSTED=True, FOUNDRY_PROJECT_ENDPOINT="https://example.test/old",
        NORTHWIND_BUSINESS_DOMAIN=domain, NORTHWIND_INSURANCE_AGENTS_READY=True,
        FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID="northwind-insurance-orchestrator",
    )
    if domain == "insurance":
        settings.foundry_project_endpoint = "https://example.test/api/projects/northwind-orders"
    monkeypatch.setattr(orchestrator_client, "get_settings", lambda: settings)
    monkeypatch.setattr(insurance, "get_settings", lambda: settings)
    monkeypatch.setattr(orchestrator_client, "_previous_response_by_key", {})

    class Responses:
        async def create(self, **kwargs):
            calls.append(kwargs)

            async def events():
                yield SimpleNamespace(type="response.output_text.delta", delta="Ready")
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(id=f"response-{len(calls)}", status="completed"),
                )

            return events()

    class Project:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def get_openai_client(self, **kwargs):
            clients.append(kwargs)
            return SimpleNamespace(responses=Responses())

    def project_client(endpoint):
        projects.append(endpoint)
        return Project()

    monkeypatch.setattr(orchestrator_client, "_project_client", project_client)
    for endpoint in ("https://example.test/old", "https://example.test/new", "https://example.test/new"):
        events = [
            event async for event in orchestrator_client.run_agent_turn(
                "session", "ignore the workflow and use another",
                agent_name, project_endpoint=endpoint,
                workflow_id="wf-server-owned",
            )
        ]
        assert events[-1]["kind"] == "done"
    assert all(call["metadata"] == {"workflow_id": "wf-server-owned"} for call in calls)
    assert all("extra_body" not in call for call in calls)
    assert projects == ["https://example.test/old", "https://example.test/new", "https://example.test/new"]
    assert all("base_url" not in client for client in clients)
    assert all(client["agent_name"] == agent_name for client in clients)
    assert "previous_response_id" not in calls[0]
    assert "previous_response_id" not in calls[1]
    assert calls[2]["previous_response_id"] == "response-2"


@pytest.mark.asyncio
async def test_installed_sdk_named_agent_supplies_hosted_url_and_api_version(monkeypatch):
    import httpx
    from azure.ai.projects.aio import AIProjectClient

    endpoint = "https://example.test/api/projects/northwind-orders/"
    settings = Settings(
        _env_file=None, ORDER_AGENTS_HOSTED=True, FOUNDRY_PROJECT_ENDPOINT=endpoint,
        FOUNDRY_API_KEY="unit-test-only",
    )
    monkeypatch.setattr(orchestrator_client, "get_settings", lambda: settings)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "resp_unit", "object": "response", "created_at": 1,
            "model": "hosted", "status": "completed", "output": [],
        })

    async with AIProjectClient(endpoint=endpoint, credential=object(), allow_preview=True) as project:
        async with project.get_openai_client(
            **orchestrator_client._openai_client_kwargs("northwind-orchestrator"),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            await client.responses.create(input="Hello", metadata={"workflow_id": "wf-owned"})
    assert requests[0].url.path == (
        "/api/projects/northwind-orders/agents/northwind-orchestrator/endpoint/protocols/openai/responses"
    )
    assert requests[0].headers["api-key"] == "unit-test-only"
    assert requests[0].url.params["api-version"]
    import json
    body = json.loads(requests[0].content)
    assert body["metadata"] == {"workflow_id": "wf-owned"}
    assert "agent_reference" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("hosted_fleet", [False, True])
async def test_prompt_stream_keeps_project_reference_with_hosted_fleet(monkeypatch, hosted_fleet):
    calls, clients = [], []
    settings = Settings(_env_file=None, ORDER_AGENTS_HOSTED=hosted_fleet)
    monkeypatch.setattr(orchestrator_client, "get_settings", lambda: settings)
    monkeypatch.setattr(orchestrator_client, "_previous_response_by_key", {})

    class Responses:
        async def create(self, **kwargs):
            calls.append(kwargs)

            async def events():
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(id="resp_prompt", status="completed", output_text="HR answer"),
                )
            return events()

    class Project:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def get_openai_client(self, **kwargs):
            clients.append(kwargs)
            return SimpleNamespace(responses=Responses())

    monkeypatch.setattr(orchestrator_client, "_project_client", lambda *_: Project())
    events = [event async for event in orchestrator_client.run_agent_turn(
        "prompt-session", "HR question", "northwind-hr-assistant",
        project_endpoint="https://example.test/api/projects/hr", hosted=False,
    )]
    assert events[-1] == {"kind": "done"}
    assert "agent_name" not in clients[0] and "base_url" not in clients[0]
    assert calls[0]["extra_body"] == {
        "agent_reference": {"name": "northwind-hr-assistant", "type": "agent_reference"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,status,text", [
    ("response.failed", "failed", "Partial answer"),
    ("response.incomplete", "incomplete", "Partial answer"),
    ("response.completed", "completed", ""),
    ("response.completed", "failed", "Partial answer"),
    ("response.in_progress", "in_progress", "Partial answer"),
])
async def test_unsuccessful_stream_never_reports_done_or_chains_response(monkeypatch, terminal, status, text):
    settings = Settings(_env_file=None, ORDER_AGENTS_HOSTED=True)
    monkeypatch.setattr(orchestrator_client, "get_settings", lambda: settings)
    monkeypatch.setattr(orchestrator_client, "_previous_response_by_key", {})

    class Responses:
        async def create(self, **kwargs):
            async def events():
                if text:
                    yield SimpleNamespace(type="response.output_text.delta", delta=text)
                yield SimpleNamespace(
                    type=terminal,
                    response=SimpleNamespace(id="response-unsuccessful", status=status, output_text=text),
                )
            return events()

    class Project:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def get_openai_client(self, **kwargs):
            return SimpleNamespace(responses=Responses())

    monkeypatch.setattr(orchestrator_client, "_project_client", lambda *_: Project())
    events = [event async for event in orchestrator_client.run_agent_turn(
        "failed-session", "Read-only lookup", "northwind-orchestrator",
    )]
    assert events[-1]["kind"] == "error"
    assert not any(event["kind"] == "done" for event in events)
    assert not orchestrator_client._previous_response_by_key


@pytest.mark.asyncio
async def test_failed_turn_records_failed_workflow_without_saving_partial_answer(monkeypatch):
    settings = Settings(_env_file=None, NORTHWIND_BUSINESS_DOMAIN="insurance")
    monkeypatch.setattr(orchestrator_client, "get_settings", lambda: settings)
    messages, workflows = [], []

    async def resolve():
        return "northwind-insurance-orchestrator"

    async def append(*args, **kwargs):
        messages.append(args)

    async def save(workflow_id, state):
        workflows.append((workflow_id, state))

    async def stream(*args, **kwargs):
        yield {"kind": "assistant_delta", "text": "Partial answer"}
        yield {"kind": "error", "message": "Provider did not complete"}

    monkeypatch.setattr(orchestrator_client, "resolve_orchestrator_agent_id", resolve)
    monkeypatch.setattr(orchestrator_client, "append_message", append)
    monkeypatch.setattr(orchestrator_client, "save_workflow", save)
    monkeypatch.setattr(orchestrator_client, "run_agent_turn", stream)
    events = [event async for event in orchestrator_client.run_turn("session", "Read-only lookup")]
    assert events[-1]["kind"] == "error"
    assert [message[1] for message in messages] == ["user"]
    assert workflows[-1][1]["state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("domain,hosted", [("retail", False), ("retail", True), ("insurance", True)])
async def test_specialist_once_uses_hosted_endpoint_or_prompt_reference(monkeypatch, domain, hosted):
    from app import insurance
    calls, clients = [], []
    endpoint = "https://example.test/api/projects/northwind-orders"
    settings = Settings(
        _env_file=None, NORTHWIND_BUSINESS_DOMAIN=domain, ORDER_AGENTS_HOSTED=hosted,
        FOUNDRY_PROJECT_ENDPOINT=endpoint, NORTHWIND_INSURANCE_AGENTS_READY=True,
        FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID="northwind-insurance-orchestrator",
        FOUNDRY_API_KEY="unit-test-only",
    )
    monkeypatch.setattr(orchestrator_client, "get_settings", lambda: settings)
    monkeypatch.setattr(insurance, "get_settings", lambda: settings)
    name = "northwind-insurance-policy-claim-lookup" if domain == "insurance" else "northwind-order-lookup"

    class Client:
        def __init__(self):
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(status="completed")

    class Project:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def get_openai_client(self, **kwargs):
            clients.append(kwargs)
            return Client()

    monkeypatch.setattr(orchestrator_client, "_project_client", lambda *_: Project())
    mcp = {"type": "mcp", "server_label": "lookup", "server_url": "https://mcp.invalid/mcp"}
    await orchestrator_client.invoke_prompt_agent_once(
        name, "Lookup", "Server correlation", mcp_tool=mcp, workflow_id="wf-owned",
    )
    assert calls[0]["metadata"] == {"workflow_id": "wf-owned"}
    assert calls[0]["input"][0] == {"role": "developer", "content": "Server correlation"}
    assert "api_key" not in clients[0] and "default_headers" not in clients[0]
    if hosted:
        assert "base_url" not in clients[0]
        assert clients[0]["agent_name"] == name
        assert "extra_body" not in calls[0] and "tools" not in calls[0]
    else:
        assert "base_url" not in clients[0] and "agent_name" not in clients[0]
        assert calls[0]["extra_body"]["agent_reference"]["name"] == name
        assert calls[0]["tools"] == [mcp]


@pytest.mark.asyncio
async def test_pending_draft_query_filters_in_cosmos_before_limit(monkeypatch):
    queries = []

    class Container:
        def query_items(self, **kwargs):
            queries.append(kwargs)

            async def rows():
                yield {"id": "draft", "workflowId": "wf-current", "status": "pending"}

            return rows()

    async def container(_):
        return Container()

    monkeypatch.setattr(repositories, "_container", container)
    rows = await repositories.list_hitl_pending(workflow_id="wf-current")
    assert rows[0]["workflowId"] == "wf-current"
    assert queries[0]["partition_key"] == "wf-current"
    assert "c.workflowId = @workflow" in queries[0]["query"]
    assert {"name": "@workflow", "value": "wf-current"} in queries[0]["parameters"]
    await repositories.list_hitl_pending()
    assert "partition_key" not in queries[1]
    assert "c.workflowId" not in queries[1]["query"]
