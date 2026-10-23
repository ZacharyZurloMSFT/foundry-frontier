# Agents and approvals

> Status: current · Last reviewed: 2026-10-23 · Sources: cited per entry · Code wins: if an entry is
> wrong, fix it in the same PR.

Part of the [gotchas index](README.md): non-obvious traps, written as **trap, why, what to do**. Read this when
working on agent tools, human approvals, the Copilot SDK harness or hosted agent versions. Keep each entry short
and cite a source.

## Approvals and agent tools

- **Approval and business execution are never agent tools.** Agents only enqueue *pending* items; humans decide
  in HITL. Model-generated text is never proof of approval or persistence, and `workflow_id` comes from
  application-owned metadata ("prompt prefixes are not authority"). Exposing HITL decide or delete operations as
  tools breaks this. Sources: [architecture.md](../../architecture.md#order-concierge-hosted-runtime),
  [architecture.md](../../architecture.md#copilot-sdk-agent-harness),
  [ai-gateway.md](../../ai-gateway.md#findings-to-act-on).

## Hosted agent versions

- **Versions are immutable; the selector controls traffic.** A new version of a pinned agent takes no traffic until
  `update_details` switches it, and switching back is the rollback; the runtime principal belongs to the agent, so
  no new role assignments are needed. [`deploy-orders-hosted.py`](../../../scripts/deploy-orders-hosted.py) switches
  immediately and accepts only the standard agent names. The two insurance roles that call MCP servers must keep
  `MCP_GATEWAY_CONNECTION` and the gateway URLs on every new version, or the locked apps answer 401. Source:
  [insurance cut-over](../../api-management.md#insurance-stack-cut-over-2026-10-23).
