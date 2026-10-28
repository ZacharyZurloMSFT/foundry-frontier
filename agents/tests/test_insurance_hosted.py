import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agents.hosted import tools
from agents.hosted.config import PROMPTS, Settings
from agents.hosted.runtime import RequestMiddleware
from agents.hosted.scope import RequestScope, current_scope
from agents.insurance.domain import ClaimActionDraft
from agents.insurance.models import InsuranceTriageResult
from scripts.hosted_agent_package import PACKAGE_SOURCES, entries

ROOT = Path(__file__).resolve().parents[2]
ENV = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://existing.invalid/api/projects/nw-insurance-claims",
    "MODEL_DEPLOYMENT_NAME": "baseline",
    "MCP_POLICY_LOOKUP_URL": "https://lookup.invalid/mcp",
    "MCP_CLAIM_REVIEW_URL": "https://draft.invalid/mcp",
}


@pytest.mark.parametrize("role", PROMPTS)
def test_canonical_insurance_prompts_and_packaging(role):
    config = Settings.from_env({**ENV, "AGENT_ROLE": role})
    assert config.prompt["name"].startswith("northwind-insurance-")
    assert config.metadata["business_domain"] == "insurance"
    archive = entries(ROOT, ROOT / "agents" / "hosted" / "requirements.txt")
    assert archive["agents/" + PROMPTS[role]] == (ROOT / "agents" / PROMPTS[role]).read_bytes()
    assert archive["agents/insurance/domain.py"] == (ROOT / "apps" / "api" / "app" / "insurance_domain.py").read_bytes()
    assert archive["data/northwind/insurance-fixtures.json"] == (ROOT / "data" / "northwind" / "insurance-fixtures.json").read_bytes()
    assert archive["agents/hosted/tools.py"] == (ROOT / "agents" / "hosted" / "tools.py").read_bytes()


def test_the_package_is_exactly_the_runtime_the_insurance_prompts_and_their_fixtures():
    archive = entries(ROOT, ROOT / "agents" / "hosted" / "requirements.txt")
    assert set(archive) == {"main.py", "requirements.txt", "agents/insurance/domain.py",
                            "agents/insurance/workflow_binding.py", *PACKAGE_SOURCES}
    assert archive["agents/insurance/workflow_binding.py"] == (
        ROOT / "apps" / "api" / "app" / "workflow_binding.py").read_bytes()
    assert archive["main.py"] == (ROOT / "agents" / "orchestrator" / "main.py").read_bytes()
    assert {name for name in archive if name.endswith(".yaml")} == {"agents/" + path for path in PROMPTS.values()}
    assert not any(name.startswith(("tests/", ".venv/", ".env")) or "/tests/" in name for name in archive)
    for name, content in archive.items():
        if name.endswith(".py"):
            compile(content, name, "exec")


def test_mcp_allowlists_and_triage_overrides_are_insurance_only():
    lookup = Settings.from_env({**ENV, "AGENT_ROLE": "policy-claim-lookup"})
    assert lookup.mcp_spec()["allowed_tools"] == ["lookup_policy", "lookup_claim"]
    draft = Settings.from_env({**ENV, "AGENT_ROLE": "claim-action-draft"})
    assert draft.mcp_spec()["allowed_tools"] == ["draft_claim_action"]
    policy = Settings.from_env({**ENV, "AGENT_ROLE": "policy-wording"})
    assert [k["kind"] for k in policy.prompt["knowledge"]] == ["foundry_iq"]
    with pytest.raises(ValueError, match="not insurance-ready"):
        Settings.from_env({**ENV, "AGENT_ROLE": "triage", "TRIAGE_MODEL_DEPLOYMENT": "other-triage-ft-v1"})
    with pytest.raises(ValueError, match="not insurance-ready"):
        Settings.from_env({**ENV, "AGENT_ROLE": "triage", "TRIAGE_MODEL_DEPLOYMENT": "other-triage-ft-v1",
                           "NORTHWIND_INSURANCE_TRIAGE_MODEL_VERIFIED": "true"})


def test_insurance_triage_schema_rejects_foreign_output():
    config = Settings.from_env({**ENV, "AGENT_ROLE": "triage"})
    result = InsuranceTriageResult(
        claim_id="CLM-2001", policy_id="POL-1001", policyholder_id="PH-1001", intent="CLAIM_STATUS",
        sentiment="calm", urgency="P3", problem_summary="Request a claim update.")
    RequestMiddleware(config).validate(RequestScope(), result.model_dump_json())
    with pytest.raises(ValueError):
        RequestMiddleware(config).validate(RequestScope(), json.dumps({"claim_ref": "CLM-2001"}))
    with pytest.raises(ValueError):
        RequestMiddleware(config).validate(
            RequestScope(), result.model_copy(update={"intent": "UNKNOWN_INTENT"}).model_dump_json())


def test_insurance_knowledge_is_explicit_and_never_substituted():
    env = {
        **ENV, "AGENT_ROLE": "policy-wording", "AZURE_SEARCH_ENDPOINT": "https://existing.search.invalid",
        "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-insurance-kb",
        "AZURE_SEARCH_INDEX": "northwind-insurance-knowledge",
        "AGENT_KB_CONNECTION": "nw-insurance-claims-kb-mcp",
    }
    spec = Settings.from_env(env).knowledge_spec()
    assert spec["project_connection_id"] == "nw-insurance-claims-kb-mcp"
    assert "/knowledgebases/northwind-insurance-kb/mcp?" in spec["url"]
    for key, value in (("AZURE_SEARCH_KNOWLEDGE_BASE", "other-kb"),
                       ("AZURE_SEARCH_INDEX", "other-knowledge"),
                       ("AGENT_KB_CONNECTION", "other-kb-mcp")):
        with pytest.raises(ValueError, match="other knowledge cannot be substituted"):
            Settings.from_env({**env, key: value}).knowledge_spec()


@pytest.mark.asyncio
async def test_insurance_proposal_binds_workflow_and_confirms_non_execution(monkeypatch):
    config = Settings.from_env({**ENV, "AGENT_ROLE": "claim-action-draft"})
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
