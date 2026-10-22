"""Prompt-agent MCP tools authenticate through the API gateway only when explicitly configured."""
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("create_prompt_agents", ROOT / "scripts" / "create-prompt-agents.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

LOOKUP_CONNECTION = "northwind-apim-tech-lookup"
WORK_ORDER_CONNECTION = "northwind-apim-work-order"
GATEWAY = "https://apim-northwind.azure-api.net"
DIRECT_LOOKUP = "https://ca-mcp-tech-lookup.gentlemushroom.canadacentral.azurecontainerapps.io/mcp"
DIRECT_WORK_ORDER = "https://ca-mcp-work-order.gentlemushroom.canadacentral.azurecontainerapps.io/mcp"
CONNECTION_VARIABLES = ("MCP_GATEWAY_CONNECTION", "MCP_GATEWAY_CONNECTION_TECH_LOOKUP", "MCP_GATEWAY_CONNECTION_WORK_ORDER")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in (*CONNECTION_VARIABLES, "NORTHWIND_GATEWAY_URL", "MCP_TECH_LOOKUP_URL", "MCP_WORK_ORDER_URL"):
        monkeypatch.delenv(name, raising=False)


def configure_connections(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_TECH_LOOKUP", LOOKUP_CONNECTION)
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_WORK_ORDER", WORK_ORDER_CONNECTION)


def mcp(url, **extra):
    return {"name": "northwind-order-lookup", "tools": [{
        "type": "mcp", "server_label": "northwind-order-lookup", "server_url": url,
        "require_approval": "never", "allowed_tools": ["lookup_order"], **extra}]}


def entry(url, **extra):
    (resolved,) = module._tools_for_agent(mcp(url, **extra))
    return resolved


@pytest.mark.parametrize("url", [
    GATEWAY + "/northwind/mcp/tech-lookup",
    "https://apim-northwind.azure-api.net/insurance/mcp/work-order",
    "https://APIM-Northwind.Azure-API.net/northwind/mcp/tech-lookup",
    "https://apim-northwind.developer.azure-api.net/x",
    "https://apim-northwind.azure-api.net:443/northwind/mcp/work-order?api-version=1",
])
def test_azure_api_hosts_are_gateway_urls(url):
    assert module._is_gateway_url(url)


@pytest.mark.parametrize("url", [
    DIRECT_LOOKUP,
    "https://azure-api.net/northwind/mcp/work-order",
    "https://notazure-api.net/northwind/mcp/work-order",
    "https://apim-northwind.azure-api.net.evil.example/mcp",
    "https://apim-northwind.azure-api.net@evil.example/mcp",
    "https://evil.example/apim-northwind.azure-api.net/mcp",
    "https://evil.example/?next=https://apim-northwind.azure-api.net",
    "${MCP_WORK_ORDER_URL}",
    "https://[bad",
    "",
])
def test_other_hosts_are_not_gateway_urls(url):
    assert not module._is_gateway_url(url)


@pytest.mark.parametrize("configured,url,expected", [
    ("https://api.northwind.example", "https://api.northwind.example/northwind/mcp/tech-lookup", True),
    ("https://api.northwind.example/northwind/", "https://api.northwind.example/insurance/mcp/work-order", True),
    ("api.northwind.example", "https://api.northwind.example/northwind/mcp/work-order", True),
    ("api.northwind.example:8443/base", "https://api.northwind.example:8443/northwind/mcp/work-order", True),
    ("  https://API.Northwind.Example  ", "https://api.northwind.example/x", True),
    ("https://api.northwind.example", "https://other.northwind.example/northwind/mcp/work-order", False),
    ("https://api.northwind.example", "https://api.northwind.example.evil.test/mcp/work-order", False),
    ("https://api.northwind.example", DIRECT_WORK_ORDER, False),
    ("https://[bad", "https://api.northwind.example/x", False),
    ("", "https://api.northwind.example/x", False),
])
def test_custom_gateway_domain_comes_from_northwind_gateway_url(monkeypatch, configured, url, expected):
    monkeypatch.setenv("NORTHWIND_GATEWAY_URL", configured)
    assert module._is_gateway_url(url) is expected


def test_gateway_mcp_tool_uses_its_endpoints_connection_and_keeps_the_url(monkeypatch, capsys):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_TECH_LOOKUP", LOOKUP_CONNECTION)
    url = GATEWAY + "/northwind/mcp/tech-lookup"
    assert entry(url) == {
        "type": "mcp", "server_label": "northwind-order-lookup", "server_url": url,
        "require_approval": "never", "allowed_tools": ["lookup_order"], "project_connection_id": LOOKUP_CONNECTION,
    }
    output = capsys.readouterr().out
    assert LOOKUP_CONNECTION in output and "target must be exactly " + url in output


def test_each_mcp_endpoint_gets_its_own_connection_never_a_shared_one(monkeypatch):
    # Foundry sends a tool's calls to the TARGET of the connection it references (verified live: a base-URL
    # connection made Foundry call the base URL and get a 404), so one connection can never serve two endpoints.
    configure_connections(monkeypatch)
    lookup = entry(GATEWAY + "/insurance/mcp/tech-lookup")
    work_order = entry(GATEWAY + "/insurance/mcp/work-order")
    assert lookup["project_connection_id"] == LOOKUP_CONNECTION
    assert work_order["project_connection_id"] == WORK_ORDER_CONNECTION
    assert lookup["project_connection_id"] != work_order["project_connection_id"]


def test_the_generic_hosted_agent_variable_is_never_attached_to_prompt_agent_tools(monkeypatch, capsys):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION", "hosted-agent-connection")
    resolved = entry(GATEWAY + "/northwind/mcp/tech-lookup")
    assert "project_connection_id" not in resolved
    assert "MCP_GATEWAY_CONNECTION_TECH_LOOKUP" in capsys.readouterr().out


def test_custom_gateway_domain_also_uses_the_endpoints_connection(monkeypatch):
    configure_connections(monkeypatch)
    monkeypatch.setenv("NORTHWIND_GATEWAY_URL", "https://api.northwind.example")
    resolved = entry("https://api.northwind.example/insurance/mcp/work-order")
    assert resolved["project_connection_id"] == WORK_ORDER_CONNECTION
    assert resolved["server_url"] == "https://api.northwind.example/insurance/mcp/work-order"


def test_connection_name_is_trimmed(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_WORK_ORDER", f"  {WORK_ORDER_CONNECTION}\n")
    assert entry(GATEWAY + "/northwind/mcp/work-order")["project_connection_id"] == WORK_ORDER_CONNECTION


@pytest.mark.parametrize("value", [None, "", "   "])
def test_gateway_tool_is_unchanged_but_warns_without_a_connection(monkeypatch, capsys, value):
    if value is not None:
        monkeypatch.setenv("MCP_GATEWAY_CONNECTION_TECH_LOOKUP", value)
    assert "project_connection_id" not in entry(GATEWAY + "/northwind/mcp/tech-lookup")
    output = capsys.readouterr().out
    assert "no project connection is configured" in output and "MCP_GATEWAY_CONNECTION_TECH_LOOKUP" in output


@pytest.mark.parametrize("url", [GATEWAY + "/northwind/mcp/other", GATEWAY + "/northwind/tech-lookup", GATEWAY + "/mcp", GATEWAY])
def test_unrecognised_gateway_endpoints_get_no_connection(monkeypatch, capsys, url):
    configure_connections(monkeypatch)
    assert "project_connection_id" not in entry(url)
    assert "unrecognised MCP endpoint" in capsys.readouterr().out


@pytest.mark.parametrize("url", [DIRECT_LOOKUP, DIRECT_LOOKUP + "/", "https://mcp.example/internal/mcp"])
def test_direct_container_app_urls_never_get_a_gateway_connection(monkeypatch, url):
    configure_connections(monkeypatch)
    resolved = entry(url)
    assert "project_connection_id" not in resolved
    assert resolved["server_url"] == url.rstrip("/") + "/"


@pytest.mark.parametrize("url,expected", [
    (DIRECT_LOOKUP, DIRECT_LOOKUP + "/"),
    (DIRECT_LOOKUP + "/", DIRECT_LOOKUP + "/"),
    (DIRECT_LOOKUP + "/other", DIRECT_LOOKUP + "/other"),
])
def test_mcp_trailing_slash_normalization_still_applies_to_direct_urls(url, expected):
    assert entry(url)["server_url"] == expected


@pytest.mark.parametrize("url", [GATEWAY + "/northwind/mcp", GATEWAY + "/insurance/mcp?", "https://apim.azure-api.net/mcp"])
def test_mcp_trailing_slash_normalization_is_skipped_for_gateway_urls(url):
    assert entry(url)["server_url"] == url


def test_custom_gateway_domain_skips_the_trailing_slash_normalization(monkeypatch):
    monkeypatch.setenv("NORTHWIND_GATEWAY_URL", "https://api.northwind.example")
    assert entry("https://api.northwind.example/northwind/mcp")["server_url"] == "https://api.northwind.example/northwind/mcp"


def test_yaml_provided_connection_wins(monkeypatch):
    configure_connections(monkeypatch)
    monkeypatch.setenv("YAML_CONNECTION", "yaml-owned-connection")
    resolved = entry(GATEWAY + "/northwind/mcp/tech-lookup", project_connection_id="${YAML_CONNECTION}")
    assert resolved["project_connection_id"] == "yaml-owned-connection"


def test_other_tool_kinds_are_not_given_a_gateway_connection(monkeypatch):
    configure_connections(monkeypatch)
    cfg = {"name": "agent", "tools": [
        {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}},
        {"type": "code_interpreter"},
    ]}
    assert all("project_connection_id" not in tool for tool in module._tools_for_agent(cfg))


@pytest.mark.parametrize("path,variable,connection_variable,connection,tool,gateway_path", [
    ("tech_lookup.yaml", "MCP_TECH_LOOKUP_URL", "MCP_GATEWAY_CONNECTION_TECH_LOOKUP", LOOKUP_CONNECTION, "lookup_order", "/northwind/mcp/tech-lookup"),
    ("work_order_draft.yaml", "MCP_WORK_ORDER_URL", "MCP_GATEWAY_CONNECTION_WORK_ORDER", WORK_ORDER_CONNECTION, "propose_resolution", "/northwind/mcp/work-order"),
    ("insurance/tech_lookup.yaml", "MCP_TECH_LOOKUP_URL", "MCP_GATEWAY_CONNECTION_TECH_LOOKUP", LOOKUP_CONNECTION, "lookup_policy", "/insurance/mcp/tech-lookup"),
    ("insurance/work_order_draft.yaml", "MCP_WORK_ORDER_URL", "MCP_GATEWAY_CONNECTION_WORK_ORDER", WORK_ORDER_CONNECTION, "draft_claim_action", "/insurance/mcp/work-order"),
])
def test_shipped_prompt_agents_cut_over_to_the_gateway_and_back(monkeypatch, path, variable, connection_variable, connection, tool, gateway_path):
    cfg = yaml.safe_load((ROOT / "agents" / path).read_text(encoding="utf-8"))
    original = cfg["tools"][0]["server_url"]
    assert original == "${" + variable + "}"

    monkeypatch.setenv(variable, DIRECT_WORK_ORDER)
    (direct,) = module._tools_for_agent(cfg)
    assert direct["server_url"] == DIRECT_WORK_ORDER + "/" and "project_connection_id" not in direct

    monkeypatch.setenv(variable, GATEWAY + gateway_path)
    monkeypatch.setenv(connection_variable, connection)
    (gated,) = module._tools_for_agent(cfg)
    assert gated["server_url"] == GATEWAY + gateway_path
    assert gated["project_connection_id"] == connection
    assert tool in gated["allowed_tools"] and gated["require_approval"] == "never"

    monkeypatch.delenv(connection_variable)
    (ungated,) = module._tools_for_agent(cfg)
    assert ungated == {key: value for key, value in gated.items() if key != "project_connection_id"}
