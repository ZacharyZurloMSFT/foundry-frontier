import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import AdjusterSupportTab, { streamSupportChat } from "./AdjusterSupportTab";

afterEach(() => vi.unstubAllGlobals());
const response = (events: unknown[]) => new Response(events.map((e) => JSON.stringify(e)).join("\n"));
async function collect(slug = "gpt-5-4") {
  const events = [];
  for await (const event of streamSupportChat(slug, null, "Evidence?", new AbortController().signal)) events.push(event);
  return events;
}

describe("grounded adjuster support", () => {
  it("advertises isolated, read-only adjuster retrieval and no HR/IT content", () => {
    const html = renderToStaticMarkup(createElement(AdjusterSupportTab));
    expect(html).toContain("Adjuster Support");
    expect(html).toContain("nw-insurance-adjuster-support");
    expect(html).toContain("fictional collision claim");
    expect(html).toContain("escalate");
    expect(html).toContain("No model-memory or web fallback");
    expect(html).not.toMatch(/\bHR\b|HR\/IT|PTO|VPN|parental|Ask Northwind|northwind-hr-assistant/);
  });
  it("offers the canonical model slugs only", () => {
    const html = renderToStaticMarkup(createElement(AdjusterSupportTab));
    for (const slug of ["gpt-5-4", "gpt-5-4-mini", "kimi"]) expect(html).toContain(`value="${slug}"`);
    expect(html).not.toContain("hr-");
  });
  it("uses only the supported backend route and preserves source metadata", async () => {
    const citation = { id: "ADJ-DUTY-001", title: "Fictional duty", version: "1",
      source_uri: "urn:northwind:synthetic:adjuster-support:ADJ-DUTY-001:v1" };
    const fetch = vi.fn().mockResolvedValue(response([
      { kind: "session", session_id: "adjuster-gpt-5-4-unit" },
      { kind: "assistant_delta", text: "Retrieved guidance" },
      { kind: "citations", citations: [citation] }, { kind: "done" },
    ]));
    vi.stubGlobal("fetch", fetch);
    const events = await collect();
    expect(events[2].citations).toEqual([citation]);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/adjuster-support\/gpt-5-4\/chat$/);
    expect(url).not.toContain("/agent/");
    expect(new Headers(init.headers).get("X-Department")).toBe("adjuster-support");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(JSON.parse(init.body)).toEqual({ session_id: null, message: "Evidence?" });
  });
  it("sends each canonical model slug to its own chat route", async () => {
    for (const slug of ["gpt-5-4", "gpt-5-4-mini", "kimi"]) {
      const fetch = vi.fn().mockResolvedValue(response([{ kind: "done" }]));
      vi.stubGlobal("fetch", fetch);
      await collect(slug);
      expect(fetch.mock.calls[0][0]).toMatch(new RegExp(`/adjuster-support/${slug}/chat$`));
    }
  });
  it("accepts citations from documents published under the older source URN prefix", async () => {
    const legacy = { id: "ADJ-EVIDENCE-001", title: "Fictional evidence", version: "1",
      source_uri: "urn:northwind:synthetic:employee-support:ADJ-EVIDENCE-001:v1" };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([{ kind: "citations", citations: [legacy] }, { kind: "done" }])));
    expect((await collect())[0].citations).toEqual([legacy]);
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
