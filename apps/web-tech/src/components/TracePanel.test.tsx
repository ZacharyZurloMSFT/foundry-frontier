import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { TraceSpan } from "../api";
import { SpanTable } from "./TracePanel";

const span = (overrides: Partial<TraceSpan>): TraceSpan => ({
  ts: "", kind: "dependency", name: "chat", duration_ms: 12, trace_id: "t", parent_span_id: "", span_id: "s",
  agent: "", tool: "", model: "gpt-5.4", prompt_tokens: 1, completion_tokens: 2, cost_usd: 0,
  git_sha: "", prompt_sha: {}, ...overrides,
});
const render = (...spans: TraceSpan[]) => renderToStaticMarkup(createElement(SpanTable, { spans }));

describe("trace model route", () => {
  it("marks a model call served by the AI Gateway and shows its alias", () => {
    const html = render(span({ gateway: "gateway", gateway_model: "aisvc-zurlo-northwind/gpt-5-4" }));
    expect(html).toContain("AI Gateway");
    expect(html).toContain('title="aisvc-zurlo-northwind/gpt-5-4"');
  });

  it("claims nothing for spans that only carry the request-level route", () => {
    expect(render(span({}))).not.toContain("route-badge");
    expect(render(span({ gateway: "gateway" }))).not.toContain("route-badge");
    expect(render(span({ gateway: "direct" }))).not.toContain("route-badge");
  });

  it("does not label spans without a model", () => {
    const html = render(span({ model: "", agent: "triage", gateway: "gateway", gateway_model: "p/m" }));
    expect(html).not.toContain("route-badge");
  });
});
