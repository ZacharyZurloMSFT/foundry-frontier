import copy
import os
import sys
from pathlib import Path

import pytest
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceExistsError, CosmosResourceNotFoundError

os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Container:
    """In-memory stand-in for the Cosmos container calls the business store and the MCP servers make."""

    def __init__(self):
        self.items = {}
        self.etag = 0

    async def create_item(self, item):
        pk = item.get("workflowId") or item["sessionId"]
        key = (pk, item["id"])
        if key in self.items:
            raise CosmosResourceExistsError(status_code=409)
        self.etag += 1
        self.items[key] = copy.deepcopy({**item, "_etag": str(self.etag)})
        return copy.deepcopy(self.items[key])

    async def read_item(self, item, partition_key):
        if (partition_key, item) not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        return copy.deepcopy(self.items[(partition_key, item)])

    async def replace_item(self, name, item, *, etag, match_condition):
        key = (item["workflowId"], name)
        if self.items[key]["_etag"] != etag:
            raise CosmosHttpResponseError(status_code=412)
        self.etag += 1
        self.items[key] = copy.deepcopy({**item, "_etag": str(self.etag)})
        return copy.deepcopy(self.items[key])


@pytest.fixture
def database():
    return {name: Container() for name in ("workflow_state", "conversations", "hitl_queue")}
