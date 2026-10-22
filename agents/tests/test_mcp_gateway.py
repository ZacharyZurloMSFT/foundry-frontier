"""Provider-executed MCP through the AI gateway: strict config, key handling and the work-order exception."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agents.hosted import main as hosted_main
from agents.hosted import tools
from agents.hosted.config import ENV_KEYS, Settings
from agents.hosted.runtime import (
    build_agent,
    knowledge_connection_headers,
    mcp_connection_headers,
)
from agents.hosted.scope import RequestScope, current_scope
from agents.insurance.domain import ClaimActionDraft
from agents.tests.test_hosted import (
    BASE,
    DRAFT,
    MockProject,
    function,
    hosted_request,
    message,
    model_events,
    settings,
)

MODEL_GATEWAY = {
    "ORDERS_MODEL_GATEWAY_ENDPOINT": "https://gateway.example/default/models/openai/v1/",
    "ORDERS_MODEL_GATEWAY_MODEL": "northwind/baseline",
    "ORDERS_MODEL_GATEWAY_DEPLOYMENT": "baseline-deployment",
}
SERVERS = "https://gateway.example/default/toolservers"
RETAIL_LOOKUP = f"{SERVERS}/northwind-order-lookup/mcp"
INSURANCE_LOOKUP = f"{SERVERS}/northwind-insurance-lookup/mcp"
RETAIL_KB = f"{SERVERS}/northwind-orders-knowledge/mcp"
INSURANCE_KB = f"{SERVERS}/northwind-insurance-knowledge/mcp"
WORK_ORDER = f"{SERVERS}/northwind-work-order/mcp"
RUNTIME_KEY = "unit-gateway-runtime-key"
CONNECTION = "unit-gateway-connection"


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)


def entry(url, prefix, **extra):
    return {"url": url, "tool_prefix": prefix, "connection": CONNECTION, **extra}


def mcp_settings(role, entries=None, domain="retail", **env):
    if entries is not None:
        env["ORDERS_MCP_GATEWAY"] = entries if isinstance(entries, str) else json.dumps(entries)
    return settings(role, NORTHWIND_BUSINESS_DOMAIN=domain, **env)


def work_order_settings(domain="retail", **env):
    return mcp_settings("resolutiondraft", {"resolutiondraft": entry(WORK_ORDER, "wo_", forwards_headers=True)},
                        domain, **env)


def connection_for(config, **changes):
    spec = config.knowledge_spec()["url"] if config.role == "policylookup" else config.mcp_spec()["server_url"]
    return {"type": "RemoteTool", "target": spec,
            "credentials": {"type": "CustomKeys", "api-key": RUNTIME_KEY}, **changes}


def test_unset_or_empty_gateway_keeps_every_direct_route():
    for overrides in ({}, {"ORDERS_MCP_GATEWAY": ""}, {"ORDERS_MCP_GATEWAY": "  "}, {"ORDERS_MCP_GATEWAY": "{}"}):
        for role in ("orderlookup", "policylookup", "resolutiondraft"):
            config = settings(role, **overrides)
            assert config.mcp_entry is None and config.mcp_route == "direct"
            assert config.metadata["mcp_route"] == "direct"
            assert config.mcp_connection is None
        assert settings("orderlookup", **overrides).mcp_spec() == {
            "type": "mcp", "server_label": "northwind-order-lookup", "server_url": BASE["MCP_TECH_LOOKUP_URL"],
            "require_approval": "never", "allowed_tools": ["lookup_order"],
        }
        assert settings("policylookup", **overrides).knowledge_spec() == {
            "name": "northwind-kb",
            "url": "https://search.example/knowledgebases/northwind-kb/mcp?api-version=2026-05-01-preview",
            "project_connection_id": "northwind-kb-mcp",
            "allowed_tools": ["knowledge_base_retrieve"],
            "approval_mode": "never_require",
        }
        assert settings("resolutiondraft", **overrides).mcp_spec()["server_url"] == BASE["MCP_WORK_ORDER_URL"]
    for role in ("manager", "triage"):
        assert "mcp_route" not in settings(role).metadata


def test_valid_entries_select_the_gateway_route_for_their_role_only():
    entries = {"orderlookup": entry(RETAIL_LOOKUP, "lookup_"), "policylookup": entry(RETAIL_KB, "knowledge_")}
    lookup = mcp_settings("orderlookup", entries)
    assert lookup.mcp_entry.url == RETAIL_LOOKUP and lookup.mcp_entry.server == "northwind-order-lookup"
    assert lookup.mcp_route == "ai-gateway" and lookup.metadata["mcp_route"] == "ai-gateway"
    assert lookup.mcp_connection == CONNECTION
    policy = mcp_settings("policylookup", entries)
    assert policy.mcp_route == "ai-gateway" and policy.mcp_connection == CONNECTION
    draft = mcp_settings("resolutiondraft", entries)
    assert draft.mcp_route == "direct" and draft.mcp_spec()["server_url"] == BASE["MCP_WORK_ORDER_URL"]
    assert "mcp_route" not in mcp_settings("manager", entries).metadata
    assert mcp_settings("orderlookup", {"orderlookup": entry(RETAIL_LOOKUP, "")}).mcp_entry.tool_prefix == ""


def entries_with(role, **changes):
    values = entry(RETAIL_LOOKUP, "lookup_")
    values.update(changes)
    return {role: {key: value for key, value in values.items() if value is not ...}}


INVALID_GATEWAY_CONFIGURATION = {
    "malformed json": ("{not json", "JSON object"),
    "array": ("[]", "JSON object"),
    "string": ('"entries"', "JSON object"),
    "null": ("null", "JSON object"),
    "manager key": ({"manager": entry(RETAIL_LOOKUP, "lookup_")}, "supports only"),
    "triage key": ({"triage": entry(RETAIL_LOOKUP, "lookup_")}, "supports only"),
    "unknown key": ({"unknown": entry(RETAIL_LOOKUP, "lookup_")}, "supports only"),
    "entry is not an object": ({"orderlookup": RETAIL_LOOKUP}, "must be a JSON object"),
    "smuggled credential field": (entries_with("orderlookup", **{"api-key": RUNTIME_KEY}), "unsupported fields"),
    "missing url": (entries_with("orderlookup", url=...), "url is required"),
    "url is not text": (entries_with("orderlookup", url=7), "url is required"),
    "http": (entries_with("orderlookup", url=RETAIL_LOOKUP.replace("https", "http")), "HTTPS"),
    "url credentials": (entries_with("orderlookup", url=RETAIL_LOOKUP.replace("//", "//user:secret@")), "HTTPS"),
    "url fragment": (entries_with("orderlookup", url=RETAIL_LOOKUP + "#frag"), "HTTPS"),
    "url query": (entries_with("orderlookup", url=RETAIL_LOOKUP + "?key=value"), "without a query"),
    "path does not end in mcp": (entries_with("orderlookup", url=f"{SERVERS}/northwind-order-lookup/sse"), "toolservers"),
    "trailing slash": (entries_with("orderlookup", url=RETAIL_LOOKUP + "/"), "toolservers"),
    "no toolservers segment": (entries_with("orderlookup", url="https://gateway.example/mcp"), "toolservers"),
    "other namespace": (entries_with("orderlookup", url="https://gateway.example/default/tools/x/mcp"), "toolservers"),
    "empty server name": (entries_with("orderlookup", url=f"{SERVERS}//mcp"), "toolservers"),
    "nested server name": (entries_with("orderlookup", url=f"{SERVERS}/a/b/mcp"), "toolservers"),
    "empty connection": (entries_with("orderlookup", connection=""), "connection"),
    "missing connection": (entries_with("orderlookup", connection=...), "connection"),
    "connection is a path": (entries_with("orderlookup", connection="projects/p/connections/c"), "connection"),
    "connection with whitespace": (entries_with("orderlookup", connection="a b"), "connection"),
    "uppercase prefix": (entries_with("orderlookup", tool_prefix="Lookup_"), "tool_prefix"),
    "hyphen prefix": (entries_with("orderlookup", tool_prefix="lookup-"), "tool_prefix"),
    "dotted prefix": (entries_with("orderlookup", tool_prefix="lookup."), "tool_prefix"),
    "numeric prefix": (entries_with("orderlookup", tool_prefix=5), "tool_prefix"),
    "missing prefix": (entries_with("orderlookup", tool_prefix=...), "tool_prefix"),
    "forwards_headers is not a boolean": (entries_with("orderlookup", forwards_headers="true"), "boolean"),
}


@pytest.mark.parametrize("raw,match", INVALID_GATEWAY_CONFIGURATION.values(), ids=INVALID_GATEWAY_CONFIGURATION)
def test_invalid_gateway_configuration_fails_closed(raw, match):
    with pytest.raises(ValueError, match=match) as failure:
        mcp_settings("orderlookup", raw)
    assert RUNTIME_KEY not in str(failure.value) and "secret" not in str(failure.value)


def test_an_invalid_shared_value_fails_every_role_not_only_the_mcp_roles():
    bad = {"orderlookup": entry("http://gateway.example/default/toolservers/x/mcp", "x_")}
    for role in ("manager", "triage", "orderlookup", "policylookup", "resolutiondraft"):
        with pytest.raises(ValueError, match="HTTPS"):
            mcp_settings(role, bad)


@pytest.mark.parametrize("extra", [
    {}, {"forwards_headers": False}, {"forwards_headers": None}, {"forwards_headers": "true"},
    {"forwards_headers": 1},
], ids=["absent", "false", "null", "string", "number"])
def test_work_order_entry_is_rejected_unless_header_forwarding_is_exactly_true(extra):
    entries = {"resolutiondraft": entry(WORK_ORDER, "wo_", **extra)}
    for role in ("resolutiondraft", "manager"):
        with pytest.raises(ValueError, match="application-bound headers that the AI gateway does not forward"):
            mcp_settings(role, entries)


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "YES", " yes "])
@pytest.mark.parametrize("role", ["orderlookup", "policylookup"])
def test_mcp_required_demands_a_gateway_entry_for_the_provider_executed_roles(role, value):
    other = "policylookup" if role == "orderlookup" else "orderlookup"
    wrong_role = {other: entry(RETAIL_KB if other == "policylookup" else RETAIL_LOOKUP,
                               "knowledge_" if other == "policylookup" else "lookup_")}
    for entries in (None, {}, wrong_role):
        with pytest.raises(ValueError, match=f"AI_GATEWAY_MCP_REQUIRED.*{role}"):
            mcp_settings(role, entries, AI_GATEWAY_MCP_REQUIRED=value)
    own = {role: entry(RETAIL_KB if role == "policylookup" else RETAIL_LOOKUP,
                       "knowledge_" if role == "policylookup" else "lookup_")}
    assert mcp_settings(role, own, AI_GATEWAY_MCP_REQUIRED=value).mcp_route == "ai-gateway"


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_mcp_required_leaves_other_roles_and_the_work_order_direct(value):
    for role in ("manager", "triage", "resolutiondraft"):
        config = mcp_settings(role, None, AI_GATEWAY_MCP_REQUIRED=value)
        assert config.metadata.get("mcp_route", "direct") == "direct"
    assert mcp_settings("resolutiondraft", None, AI_GATEWAY_MCP_REQUIRED=value).mcp_spec()["server_url"] == (
        BASE["MCP_WORK_ORDER_URL"])


@pytest.mark.parametrize("value", [None, "", "  ", "false", "FALSE", "0", "no", "off"])
def test_mcp_required_off_values_keep_direct_mcp(value):
    env = {} if value is None else {"AI_GATEWAY_MCP_REQUIRED": value}
    for role in ("orderlookup", "policylookup"):
        assert settings(role, **env).mcp_route == "direct"


@pytest.mark.parametrize("name", ["AI_GATEWAY_MCP_REQUIRED", "AI_GATEWAY_REQUIRED"])
@pytest.mark.parametrize("value", ["maybe", "enabled", "on", "ture", "2"])
def test_a_mistyped_enforcement_flag_is_an_error_not_a_silent_off(name, value):
    with pytest.raises(ValueError, match=name):
        settings("orderlookup", **{name: value})


def test_gateway_order_lookup_prefixes_the_gateway_tools_not_the_canonical_prompt():
    retail = mcp_settings("orderlookup", {"orderlookup": entry(RETAIL_LOOKUP, "lookup_")}).mcp_spec()
    assert retail["server_url"] == RETAIL_LOOKUP
    assert retail["allowed_tools"] == ["lookup_lookup_order"]
    assert retail["require_approval"] == "never" and retail["server_label"] == "northwind-order-lookup"
    insurance = mcp_settings("orderlookup", {"orderlookup": entry(INSURANCE_LOOKUP, "lookup_")},
                             "insurance").mcp_spec()
    assert insurance["server_url"] == INSURANCE_LOOKUP
    assert insurance["allowed_tools"] == ["lookup_lookup_policy", "lookup_lookup_claim"]
    assert insurance["server_label"] == "northwind-insurance-policy-claim-lookup"
    bare = mcp_settings("orderlookup", {"orderlookup": entry(RETAIL_LOOKUP, "")}).mcp_spec()
    assert bare["allowed_tools"] == ["lookup_order"]


def test_gateway_order_lookup_still_validates_the_canonical_prompt():
    config = mcp_settings("orderlookup", {"orderlookup": entry(RETAIL_LOOKUP, "lookup_")})
    for allowed in (["lookup_lookup_order"], ["lookup_order", "extra"], []):
        prompt = copy.deepcopy(config.prompt)
        prompt["tools"][0]["allowed_tools"] = allowed
        with pytest.raises(ValueError, match="must allow only"):
            replace(config, prompt=prompt).mcp_spec()
    prompt = copy.deepcopy(config.prompt)
    prompt["tools"][0]["require_approval"] = "always"
    with pytest.raises(ValueError, match="never auto-approve"):
        replace(config, prompt=prompt).mcp_spec()


def test_gateway_knowledge_spec_replaces_the_project_connection_with_the_gateway():
    for domain, url, label in (("retail", RETAIL_KB, "northwind-orders-knowledge"),
                               ("insurance", INSURANCE_KB, "northwind-insurance-knowledge")):
        config = mcp_settings("policylookup", {"policylookup": entry(url, "knowledge_")}, domain)
        assert config.knowledge_spec() == {
            "name": label, "url": url,
            "allowed_tools": ["knowledge_knowledge_base_retrieve"],
            "approval_mode": "never_require",
        }


def test_gateway_knowledge_does_not_depend_on_direct_search_configuration():
    env = {key: value for key, value in BASE.items() if not key.startswith(("AZURE_SEARCH", "ORDERS_KB"))}
    config = Settings.from_env({**env, "ORDER_AGENT_ROLE": "policylookup", "NORTHWIND_BUSINESS_DOMAIN": "insurance",
                                "ORDERS_MCP_GATEWAY": json.dumps({"policylookup": entry(INSURANCE_KB, "knowledge_")})})
    assert config.knowledge_spec()["url"] == INSURANCE_KB


@pytest.mark.parametrize("domain,server", [
    ("insurance", "northwind-orders-knowledge"),
    ("retail", "northwind-insurance-knowledge"),
    ("insurance", "northwind-claims-knowledge"),
    ("retail", "northwind-hr-knowledge"),
    ("insurance", "evil-northwind-insurance-knowledge"),
    ("insurance", "northwind-insurance-knowledge-v2"),
    ("retail", "northwind-orders-knowledge2"),
])
def test_knowledge_domains_cannot_be_substituted_through_the_gateway(domain, server):
    with pytest.raises(ValueError, match="cannot be substituted"):
        mcp_settings("policylookup", {"policylookup": entry(f"{SERVERS}/{server}/mcp", "knowledge_")}, domain)


def test_direct_insurance_knowledge_guard_still_applies_without_a_gateway_entry():
    with pytest.raises(ValueError, match="retail knowledge cannot be substituted"):
        mcp_settings("policylookup", None, "insurance").knowledge_spec()


@pytest.mark.parametrize("role,domain,url,prefix", [
    ("orderlookup", "retail", RETAIL_LOOKUP, "lookup_"),
    ("orderlookup", "insurance", INSURANCE_LOOKUP, "lookup_"),
    ("policylookup", "retail", RETAIL_KB, "knowledge_"),
    ("policylookup", "insurance", INSURANCE_KB, "knowledge_"),
])
def test_connection_credential_is_bound_to_the_gateway_url(role, domain, url, prefix):
    config = mcp_settings(role, {role: entry(url, prefix)}, domain)
    good = connection_for(config)
    assert good["target"] == url
    assert mcp_connection_headers(config, good) == {"api-key": RUNTIME_KEY}
    assert knowledge_connection_headers(config, good) == {"api-key": RUNTIME_KEY}
    changed = (
        {"type": "Other"}, {"type": None}, {"target": "https://wrong.example/mcp"}, {"target": url + "/"},
        {"target": url + "?x=1"}, {"target": "https://search.example/knowledgebases/northwind-kb/mcp"},
        {"target": None}, {"credentials": None}, {"credentials": "unit"},
        {"credentials": {"type": "ApiKey", "api-key": RUNTIME_KEY}},
        {"credentials": {"type": "CustomKeys"}},
        {"credentials": {"type": "CustomKeys", "api-key": ""}},
        {"credentials": {"type": "CustomKeys", "api-key": 7}},
        {"credentials": {"type": "CustomKeys", "api_key": RUNTIME_KEY}},
    )
    for change in changed:
        with pytest.raises(ValueError) as failure:
            mcp_connection_headers(config, {**good, **change})
        assert RUNTIME_KEY not in str(failure.value)


def test_a_direct_search_connection_cannot_satisfy_a_gateway_entry_and_the_reverse():
    gateway = mcp_settings("policylookup", {"policylookup": entry(RETAIL_KB, "knowledge_")})
    direct = settings("policylookup")
    direct_connection = connection_for(direct)
    assert direct_connection["target"].startswith("https://search.example/knowledgebases/")
    with pytest.raises(ValueError, match="canonical RemoteTool endpoint"):
        mcp_connection_headers(gateway, direct_connection)
    with pytest.raises(ValueError, match="canonical RemoteTool endpoint"):
        mcp_connection_headers(direct, connection_for(gateway))


def test_roles_without_a_resolvable_connection_cannot_load_credentials():
    for config in (settings("orderlookup"), settings("manager"), settings("triage"),
                   work_order_settings(), mcp_settings("resolutiondraft")):
        with pytest.raises(ValueError, match="no MCP connection credential"):
            mcp_connection_headers(config, connection_for(settings("policylookup")))


def test_the_runtime_key_never_reaches_settings_metadata_logs_or_errors():
    config = mcp_settings("policylookup", {"policylookup": entry(RETAIL_KB, "knowledge_")}, **MODEL_GATEWAY)
    headers = mcp_connection_headers(config, connection_for(config))
    assert headers == {"api-key": RUNTIME_KEY}
    for surface in (json.dumps(config.metadata), json.dumps(dict(config.env)), repr(config), str(config.knowledge_spec())):
        assert RUNTIME_KEY not in surface


def test_connection_name_resolution_follows_the_role_and_the_route():
    gateway = {"orderlookup": entry(RETAIL_LOOKUP, "lookup_"), "policylookup": entry(RETAIL_KB, "knowledge_")}
    assert mcp_settings("orderlookup", gateway).mcp_connection == CONNECTION
    assert mcp_settings("policylookup", gateway).mcp_connection == CONNECTION
    assert work_order_settings().mcp_connection is None
    assert settings("policylookup", **MODEL_GATEWAY).mcp_connection == "northwind-kb-mcp"
    for config in (settings("policylookup"), settings("orderlookup"), settings("orderlookup", **MODEL_GATEWAY),
                   settings("manager"), settings("triage")):
        assert config.mcp_connection is None


class FakeConnections:
    def __init__(self, calls, connection):
        self.calls, self.connection = calls, connection

    def get(self, name, **options):
        self.calls.append((name, options))
        return SimpleNamespace(as_dict=lambda: self.connection)


@contextmanager
def fake_connection_clients(monkeypatch, connection):
    seen = SimpleNamespace(credentials=[], projects=[], gets=[])

    class Credential:
        def __init__(self, **options):
            seen.credentials.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Project:
        def __init__(self, **options):
            seen.projects.append(options)
            self.connections = FakeConnections(seen.gets, connection)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(hosted_main, "ConnectionCredential", Credential)
    monkeypatch.setattr(hosted_main, "ConnectionProjectClient", Project)
    yield seen


@pytest.mark.parametrize("role,url,prefix", [
    ("orderlookup", RETAIL_LOOKUP, "lookup_"), ("policylookup", RETAIL_KB, "knowledge_"),
])
def test_startup_reads_the_gateway_connection_under_the_agent_identity(monkeypatch, role, url, prefix):
    config = mcp_settings(role, {role: entry(url, prefix)}, AZURE_CLIENT_ID="unit-client-id")
    with fake_connection_clients(monkeypatch, connection_for(config)) as seen:
        assert hosted_main.load_mcp_headers(config) == {"api-key": RUNTIME_KEY}
    assert seen.credentials == [{"managed_identity_client_id": "unit-client-id",
                                 "exclude_interactive_browser_credential": True}]
    assert seen.projects == [{"endpoint": BASE["FOUNDRY_PROJECT_ENDPOINT"], "credential": seen.projects[0]["credential"],
                              "allow_preview": True}]
    assert seen.gets == [(CONNECTION, {"include_credentials": True})]


def test_startup_rejects_a_connection_that_does_not_front_the_gateway_url(monkeypatch):
    config = mcp_settings("orderlookup", {"orderlookup": entry(RETAIL_LOOKUP, "lookup_")})
    wrong = connection_for(config, target=INSURANCE_LOOKUP)
    with fake_connection_clients(monkeypatch, wrong), pytest.raises(
            ValueError, match="canonical RemoteTool endpoint") as failure:
        hosted_main.load_mcp_headers(config)
    assert RUNTIME_KEY not in str(failure.value)


def test_startup_keeps_the_existing_search_connection_and_skips_every_other_role(monkeypatch):
    policy = settings("policylookup", **MODEL_GATEWAY)
    with fake_connection_clients(monkeypatch, connection_for(policy)) as seen:
        assert hosted_main.load_mcp_headers(policy) == {"api-key": RUNTIME_KEY}
    assert seen.gets == [("northwind-kb-mcp", {"include_credentials": True})]
    for config in (settings("policylookup"), settings("orderlookup"), settings("manager"), settings("triage"),
                   work_order_settings(), mcp_settings("orderlookup", None, **MODEL_GATEWAY)):
        with fake_connection_clients(monkeypatch, {}) as seen:
            assert hosted_main.load_mcp_headers(config) is None
        assert not (seen.credentials or seen.projects or seen.gets)


@pytest.mark.parametrize("model_gateway", [True, False], ids=["gateway-model", "direct-model"])
@pytest.mark.parametrize("role,domain,url,prefix,tools", [
    ("orderlookup", "retail", RETAIL_LOOKUP, "lookup_", ["lookup_lookup_order"]),
    ("orderlookup", "insurance", INSURANCE_LOOKUP, "lookup_", ["lookup_lookup_policy", "lookup_lookup_claim"]),
    ("policylookup", "retail", RETAIL_KB, "knowledge_", ["knowledge_knowledge_base_retrieve"]),
    ("policylookup", "insurance", INSURANCE_KB, "knowledge_", ["knowledge_knowledge_base_retrieve"]),
])
def test_hosted_request_sends_the_gateway_tool_server_to_the_provider(role, domain, url, prefix, tools, model_gateway):
    async def run():
        sent = []

        def inference(name, body):
            sent.append(body)
            native = body["tools"][0]
            return model_events([
                {"id": "mcp_gateway", "type": "mcp_call", "name": tools[0], "server_label": native["server_label"],
                 "arguments": '{"query":"shipping"}', "status": "completed",
                 "output": '{"answer":"Seven days","references":[{"title":"Shipping"}]}'},
                message("Seven days [Shipping]."),
            ])

        env = MODEL_GATEWAY if model_gateway else {}
        config = mcp_settings(role, {role: entry(url, prefix)}, domain, **env)
        result = await hosted_request(config, MockProject(inference), {"stream": False},
                                      mcp_headers={"api-key": RUNTIME_KEY})
        assert result["status"] == "completed", result.get("error")
        assert len(sent[0]["tools"]) == 1
        native = sent[0]["tools"][0]
        assert native["type"] == "mcp"
        assert native["server_url"] == url
        assert native["allowed_tools"] == tools
        assert native["headers"] == {"api-key": RUNTIME_KEY}
        assert native["require_approval"] == "never"
        assert "project_connection_id" not in native
        assert result["metadata"]["mcp_route"] == "ai-gateway"
        assert RUNTIME_KEY not in json.dumps(result["metadata"])
        assert any(item["type"] == "mcp_call" and item["name"] == tools[0] for item in result["output"])
    asyncio.run(run())


@pytest.mark.parametrize("role,url,prefix", [
    ("orderlookup", RETAIL_LOOKUP, "lookup_"), ("policylookup", RETAIL_KB, "knowledge_"),
])
def test_gateway_mcp_cannot_run_without_its_connection_credential(role, url, prefix):
    project = MockProject(lambda *_: [])
    try:
        config = mcp_settings(role, {role: entry(url, prefix)})
        with pytest.raises(ValueError, match="connection headers"):
            build_agent(config, project)
        with pytest.raises(ValueError, match="connection headers"):
            build_agent(config, project, mcp_headers={})
    finally:
        asyncio.run(project.close())


def test_the_legacy_headers_keyword_still_carries_a_gateway_credential():
    project = MockProject(lambda *_: [])
    try:
        config = mcp_settings("orderlookup", {"orderlookup": entry(RETAIL_LOOKUP, "lookup_")})
        agent = build_agent(config, project, knowledge_headers={"api-key": RUNTIME_KEY})
        assert agent.default_options["tools"][0]["headers"] == {"api-key": RUNTIME_KEY}
    finally:
        asyncio.run(project.close())


def test_direct_mcp_never_receives_a_gateway_key():
    project = MockProject(lambda *_: [])
    try:
        agent = build_agent(settings("orderlookup"), project, mcp_headers={"api-key": RUNTIME_KEY})
        assert "headers" not in agent.default_options["tools"][0]
        assert agent.default_options["tools"][0]["server_url"] == BASE["MCP_TECH_LOOKUP_URL"]
        policy = build_agent(settings("policylookup"), project, mcp_headers={"api-key": RUNTIME_KEY})
        native = policy.default_options["tools"][0]
        assert native["project_connection_id"] == "northwind-kb-mcp" and "headers" not in native
    finally:
        asyncio.run(project.close())


def test_work_order_stays_direct_unless_it_explicitly_opts_in():
    for env in ({}, {"AI_GATEWAY_MCP_REQUIRED": "true"}, {"AI_GATEWAY_REQUIRED": "true", **MODEL_GATEWAY}):
        config = settings("resolutiondraft", **env)
        assert config.mcp_route == "direct" and config.metadata["mcp_route"] == "direct"
        assert config.mcp_spec()["server_url"] == BASE["MCP_WORK_ORDER_URL"]
    retail = work_order_settings()
    assert retail.mcp_route == "ai-gateway" and retail.metadata["mcp_route"] == "ai-gateway"
    assert retail.mcp_spec()["server_url"] == WORK_ORDER
    assert retail.mcp_spec()["allowed_tools"] == ["wo_propose_resolution"]
    insurance = work_order_settings("insurance")
    assert insurance.mcp_spec()["allowed_tools"] == ["wo_draft_claim_action"]


def test_work_order_gateway_requires_an_identity_token_provider():
    with pytest.raises(ValueError, match="token provider"):
        tools.proposal_tool(work_order_settings())
    project = MockProject(lambda *_: [])
    try:
        with pytest.raises(ValueError, match="token provider"):
            build_agent(work_order_settings(), project)
        assert build_agent(work_order_settings(), project, gateway_token_provider=AsyncMock())
    finally:
        asyncio.run(project.close())


INSURANCE_DRAFT = ClaimActionDraft(
    claim_id="CLM-2001", policy_id="POL-1001", policyholder_id="PH-1001",
    action_type="request_evidence", summary="Request photos.", policy_refs=[])


@pytest.mark.parametrize("domain,tool_name,wire_name,draft", [
    ("retail", "propose_resolution", "wo_propose_resolution", DRAFT),
    ("insurance", "draft_claim_action", "wo_draft_claim_action", INSURANCE_DRAFT.model_dump(exclude_none=True)),
])
def test_work_order_opt_in_calls_the_gateway_with_the_prefixed_tool_and_entra_bearer(monkeypatch, domain, tool_name,
                                                                                     wire_name, draft):
    async def run():
        provider = AsyncMock(return_value="unit-entra-token")
        persisted = {"status": "pending_approval", "hitl_id": "saved", "workflow_id": "wf-owned", "draft": draft}
        if domain == "insurance":
            persisted.update(kind="insurance_claim_action", synthetic=True, execution_status="not_executed")
        mcp = AsyncMock(return_value=persisted)
        monkeypatch.setattr(tools, "call_proposal_mcp", mcp)
        proposal = tools.proposal_tool(work_order_settings(domain), token_provider=provider)
        token = current_scope.set(RequestScope(workflow_id="wf-owned"))
        try:
            await proposal.invoke(arguments={"draft": draft})
        finally:
            current_scope.reset(token)
        assert proposal.name == tool_name
        mcp.assert_awaited_once()
        url, workflow, _payload, name = mcp.await_args.args
        assert (url, workflow, name) == (WORK_ORDER, "wf-owned", wire_name)
        request = httpx.Request("POST", WORK_ORDER)
        await mcp.await_args.kwargs["auth"].async_auth_flow(request).__anext__()
        assert request.headers["authorization"] == "Bearer unit-entra-token"
    asyncio.run(run())


def test_work_order_gateway_call_reaches_the_wire_with_bearer_and_application_headers(monkeypatch):
    requests = []

    def gateway(request):
        requests.append(request)
        if request.method != "POST":
            return httpx.Response(405)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            result = {"protocolVersion": payload["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "Azure API Management", "version": "1"}}
        elif payload["method"] == "tools/list":
            result = {"tools": [{"name": "wo_propose_resolution", "inputSchema": {"type": "object"}}]}
        else:
            pending = {"status": "pending_approval", "hitl_id": "saved", "workflow_id": "wf-harness-owned",
                       "draft": payload["params"]["arguments"]["draft"]}
            result = {"content": [{"type": "text", "text": json.dumps(pending)}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(tools.httpx, "AsyncClient",
                        lambda **options: real_client(**options, transport=httpx.MockTransport(gateway)))

    async def run():
        provider = AsyncMock(return_value="unit-entra-token")
        result = await tools.call_proposal_mcp(
            WORK_ORDER, "wf-harness-owned", DRAFT, "wo_propose_resolution", auth=tools.GatewayBearerAuth(provider))
        assert result["status"] == "pending_approval"
        calls = [json.loads(r.content) for r in requests if r.method == "POST"]
        assert calls[0]["method"] == "initialize" and calls[1]["method"] == "notifications/initialized"
        invocation = next(c for c in calls if c["method"] == "tools/call")
        assert invocation["params"] == {"name": "wo_propose_resolution",
                                        "arguments": {"workflow_id": "wf-harness-owned", "draft": DRAFT}}
        assert all(str(r.url) == WORK_ORDER for r in requests)
        assert all(r.headers["authorization"] == "Bearer unit-entra-token" for r in requests)
        assert all(r.headers["x-orders-workflow"] == "wf-harness-owned" for r in requests)
        assert all(r.headers["x-harness-workflow"] == "wf-harness-owned" for r in requests)
    asyncio.run(run())


def test_direct_work_order_call_sends_no_bearer(monkeypatch):
    seen = {}

    @asynccontextmanager
    async def transport(url, *, http_client):
        seen["auth"] = http_client.auth
        seen["authorization"] = http_client.headers.get("authorization")
        yield (None, None, None)

    class Session:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def initialize(self):
            pass

        async def call_tool(self, name, arguments):
            seen["name"] = name
            return SimpleNamespace(isError=False, content=[], structuredContent={"result": json.dumps({
                "status": "pending_approval", "hitl_id": "real",
                "workflow_id": arguments["workflow_id"], "draft": arguments["draft"]})})

    monkeypatch.setattr(tools, "streamable_http_client", transport)
    monkeypatch.setattr(tools, "ClientSession", Session)
    asyncio.run(tools.call_proposal_mcp(BASE["MCP_WORK_ORDER_URL"], "wf-owned", DRAFT))
    assert seen == {"auth": None, "authorization": None, "name": "propose_resolution"}


def test_hosted_work_order_opt_in_persists_through_the_gateway_client(monkeypatch):
    async def run():
        provider = AsyncMock(return_value="unit-entra-token")

        def persisted(url, workflow, draft, name, **options):
            return {"status": "pending_approval", "workflow_id": workflow, "hitl_id": "hitl-real", "draft": draft}

        mcp = AsyncMock(side_effect=persisted)
        monkeypatch.setattr(tools, "call_proposal_mcp", mcp)
        count = 0

        def infer(name, body):
            nonlocal count
            count += 1
            return model_events([function("propose_resolution", {"draft": DRAFT})] if count == 1
                                else [message("The draft is awaiting human approval.")])

        result = await hosted_request(
            work_order_settings(), MockProject(infer), {"stream": True, "metadata": {"workflow_id": "wf-owned"}},
            gateway_token_provider=provider)
        assert result[-1]["type"] == "response.completed", result[-1]
        assert result[-1]["response"]["metadata"]["mcp_route"] == "ai-gateway"
        assert mcp.await_args.args[0] == WORK_ORDER and mcp.await_args.args[3] == "wo_propose_resolution"
    asyncio.run(run())


def run_main(monkeypatch, role, entries, connection, **env):
    """Run the real startup path with Azure clients replaced; returns the host, connection reads and scopes."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**BASE, **MODEL_GATEWAY, "ORDER_AGENT_ROLE": role, "AZURE_CLIENT_ID": "unit-client-id",
                       "ORDERS_MCP_GATEWAY": json.dumps(entries), **env}.items():
        monkeypatch.setenv(key, value)
    hosts, providers = [], {"main": [], "account": []}

    class Host:
        def __init__(self, agent, settings, *, resources=()):
            self.agent, self.settings, self.resources = agent, settings, resources
            hosts.append(self)

        def run(self, **options):
            self.options = options

    def provider_factory(name):
        def make(identity, scope):
            provider = AsyncMock(return_value="unit-entra-token")
            providers[name].append((provider, scope))
            return provider
        return make

    monkeypatch.setattr(hosted_main, "DefaultAzureCredential",
                        lambda **_: SimpleNamespace(close=AsyncMock(), get_token=AsyncMock()))
    monkeypatch.setattr(hosted_main, "AIProjectClient", lambda **_: SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(hosted_main, "OrdersResponsesHost", Host)
    monkeypatch.setattr(hosted_main, "get_bearer_token_provider", provider_factory("main"))
    monkeypatch.setattr("agents.hosted.runtime.get_bearer_token_provider", provider_factory("account"))
    with fake_connection_clients(monkeypatch, connection) as seen:
        hosted_main.main()
    return hosts[0], seen, providers


@pytest.mark.parametrize("role,url,prefix", [
    ("orderlookup", RETAIL_LOOKUP, "lookup_"), ("policylookup", RETAIL_KB, "knowledge_"),
])
def test_main_gives_the_provider_executed_agent_its_gateway_runtime_key(monkeypatch, role, url, prefix):
    entries = {role: entry(url, prefix)}
    config = mcp_settings(role, entries)
    host, seen, providers = run_main(monkeypatch, role, entries, connection_for(config),
                                     AI_GATEWAY_REQUIRED="true", AI_GATEWAY_MCP_REQUIRED="true")
    assert seen.gets == [(CONNECTION, {"include_credentials": True})]
    native = host.agent.default_options["tools"][0]
    assert native["server_url"] == url and native["headers"] == {"api-key": RUNTIME_KEY}
    assert RUNTIME_KEY not in json.dumps(host.settings.metadata)
    assert providers["main"] == []
    assert {scope for _, scope in providers["account"]} == {config.gateway_scope}


def test_main_gives_the_work_order_opt_in_the_agent_identity_not_a_stored_key(monkeypatch):
    async def run():
        entries = {"resolutiondraft": entry(WORK_ORDER, "wo_", forwards_headers=True)}
        host, seen, providers = run_main(monkeypatch, "resolutiondraft", entries, {}, AI_GATEWAY_REQUIRED="true")
        assert not (seen.credentials or seen.projects or seen.gets)
        (provider, scope), = providers["main"]
        assert scope == "https://azure-api.net/aigateway/.default"
        mcp = AsyncMock(return_value={"status": "pending_approval", "hitl_id": "saved",
                                      "workflow_id": "wf-owned", "draft": DRAFT})
        monkeypatch.setattr(tools, "call_proposal_mcp", mcp)
        token = current_scope.set(RequestScope(workflow_id="wf-owned"))
        try:
            await host.agent.default_options["tools"][0].invoke(arguments={"draft": DRAFT})
        finally:
            current_scope.reset(token)
        assert mcp.await_args.kwargs["auth"].token_provider is provider
    asyncio.run(run())
