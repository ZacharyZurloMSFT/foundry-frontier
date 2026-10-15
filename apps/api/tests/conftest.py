import os
import sys
from pathlib import Path

import pytest

os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def database(monkeypatch):
    from app.harness import store
    from test_harness import Container

    containers = {name: Container() for name in ("workflow_state", "conversations", "hitl_queue")}
    async def get(name):
        return containers[name]
    monkeypatch.setattr(store, "_container", get)
    return containers
