import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import ChatTab from "./ChatTab";

describe("Claims Concierge chat", () => {
  it("is a gated text chat with its orchestrator trace and no hands-free voice control", () => {
    const html = renderToStaticMarkup(createElement(ChatTab));
    expect(html).toContain("Insurance chat unavailable until authorised rollout.");
    expect(html).toContain(">Send<");
    expect(html).toContain("Orchestrator trace");
    expect(html).not.toMatch(/hands-free|Stop voice|Connecting/i);
  });
});
