import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import HandoffGraph from "./HandoffGraph";

const labels = (html: string, state: "active" | "visited" | "idle") =>
  [...html.matchAll(new RegExp(`class="handoff-node ${state}"[^>]*>.*?<text[^>]*>([^<]*)</text>`, "g"))].map((m) => m[1]);

describe("handoff graph", () => {
  it("highlights the insurance specialist the orchestrator is calling and dims earlier ones", () => {
    const html = renderToStaticMarkup(createElement(HandoffGraph, {
      active: "claim_action_draft", visited: ["triage", "policy_claim_lookup"],
    }));
    expect(labels(html, "active")).toEqual(["claim action draft"]);
    expect(labels(html, "visited")).toEqual(["triage", "claim / policy records"]);
    expect(labels(html, "idle")).toEqual(["policy wording"]);
  });

  it("shows every specialist as idle before the first handoff", () => {
    const html = renderToStaticMarkup(createElement(HandoffGraph, { active: null, visited: [] }));
    expect(labels(html, "idle")).toEqual(["triage", "claim / policy records", "policy wording", "claim action draft"]);
    expect(labels(html, "active")).toEqual([]);
  });
});
