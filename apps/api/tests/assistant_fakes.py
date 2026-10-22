"""Offline doubles for assistant tests: a scripted Responses API and an owner-aware conversation container."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any


class ConversationContainer:
    """Cosmos-like: a partition query by session, narrowed by `@kind` when the query asks for a record kind."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    async def create_item(self, item: dict[str, Any]) -> dict[str, Any]:
        self.items.append(copy.deepcopy(item))
        return item

    def query_items(self, **kwargs: Any):
        session = kwargs["partition_key"]
        kind = {p["name"]: p["value"] for p in kwargs.get("parameters", [])}.get("@kind")

        async def rows():
            matching = (i for i in self.items if i["sessionId"] == session and (kind is None or i.get("record_kind") == kind))
            for row in sorted(matching, key=lambda r: r["ts"], reverse=True):
                yield copy.deepcopy(row)
        return rows()


def tool_call(name: str, arguments: str | dict[str, Any], call_id: str = "call-1") -> SimpleNamespace:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(type="function_call", name=name, arguments=text, call_id=call_id, id="fc_" + call_id)


def requests_tools(*calls: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(status="completed", output=list(calls), output_text="")


def answer(text: str, citations: tuple[str, ...] = (), human_review: bool = False) -> SimpleNamespace:
    return raw(json.dumps({"answer": text, "citations": list(citations), "human_review": human_review}))


def raw(text: str) -> SimpleNamespace:
    return SimpleNamespace(status="completed", output=[], output_text=text)


class ScriptedProject:
    """Stands in for `_project_client`: records every request and replays a fixed script of responses."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def __call__(self, *args: Any, **kwargs: Any) -> ScriptedProject:
        return self

    async def __aenter__(self) -> ScriptedProject:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def get_openai_client(self, **kwargs: Any) -> ScriptedProject:
        assert kwargs == {}
        return self

    async def _create(self, **kwargs: Any) -> Any:
        self.requests.append(copy.deepcopy(kwargs))
        if not self.script:
            raise AssertionError("The model was called more often than the test scripted.")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return await step(kwargs)
        return step
