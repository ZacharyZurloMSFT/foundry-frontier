"""Prompt-agent MCP tools authenticate through the API gateway only when explicitly configured."""
import copy
import importlib.util
import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("create_prompt_agents", ROOT / "scripts" / "create-prompt-agents.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
INSURANCE_PROMPTS = ROOT / "agents" / "insurance"

LOOKUP_CONNECTION = "northwind-apim-policy-lookup"
CLAIM_REVIEW_CONNECTION = "northwind-apim-claim-review"
GATEWAY = "https://apim-northwind.azure-api.net"
# The live insurance APIs keep these gateway path segments.
LOOKUP_PATH = "/insurance/mcp/tech-lookup"
CLAIM_REVIEW_PATH = "/insurance/mcp/work-order"
DIRECT_LOOKUP = "https://ca-insurance-lookup.gentlemushroom.canadacentral.azurecontainerapps.io/mcp"
DIRECT_CLAIM_REVIEW = "https://ca-insurance-review.gentlemushroom.canadacentral.azurecontainerapps.io/mcp"
CANONICAL_CONNECTIONS = ("MCP_GATEWAY_CONNECTION_POLICY_LOOKUP", "MCP_GATEWAY_CONNECTION_CLAIM_REVIEW")
DEPRECATED_CONNECTIONS = ("MCP_GATEWAY_CONNECTION_TECH_LOOKUP", "MCP_GATEWAY_CONNECTION_WORK_ORDER")
CANONICAL_URLS = ("MCP_POLICY_LOOKUP_URL", "MCP_CLAIM_REVIEW_URL")
DEPRECATED_URLS = ("MCP_TECH_LOOKUP_URL", "MCP_WORK_ORDER_URL")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    names = ("MCP_GATEWAY_CONNECTION", *CANONICAL_CONNECTIONS, *DEPRECATED_CONNECTIONS,
             "NORTHWIND_GATEWAY_URL", *CANONICAL_URLS, *DEPRECATED_URLS)
    for name in names:
        monkeypatch.delenv(name, raising=False)


def configure_connections(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_POLICY_LOOKUP", LOOKUP_CONNECTION)
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_CLAIM_REVIEW", CLAIM_REVIEW_CONNECTION)


def mcp(url, **extra):
    return {"name": "northwind-insurance-policy-claim-lookup", "tools": [{
        "type": "mcp", "server_label": "northwind-insurance-policy-claim-lookup", "server_url": url,
        "require_approval": "never", "allowed_tools": ["lookup_policy"], **extra}]}


def entry(url, **extra):
    (resolved,) = module._tools_for_agent(mcp(url, **extra))
    return resolved


@pytest.mark.parametrize("url", [
    GATEWAY + LOOKUP_PATH,
    "https://apim-northwind.azure-api.net/insurance/mcp/work-order",
    "https://APIM-Northwind.Azure-API.net" + LOOKUP_PATH,
    "https://apim-northwind.developer.azure-api.net/x",
    "https://apim-northwind.azure-api.net:443/insurance/mcp/work-order?api-version=1",
])
def test_azure_api_hosts_are_gateway_urls(url):
    assert module._is_gateway_url(url)


@pytest.mark.parametrize("url", [
    DIRECT_LOOKUP,
    "https://azure-api.net/insurance/mcp/work-order",
    "https://notazure-api.net/insurance/mcp/work-order",
    "https://apim-northwind.azure-api.net.evil.example/mcp",
    "https://apim-northwind.azure-api.net@evil.example/mcp",
    "https://evil.example/apim-northwind.azure-api.net/mcp",
    "https://evil.example/?next=https://apim-northwind.azure-api.net",
    "${MCP_CLAIM_REVIEW_URL}",
    "https://[bad",
    "",
])
def test_other_hosts_are_not_gateway_urls(url):
    assert not module._is_gateway_url(url)


@pytest.mark.parametrize("configured,url,expected", [
    ("https://api.northwind.example", "https://api.northwind.example" + LOOKUP_PATH, True),
    ("https://api.northwind.example/insurance/", "https://api.northwind.example" + CLAIM_REVIEW_PATH, True),
    ("api.northwind.example", "https://api.northwind.example" + CLAIM_REVIEW_PATH, True),
    ("api.northwind.example:8443/base", "https://api.northwind.example:8443" + CLAIM_REVIEW_PATH, True),
    ("  https://API.Northwind.Example  ", "https://api.northwind.example/x", True),
    ("https://api.northwind.example", "https://other.northwind.example" + CLAIM_REVIEW_PATH, False),
    ("https://api.northwind.example", "https://api.northwind.example.evil.test/mcp/work-order", False),
    ("https://api.northwind.example", DIRECT_CLAIM_REVIEW, False),
    ("https://[bad", "https://api.northwind.example/x", False),
    ("", "https://api.northwind.example/x", False),
])
def test_custom_gateway_domain_comes_from_northwind_gateway_url(monkeypatch, configured, url, expected):
    monkeypatch.setenv("NORTHWIND_GATEWAY_URL", configured)
    assert module._is_gateway_url(url) is expected


def test_gateway_mcp_tool_uses_its_endpoints_connection_and_keeps_the_url(monkeypatch, capsys):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_POLICY_LOOKUP", LOOKUP_CONNECTION)
    url = GATEWAY + LOOKUP_PATH
    assert entry(url) == {
        "type": "mcp", "server_label": "northwind-insurance-policy-claim-lookup", "server_url": url,
        "require_approval": "never", "allowed_tools": ["lookup_policy"], "project_connection_id": LOOKUP_CONNECTION,
    }
    output = capsys.readouterr().out
    assert LOOKUP_CONNECTION in output and "target must be exactly " + url in output


def test_each_mcp_endpoint_gets_its_own_connection_never_a_shared_one(monkeypatch):
    # Foundry sends a tool's calls to the TARGET of the connection it references (verified live: a base-URL
    # connection made Foundry call the base URL and get a 404), so one connection can never serve two endpoints.
    configure_connections(monkeypatch)
    lookup = entry(GATEWAY + LOOKUP_PATH)
    claim_review = entry(GATEWAY + CLAIM_REVIEW_PATH)
    assert lookup["project_connection_id"] == LOOKUP_CONNECTION
    assert claim_review["project_connection_id"] == CLAIM_REVIEW_CONNECTION
    assert lookup["project_connection_id"] != claim_review["project_connection_id"]


def test_deprecated_connection_variables_are_still_read(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_TECH_LOOKUP", "old-lookup-connection")
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_WORK_ORDER", "old-review-connection")
    assert entry(GATEWAY + LOOKUP_PATH)["project_connection_id"] == "old-lookup-connection"
    assert entry(GATEWAY + CLAIM_REVIEW_PATH)["project_connection_id"] == "old-review-connection"


@pytest.mark.parametrize("canonical", ["new-connection", "  new-connection\n"])
def test_the_canonical_connection_variable_wins_over_the_deprecated_one(monkeypatch, canonical):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_POLICY_LOOKUP", canonical)
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_TECH_LOOKUP", "old-lookup-connection")
    assert entry(GATEWAY + LOOKUP_PATH)["project_connection_id"] == "new-connection"


def test_a_blank_canonical_connection_variable_falls_back_to_the_deprecated_one(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_CLAIM_REVIEW", "   ")
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_WORK_ORDER", "old-review-connection")
    assert entry(GATEWAY + CLAIM_REVIEW_PATH)["project_connection_id"] == "old-review-connection"


def test_the_generic_hosted_agent_variable_is_never_attached_to_prompt_agent_tools(monkeypatch, capsys):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION", "hosted-agent-connection")
    resolved = entry(GATEWAY + LOOKUP_PATH)
    assert "project_connection_id" not in resolved
    assert "MCP_GATEWAY_CONNECTION_POLICY_LOOKUP" in capsys.readouterr().out


def test_custom_gateway_domain_also_uses_the_endpoints_connection(monkeypatch):
    configure_connections(monkeypatch)
    monkeypatch.setenv("NORTHWIND_GATEWAY_URL", "https://api.northwind.example")
    resolved = entry("https://api.northwind.example" + CLAIM_REVIEW_PATH)
    assert resolved["project_connection_id"] == CLAIM_REVIEW_CONNECTION
    assert resolved["server_url"] == "https://api.northwind.example" + CLAIM_REVIEW_PATH


def test_connection_name_is_trimmed(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_CONNECTION_CLAIM_REVIEW", f"  {CLAIM_REVIEW_CONNECTION}\n")
    assert entry(GATEWAY + CLAIM_REVIEW_PATH)["project_connection_id"] == CLAIM_REVIEW_CONNECTION


@pytest.mark.parametrize("value", [None, "", "   "])
def test_gateway_tool_is_unchanged_but_warns_without_a_connection(monkeypatch, capsys, value):
    if value is not None:
        monkeypatch.setenv("MCP_GATEWAY_CONNECTION_POLICY_LOOKUP", value)
    assert "project_connection_id" not in entry(GATEWAY + LOOKUP_PATH)
    output = capsys.readouterr().out
    assert "no project connection is configured" in output
    assert "MCP_GATEWAY_CONNECTION_POLICY_LOOKUP" in output and "MCP_GATEWAY_CONNECTION_TECH_LOOKUP" in output


@pytest.mark.parametrize("url", [GATEWAY + "/insurance/mcp/other", GATEWAY + "/insurance/tech-lookup", GATEWAY + "/mcp", GATEWAY])
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


@pytest.mark.parametrize("url", [GATEWAY + "/insurance/mcp", GATEWAY + "/insurance/mcp?", "https://apim.azure-api.net/mcp"])
def test_mcp_trailing_slash_normalization_is_skipped_for_gateway_urls(url):
    assert entry(url)["server_url"] == url


def test_custom_gateway_domain_skips_the_trailing_slash_normalization(monkeypatch):
    monkeypatch.setenv("NORTHWIND_GATEWAY_URL", "https://api.northwind.example")
    assert entry("https://api.northwind.example/insurance/mcp")["server_url"] == "https://api.northwind.example/insurance/mcp"


def test_yaml_provided_connection_wins(monkeypatch):
    configure_connections(monkeypatch)
    monkeypatch.setenv("YAML_CONNECTION", "yaml-owned-connection")
    resolved = entry(GATEWAY + LOOKUP_PATH, project_connection_id="${YAML_CONNECTION}")
    assert resolved["project_connection_id"] == "yaml-owned-connection"


def test_other_tool_kinds_are_not_given_a_gateway_connection(monkeypatch):
    configure_connections(monkeypatch)
    cfg = {"name": "agent", "tools": [
        {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}},
        {"type": "code_interpreter"},
    ]}
    assert all("project_connection_id" not in tool for tool in module._tools_for_agent(cfg))


@pytest.mark.parametrize("canonical,deprecated", list(zip(CANONICAL_URLS, DEPRECATED_URLS)))
def test_url_placeholders_expand_from_the_canonical_variable_first(monkeypatch, canonical, deprecated):
    monkeypatch.setenv(deprecated, "https://old.example/mcp/")
    for placeholder in (canonical, deprecated):
        assert module._expand_env("${" + placeholder + "}") == "https://old.example/mcp/"
    monkeypatch.setenv(canonical, " https://new.example/mcp/ ")
    for placeholder in (canonical, deprecated):
        assert module._expand_env("${" + placeholder + "}") == "https://new.example/mcp/"
    monkeypatch.setenv(canonical, "")
    for placeholder in (canonical, deprecated):
        assert module._expand_env("${" + placeholder + "}") == "https://old.example/mcp/"


def test_other_placeholders_and_non_strings_expand_as_before(monkeypatch):
    monkeypatch.setenv("SOME_OTHER_VARIABLE", "value")
    assert module._expand_env("a-${SOME_OTHER_VARIABLE}-${NOT_SET_ANYWHERE}") == "a-value-${NOT_SET_ANYWHERE}"
    assert module._expand_env(5) == 5 and module._expand_env(None) is None


LOOKUP_AGENT = "northwind-insurance-policy-claim-lookup"
CLAIM_REVIEW_AGENT = "northwind-insurance-claim-action-draft"
# agent, canonical URL variable, deprecated URL variable, connection variable, connection, tool, gateway path
SHIPPED_MCP_PROMPTS = [
    (LOOKUP_AGENT, "MCP_POLICY_LOOKUP_URL", "MCP_TECH_LOOKUP_URL",
     "MCP_GATEWAY_CONNECTION_POLICY_LOOKUP", LOOKUP_CONNECTION, "lookup_policy", LOOKUP_PATH),
    (CLAIM_REVIEW_AGENT, "MCP_CLAIM_REVIEW_URL", "MCP_WORK_ORDER_URL",
     "MCP_GATEWAY_CONNECTION_CLAIM_REVIEW", CLAIM_REVIEW_CONNECTION, "draft_claim_action", CLAIM_REVIEW_PATH),
]
SHIPPED_MCP_IDS = ["policy-claim-lookup", "claim-action-draft"]
SHIPPED_COLUMNS = "agent,canonical,deprecated,connection_variable,connection,tool,gateway_path"


def shipped_prompt(agent_name):
    """The agents/insurance prompt that declares `agent_name`, found by its name so a file rename cannot hide it."""
    for path in sorted(INSURANCE_PROMPTS.glob("*.yaml")):
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        if cfg.get("name") == agent_name:
            return cfg
    raise AssertionError(f"no prompt under agents/insurance declares {agent_name}")


@pytest.mark.parametrize(SHIPPED_COLUMNS, SHIPPED_MCP_PROMPTS, ids=SHIPPED_MCP_IDS)
def test_shipped_prompts_name_the_canonical_url_placeholder(
        agent, canonical, deprecated, connection_variable, connection, tool, gateway_path):
    (declared,) = shipped_prompt(agent)["tools"]
    assert declared["server_url"] == "${" + canonical + "}"


def test_no_shipped_prompt_still_uses_a_deprecated_url_placeholder():
    prompts = sorted(INSURANCE_PROMPTS.glob("*.yaml"))
    assert prompts
    for path in prompts:
        text = path.read_text(encoding="utf-8")
        assert not any(name in text for name in DEPRECATED_URLS), path.name


@pytest.mark.parametrize(SHIPPED_COLUMNS, SHIPPED_MCP_PROMPTS, ids=SHIPPED_MCP_IDS)
@pytest.mark.parametrize("variable_index", [0, 1], ids=["canonical-url-variable", "deprecated-url-variable"])
def test_shipped_prompt_agents_cut_over_to_the_gateway_and_back(
        monkeypatch, agent, canonical, deprecated, connection_variable, connection, tool, gateway_path, variable_index):
    cfg = shipped_prompt(agent)
    variable = (canonical, deprecated)[variable_index]

    monkeypatch.setenv(variable, DIRECT_CLAIM_REVIEW)
    (direct,) = module._tools_for_agent(cfg)
    assert direct["server_url"] == DIRECT_CLAIM_REVIEW + "/" and "project_connection_id" not in direct

    monkeypatch.setenv(variable, GATEWAY + gateway_path)
    monkeypatch.setenv(connection_variable, connection)
    (gated,) = module._tools_for_agent(cfg)
    assert gated["server_url"] == GATEWAY + gateway_path
    assert gated["project_connection_id"] == connection
    assert tool in gated["allowed_tools"] and gated["require_approval"] == "never"

    monkeypatch.delenv(connection_variable)
    (ungated,) = module._tools_for_agent(cfg)
    assert ungated == {key: value for key, value in gated.items() if key != "project_connection_id"}


def test_an_environment_defining_only_the_deprecated_url_names_still_resolves_the_shipped_prompts(monkeypatch):
    monkeypatch.setenv("MCP_TECH_LOOKUP_URL", DIRECT_LOOKUP)
    monkeypatch.setenv("MCP_WORK_ORDER_URL", DIRECT_CLAIM_REVIEW)
    assert not any(os.environ.get(name) for name in CANONICAL_URLS)
    (lookup,) = module._tools_for_agent(shipped_prompt(LOOKUP_AGENT))
    (claim_review,) = module._tools_for_agent(shipped_prompt(CLAIM_REVIEW_AGENT))
    assert lookup["server_url"] == DIRECT_LOOKUP + "/"
    assert claim_review["server_url"] == DIRECT_CLAIM_REVIEW + "/"


@pytest.mark.parametrize(SHIPPED_COLUMNS, SHIPPED_MCP_PROMPTS, ids=SHIPPED_MCP_IDS)
def test_a_prompt_still_written_with_the_deprecated_placeholder_resolves_from_the_canonical_variable(
        monkeypatch, agent, canonical, deprecated, connection_variable, connection, tool, gateway_path):
    cfg = copy.deepcopy(shipped_prompt(agent))
    cfg["tools"][0]["server_url"] = "${" + deprecated + "}"
    monkeypatch.setenv(canonical, "https://canonical-only.example/mcp")
    assert not os.environ.get(deprecated)
    (resolved,) = module._tools_for_agent(cfg)
    assert resolved["server_url"] == "https://canonical-only.example/mcp/"
