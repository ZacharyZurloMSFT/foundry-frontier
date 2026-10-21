import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from agents.hosted import tools
from agents.hosted.config import INSURANCE_PROMPTS, Settings
from agents.hosted.runtime import RequestMiddleware
from agents.hosted.scope import RequestScope, current_scope
from agents.insurance.domain import ClaimActionDraft
from agents.insurance.models import InsuranceTriageResult
from scripts.orders_agent_package import entries
from apps.api.app.insurance_config import INSURANCE_AGENT_NAMES, ROLE_DIRECTORIES

ROOT = Path(__file__).resolve().parents[2]
ENV = {
    "NORTHWIND_BUSINESS_DOMAIN": "insurance",
    "FOUNDRY_PROJECT_ENDPOINT": "https://existing.invalid/api/projects/nw-insurance-claims",
    "MODEL_DEPLOYMENT_NAME": "baseline",
    "MCP_TECH_LOOKUP_URL": "https://lookup.invalid/mcp",
    "MCP_WORK_ORDER_URL": "https://draft.invalid/mcp",
}


@pytest.mark.parametrize("role", INSURANCE_PROMPTS)
def test_canonical_insurance_prompts_and_packaging(role):
    config = Settings.from_env({**ENV, "ORDER_AGENT_ROLE": role})
    assert config.prompt["name"].startswith("northwind-insurance-")
    assert config.metadata["business_domain"] == "insurance"
    archive = entries(ROOT, ROOT / "agents" / "hosted" / "requirements.txt")
    assert archive["agents/" + INSURANCE_PROMPTS[role]] == (ROOT / "agents" / INSURANCE_PROMPTS[role]).read_bytes()
    assert archive["agents/insurance/domain.py"] == (ROOT / "apps" / "api" / "app" / "insurance_domain.py").read_bytes()
    assert archive["data/northwind/insurance-fixtures.json"] == (ROOT / "data" / "northwind" / "insurance-fixtures.json").read_bytes()
    assert archive["agents/hosted/tools.py"] == (ROOT / "agents" / "hosted" / "tools.py").read_bytes()
    definition = yaml.safe_load((ROOT / "agents" / ROLE_DIRECTORIES[role] / "agent.yaml").read_text())
    variables = {item["name"]: item["value"] for item in definition["environment_variables"]}
    assert definition["name"] == INSURANCE_AGENT_NAMES[role]
    assert variables["NORTHWIND_BUSINESS_DOMAIN"] == "insurance"
    assert variables["AGENT_ROLE"] == role
    assert "NORTHWIND_INSURANCE_AGENTS_READY" not in variables
    assert not any(name.startswith(("ORDERS_", "ORDER_")) for name in variables)


def test_mcp_allowlists_not_retail_and_no_web_contract_sources():
    lookup = Settings.from_env({**ENV, "ORDER_AGENT_ROLE": "orderlookup"})
    assert lookup.mcp_spec()["allowed_tools"] == ["lookup_policy", "lookup_claim"]
    draft = Settings.from_env({**ENV, "ORDER_AGENT_ROLE": "resolutiondraft"})
    assert draft.mcp_spec()["allowed_tools"] == ["draft_claim_action"]
    policy = Settings.from_env({**ENV, "ORDER_AGENT_ROLE": "policylookup"})
    assert [k["kind"] for k in policy.prompt["knowledge"]] == ["foundry_iq"]
    with pytest.raises(ValueError, match="not insurance-ready"):
        Settings.from_env({**ENV, "ORDER_AGENT_ROLE": "triage", "TRIAGE_MODEL_DEPLOYMENT": "northwind-orders-triage-ft-v1"})
    with pytest.raises(ValueError, match="not insurance-ready"):
        Settings.from_env({**ENV, "ORDER_AGENT_ROLE": "triage", "TRIAGE_MODEL_DEPLOYMENT": "northwind-orders-triage-ft-v1",
                           "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED": "true"})


def test_insurance_triage_schema_rejects_retail_output():
    config = Settings.from_env({**ENV, "ORDER_AGENT_ROLE": "triage"})
    result = InsuranceTriageResult(
        claim_id="CLM-2001", policy_id="POL-1001", policyholder_id="PH-1001", intent="CLAIM_STATUS",
        sentiment="calm", urgency="P3", problem_summary="Request a claim update.")
    RequestMiddleware(config).validate(RequestScope(), result.model_dump_json())
    with pytest.raises(ValueError):
        RequestMiddleware(config).validate(RequestScope(), json.dumps({"order_id": "NW-40021"}))


def test_insurance_knowledge_is_explicit_and_never_uses_retail_corpus():
    env = {
        **ENV, "ORDER_AGENT_ROLE": "policylookup", "AZURE_SEARCH_ENDPOINT": "https://existing.search.invalid",
        "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-insurance-kb",
        "AZURE_SEARCH_INDEX": "northwind-insurance-knowledge",
        "ORDERS_KB_CONNECTION": "nw-insurance-claims-kb-mcp",
    }
    spec = Settings.from_env(env).knowledge_spec()
    assert spec["project_connection_id"] == "nw-insurance-claims-kb-mcp"
    assert "/knowledgebases/northwind-insurance-kb/mcp?" in spec["url"]
    for key, value in (("AZURE_SEARCH_KNOWLEDGE_BASE", "northwind-kb"),
                       ("AZURE_SEARCH_INDEX", "northwind-knowledge"),
                       ("ORDERS_KB_CONNECTION", "northwind-orders-kb-mcp")):
        with pytest.raises(ValueError, match="retail knowledge cannot be substituted"):
            Settings.from_env({**env, key: value}).knowledge_spec()


@pytest.mark.asyncio
async def test_insurance_proposal_binds_workflow_and_confirms_non_execution(monkeypatch):
    config = Settings.from_env({**ENV, "ORDER_AGENT_ROLE": "resolutiondraft"})
    draft = ClaimActionDraft(
        claim_id="CLM-2001", policy_id="POL-1001", policyholder_id="PH-1001",
        action_type="request_evidence", summary="Request photos.", policy_refs=[])
    scope = RequestScope(workflow_id="wf-insurance-bound")
    payload = draft.model_dump(exclude_none=True)
    response = {"status": "pending_approval", "hitl_id": "saved", "workflow_id": scope.workflow_id,
                "draft": payload, "kind": "insurance_claim_action", "synthetic": True,
                "execution_status": "not_executed"}
    call = AsyncMock(return_value=response)
    monkeypatch.setattr(tools, "call_proposal_mcp", call)
    token = current_scope.set(scope)
    try:
        content = await tools.proposal_tool(config).invoke(arguments={"draft": payload})
        result = json.loads(content[0].text)
        assert result["execution_status"] == "not_executed"
        call.assert_awaited_once_with("https://draft.invalid/mcp", "wf-insurance-bound", payload, "draft_claim_action")
    finally:
        current_scope.reset(token)
