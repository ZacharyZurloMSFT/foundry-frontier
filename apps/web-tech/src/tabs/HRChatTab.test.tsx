import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import HRChatTab, { streamSupportChat } from "./HRChatTab";

afterEach(() => vi.unstubAllGlobals());
const response = (events: unknown[]) => new Response(events.map((e) => JSON.stringify(e)).join("\n"));
async function collect() {
  const events = [];
  for await (const event of streamSupportChat("hr-gpt-5-4", null, "Evidence?", new AbortController().signal)) events.push(event);
  return events;
}

describe("grounded employee and adjuster support", () => {
  it("preserves HR/IT help while advertising isolated, read-only adjuster retrieval", () => {
    const html = renderToStaticMarkup(createElement(HRChatTab));
    expect(html).toContain("Employee &amp; Adjuster Support");
    expect(html).toContain("nw-insurance-adjuster-support");
    expect(html).toContain("PTO days");
    expect(html).toContain("VPN");
    expect(html).toContain("fictional collision claim");
    expect(html).toContain("No legacy HR agent, model-memory or web fallback");
    expect(html).not.toContain("northwind-hr-assistant");
  });
  it("uses only the supported backend route and preserves source metadata", async () => {
    const citation = { id: "ADJ-DUTY-001", title: "Fictional duty", version: "1",
      source_uri: "urn:northwind:synthetic:employee-support:ADJ-DUTY-001:v1" };
    const fetch = vi.fn().mockResolvedValue(response([
      { kind: "session", session_id: "adjuster-hr-gpt-5-4-unit" },
      { kind: "assistant_delta", text: "Retrieved guidance" },
      { kind: "citations", citations: [citation] }, { kind: "done" },
    ]));
    vi.stubGlobal("fetch", fetch);
    const events = await collect();
    expect(events[2].citations).toEqual([citation]);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/adjuster-support\/hr-gpt-5-4\/chat$/);
    expect(url).not.toContain("/agent/");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(JSON.parse(init.body)).toEqual({ session_id: null, message: "Evidence?" });
  });
  it("does not invent fallback answers on no evidence or backend failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 })));
    await expect(collect()).rejects.toThrow("no fallback answer");
  });
  it("fails closed on truncated streams and fabricated citation URIs", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([{ kind: "assistant_delta", text: "incomplete" }])));
    await expect(collect()).rejects.toThrow("interrupted");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([
      { kind: "citations", citations: [{ id: "fake", title: "Fake", version: "1", source_uri: "https://arbitrary.example" }] },
      { kind: "done" },
    ])));
    await expect(collect()).rejects.toThrow("invalid source citations");
  });
});
