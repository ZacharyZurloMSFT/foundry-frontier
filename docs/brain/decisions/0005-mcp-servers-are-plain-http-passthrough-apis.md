# 0005 — MCP servers are published as plain HTTP passthrough APIs

> Status: Accepted · Recorded: 2026-10-23 (the decision pre-dates this record) · Sources:
> [`infra/live/apim/policies/mcp-api.xml`](../../../infra/live/apim/policies/mcp-api.xml),
> [api-management.md](../../api-management.md#findings-that-shaped-the-design-verified-live-on-scratch-apis)

## Context

API Management can host an MCP server natively (a preview `mcp` API type) or publish a backend as an ordinary
HTTP API. The backends here are FastMCP servers (stateless streamable HTTP) mounted at `/mcp/`, and a request to
`/mcp` without the trailing slash is a 307 that Foundry's MCP client does not follow.

## Decision

- The MCP APIs are GA plain HTTP passthrough APIs whose policy rewrites the backend URI to `/mcp/`, so clients
  never append `/mcp`. This repository manages two, `insurance-mcp-tech-lookup` and `insurance-mcp-work-order`
  (live API ids kept for the policy and claim lookup and the claim-review servers); the earlier `northwind` stack
  had two more.
- The native `type: mcp` API is not used: it is a preview, APIM-hosted tool server with a different ARM contract
  than documented (found on scratch APIs). Initialize, `tools/list` and `tools/call` work through the passthrough
  APIs, and subscription-key enforcement behaved as intended.

## Consequences

- Do not "upgrade" these APIs to the native type, and do not read `context.Response.Body` in the policy: it
  forces buffering and breaks event streams.
- Access is a subscription key on the stack's `agents` product; the policy adds the gateway token that the guard
  on the app verifies ([ADR 0003](0003-gateways-opt-in-fail-closed-two-tiers.md)).
