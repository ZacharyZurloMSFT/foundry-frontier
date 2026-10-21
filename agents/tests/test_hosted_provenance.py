import asyncio
import hashlib
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from azure.ai.agentserver.responses import InMemoryResponseProvider

from agents.hosted import config
from agents.hosted.main import OrdersResponsesHost
from agents.hosted.runtime import build_agent
from agents.tests.test_hosted import MemoryProvider, MockProject, TRIAGE, message, model_events, settings
from scripts.orders_agent_package import bundle


def test_packaged_runtime_verifies_every_source_byte_before_reporting_provenance(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    archive = tmp_path / "agent.zip"
    bundle(root, root / "agents" / "hosted" / "requirements.txt", archive)
    staged = tmp_path / "staged"
    with zipfile.ZipFile(archive) as source:
        source.extractall(staged)
    monkeypatch.setattr(config, "AGENTS_DIR", staged / "agents")
    raw = (staged / "agents" / "hosted" / "build.json").read_bytes()
    metadata = config.source_metadata()
    assert metadata["source_manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    assert metadata["source_commit"] == json.loads(raw)["source_commit"]
    prompt = staged / "agents" / "insurance" / "triage.yaml"
    prompt.write_bytes(prompt.read_bytes() + b"\n# changed after packaging\n")
    with pytest.raises(ValueError, match="differs from its packaged build manifest"):
        config.source_metadata()


def test_completed_response_readback_preserves_observed_runtime_metadata(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    async def run():
        configuration = settings()
        project = MockProject(lambda *_: model_events([message(json.dumps(TRIAGE))]))
        account = project.get_openai_client(account=True)
        agent = build_agent(configuration, project, account_client=account)
        server = OrdersResponsesHost(
            agent, configuration, store=InMemoryResponseProvider(),
            agent_session_store_provider=MemoryProvider(), function_approval_store_provider=MemoryProvider(),
        )
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://unit.test") as client:
                completed = (await client.post("/responses", json={
                    "input": "Classify", "metadata": {"workflow_id": "wf-readback"},
                })).json()
                assert completed["status"] == "completed"
                readback = await client.get("/responses/" + completed["id"])
                assert readback.status_code == 200
                assert readback.json()["metadata"] == completed["metadata"]
                assert completed["metadata"]["prompt_sha256"] == configuration.prompt_sha256
                assert completed["metadata"]["config_sha256"] == configuration.metadata["config_sha256"]
        finally:
            await server._cleanup_agent()
            await project.close()
    asyncio.run(run())
