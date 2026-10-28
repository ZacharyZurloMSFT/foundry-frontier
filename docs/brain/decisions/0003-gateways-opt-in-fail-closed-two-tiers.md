# 0003 — Gateways are opt-in, fail closed, and split by header handling

> Status: Accepted · Recorded: 2026-10-22 (the decision pre-dates this record) · Sources:
> [api-management.md](../../api-management.md), [ai-gateway.md](../../ai-gateway.md),
> [architecture.md](../../architecture.md#api-gateway-opt-in)

> **2026-10-28 repository revision (not a live cutover):** the intended split is now by
> traffic ownership, not header forwarding: REST/voice APIs on classic APIM, supported models
> and remote MCP tools on AI Gateway. The staged migration replaces claim-review workflow
> headers with application-signed, short-lived, exact-draft proofs and pins MCP backends to
> AI Gateway managed identity. The old decision below describes the current compatibility
> deployment until gateway-version support is verified and cutover is separately approved.
> See [staged separation](../../ai-gateway.md#staged-separation-repository-preparation-not-deployed).

## Context

Governance (CORS, rate limits, keys, token-based access) was wanted in front of the apps' REST, WebSocket and
MCP surfaces and in front of model inference, but a fresh `azd up` has to stay unchanged. API Management
Standard v2 has no dedicated outbound IP, so IP rules cannot identify the gateway. The AI Gateway tier drops
client headers before the MCP backend, while the claim-review MCP binds an application-owned workflow header
(`x-workflow-id`).

## Decision

- Both gateways are opt-in and off by default. Live changes are applied by guarded, plan-first scripts and
  workflows, not by `azd up`.
- Classic API Management (`apim-northwind`) fronts REST, WebSocket and MCP for the apps and forwards headers;
  the AI Gateway tier (`ai-gateway-northwind`) fronts models and header-free MCP
  ([ai-gateway.md](../../ai-gateway.md#two-gateways)).
- Apps verify a gateway-issued Entra token in-process (the gateway guard) instead of relying on IP restrictions
  ([api-management.md](../../api-management.md#security-model)).
- When a gateway is required (`AI_GATEWAY_REQUIRED`, `AI_GATEWAY_MCP_REQUIRED`,
  `NORTHWIND_GATEWAY_AUTH_REQUIRED`), direct paths fail closed. Rollout order is route, verify, then enforce
  ([ai-gateway.md](../../ai-gateway.md#runbook)).

## Consequences

- The claim-review MCP goes through `apim-northwind`, never usefully through the AI Gateway: the gateway drops the
  workflow header, so the app rejects the call by design. It can stay direct only while its app is not locked behind
  the gateway guard.
- Changing the REST surface means regenerating the OpenAPI export
  ([gotchas](../gotchas/api-and-gateways.md)).
- RBAC alone cannot force gateway-only inference: enforcement is code that fails closed, removing redundant
  role assignments, and detecting bypass ([ai-gateway.md](../../ai-gateway.md#findings-to-act-on)).
