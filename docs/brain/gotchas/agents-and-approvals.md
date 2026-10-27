# Agents and approvals

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
working on agent tools, human approvals, the Copilot SDK harness or hosted agent versions. Keep each entry short
and cite a source.

## Approvals and agent tools

- **Approval and business execution are never agent tools.** Agents only enqueue *pending* items; humans decide
  in HITL. Model-generated text is never proof of approval or persistence, and `workflow_id` comes from
  application-owned request metadata, never from message text (the `[workflow_id: ...]` prefix the API adds is not
  authority). Exposing HITL decide or delete operations as tools breaks this. Sources:
  [architecture.md](../../architecture.md#hosted-agents),
  [architecture.md](../../architecture.md#copilot-sdk-agent-harness),
  [ai-gateway.md](../../ai-gateway.md#findings-to-act-on).

## Hosted agent versions

- **Versions are immutable; the selector controls traffic.** A new version of a pinned agent takes no traffic until
  `update_details` switches it, and switching back is the rollback; the runtime principal belongs to the agent, so
  no new role assignments are needed. [`deploy-hosted-agents.py`](../../../scripts/deploy-hosted-agents.py) switches
  immediately and accepts only the five standard roles. Claim-action drafting must keep
  `MCP_GATEWAY_CONNECTION` and its classic gateway URL on every new version, or the locked app answers 401.
  Lookup and wording now use their AI Gateway MCP connections instead. Source:
  [insurance cut-over](../../api-management.md#insurance-stack-cut-over-2026-10-23).

- **An environment flag is not proof an older deployed runtime implements it.** The older hosted bundle
  ignored `AI_GATEWAY_REQUIRED` and the MCP gateway route configuration. Check source and selected-version responses:
  `model_gateway_required=true`, `mcp_route=ai-gateway`, and the **exact** namespaced tool name.
  `knowledge_base_retrieve` misleadingly starts with `knowledge_`, but only
  `knowledge_knowledge_base_retrieve` proves that gateway namespace. Preserve canonical prompts when
  reconciling runtime modules. Source: [live rollout evidence](../../ai-gateway.md#live-rollout-evidence-and-rollback).
