"""Opt-in SDK/runtime integration using a local deterministic model server, not Azure."""
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from app.harness import runtime, tools, insurance_tools
from app.harness.store import RunStore
from app.settings import Settings

pytestmark = pytest.mark.skipif(os.environ.get("TEST_COPILOT_RUNTIME") != "1",
                                reason="Set TEST_COPILOT_RUNTIME=1 after staging the pinned runtime.")


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["retail", "insurance"])
async def test_real_sdk_calls_only_registered_tools_and_refreshes_tokens(monkeypatch, domain):
    requests = []
    expected_names = insurance_tools.NAMES if domain == "insurance" else tools.NAMES
    selected_tool = "policy_claim_lookup" if domain == "insurance" else "triage"
    final_text = "Claim evidence retrieved." if domain == "insurance" else "Order classified."

    async def model(request):
        body = await request.json()
        requests.append((request.path, request.headers.get("Authorization"), body))
        response_id = f"resp_{len(requests)}"
        function = None
        if len(requests) == 1:
            names = []
            for tool in body["tools"]:
                candidates = tool.get("tools", [tool])
                for candidate in candidates:
                    names.append(candidate.get("name", "").split(":")[-1].split(".")[-1])
                    if candidate.get("name", "").endswith(selected_tool):
                        function = {"id": "fc_1", "type": "function_call", "status": "completed",
                                    "call_id": "call_1", "name": candidate["name"],
                                    "arguments": ('{"claim_id":"CLM-2001"}' if domain == "insurance"
                                                  else '{"prompt":"Classify this order-status request."}')}
                        if tool.get("type") == "namespace":
                            function["namespace"] = tool["name"]
            assert set(names) == set(expected_names), names
            assert function, [t.get("name") for t in body["tools"]]
        item = function or {
            "id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": final_text, "annotations": []}],
        }
        response = {"id": response_id, "object": "response", "created_at": int(time.time()),
                    "model": body["model"], "status": "in_progress", "output": []}
        events = [
            {"type": "response.created", "response": response},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**item, "arguments": ""} if function else {**item, "content": []}},
        ]
        if function:
            events.extend([
                {"type": "response.function_call_arguments.delta", "item_id": item["id"],
                 "output_index": 0, "delta": item["arguments"]},
                {"type": "response.function_call_arguments.done", "item_id": item["id"],
                 "output_index": 0, "arguments": item["arguments"]},
            ])
        else:
            part = item["content"][0]
            events.extend([
                {"type": "response.content_part.added", "item_id": item["id"], "output_index": 0,
                 "content_index": 0, "part": {**part, "text": ""}},
                {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                 "content_index": 0, "delta": part["text"]},
                {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0,
                 "content_index": 0, "text": part["text"]},
                {"type": "response.content_part.done", "item_id": item["id"], "output_index": 0,
                 "content_index": 0, "part": part},
            ])
        events.extend([
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {
                **response, "status": "completed", "output": [item],
                "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            }},
        ])
        text = "".join(f"event: {e['type']}\ndata: {json.dumps({**e, 'sequence_number': i})}\n\n"
                       for i, e in enumerate(events))
        return web.Response(text=text, content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/{tail:.*}", model)
    server = web.AppRunner(app)
    await server.setup()
    await web.TCPSite(server, "127.0.0.1", 0).start()
    settings = Settings(_env_file=None, COPILOT_HARNESS_TIMEOUT_SECONDS=30,
                        NORTHWIND_BUSINESS_DOMAIN=domain,
                        FOUNDRY_PROJECT_ENDPOINT=f"http://127.0.0.1:{server.addresses[0][1]}/api/projects/northwind")
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    specialist = AsyncMock(return_value=SimpleNamespace(
        status="completed", output_text="Intent: order status", output=[], usage=None))
    monkeypatch.setattr(tools, "invoke_prompt_agent_once", specialist)
    r = runtime.HarnessRuntime()
    try:
        await r.start()
        await r.credential.close()
        credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="local-test-token")),
                                     close=AsyncMock())
        r.credential = credential
        events = [e async for e in r.run("harness-local-contract", "wf-harness-local-contract",
                                         "Retrieve CLM-2001." if domain == "insurance" else "Classify my order request.",
                                         RunStore())]
        assert events[-1].text.startswith(final_text)
        if domain == "insurance":
            assert "NW-AUTO-WA-2026-v1#collision" in events[-1].text
            assert "No claim decision" in events[-1].text
        assert specialist.await_count == (0 if domain == "insurance" else 1)
        assert any(e.kind == "tool_result" and e.tool == selected_tool for e in events)
        assert len(requests) == 2
        assert all(path == "/api/projects/northwind/openai/v1/responses" for path, _, _ in requests)
        assert all(auth == "Bearer local-test-token" for _, auth, _ in requests)
        assert credential.get_token.await_count >= 2
    finally:
        await r.stop()
        await server.cleanup()
