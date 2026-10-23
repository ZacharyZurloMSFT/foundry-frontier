import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ClaimsInboxTab, { boundedDisplay, CUResult, extractLiveSample, parseLiveResult, reviewedHandoff, uploadLiveDocument, uploadType } from "./ClaimsInboxTab";

const field = (valueString: string) => ({ type: "string", valueString, source: "D(1,0,0,1,0,1,1,0,1)" });
const result: CUResult = {
  status: "succeeded", extraction_mode: "live_cu", source_file: "input.pdf", source_sha256: "a".repeat(64),
  operation_id: "actual-op-23", source_text: "Untrusted input", review_required: true,
  result: { analyzerId: "northwindInsuranceClaimsV1", contents: [{ fields: {
    claim_id: { ...field("CLM-LIVE-913"), confidence: 0.61 },
    policy_id: field("POL-LIVE-27"), policyholder_id: field("PH-LIVE-8"),
    loss_date: { type: "date", valueDate: "2026-09-24" },
  } }] },
};
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => vi.unstubAllGlobals());

describe("live insurance CU", () => {
  it("shows an honest configuration check and readonly review boundary", () => {
    const html = renderToStaticMarkup(createElement(ClaimsInboxTab));
    expect(html).toContain("Checking live CU configuration");
    expect(html).toContain("no authored extraction is substituted");
    expect(html).toContain("not ground truth");
    expect(html).not.toContain("golden dataset");
  });
  it("posts only a source filename and returns actual typed fields unchanged", async () => {
    const fetch = vi.fn().mockResolvedValue(response(result));
    vi.stubGlobal("fetch", fetch);
    const actual = await extractLiveSample("input.pdf");
    expect(actual).toEqual(result);
    expect(actual.result.contents[0].fields?.claim_id.confidence).toBe(0.61);
    expect(actual.result.contents[0].fields?.policy_id.confidence).toBeUndefined();
    expect(fetch.mock.calls[0][0]).toMatch(/\/claims\/extract$/);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ filename: "input.pdf" });
  });
  it("uses a raw body and matching content type, never multipart, URL or fixture data", async () => {
    const fetch = vi.fn().mockResolvedValue(response(result));
    vi.stubGlobal("fetch", fetch);
    const file = new File(["%PDF-1.7\n%%EOF"], "input.pdf", { type: "application/pdf" });
    await uploadLiveDocument(file);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/claims\/upload\?filename=input.pdf$/);
    expect(init.body).toBe(file);
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/pdf");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("surfaces 503/4xx and rejects authored or incomplete success responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ detail: { code: "cu_disabled", message: "Live CU disabled" } }, 503)));
    await expect(extractLiveSample("input.pdf")).rejects.toThrow("Live CU disabled");
    expect(() => parseLiveResult({ ...result, extraction_mode: "preauthored_synthetic_sample" })).toThrow("No verified live CU");
    expect(() => parseLiveResult({ ...result, result: { contents: [] } })).toThrow("No verified live CU");
  });
  it("never hands off before review and only uses actual grounded IDs", () => {
    expect(() => reviewedHandoff(result, false)).toThrow("Review the original");
    const payload = reviewedHandoff(result, true);
    expect(payload.claim_id).toBe("CLM-LIVE-913");
    expect(payload.policy_id).toBe("POL-LIVE-27");
    expect(payload.policyholder_id).toBe("PH-LIVE-8");
    expect(payload.loss_date).toBe("2026-09-24");
    expect(JSON.stringify(payload)).not.toContain("Untrusted input");
    expect(JSON.stringify(payload)).not.toContain("CLM-2001");
    const missing = structuredClone(result);
    delete missing.result.contents[0].fields?.policyholder_id;
    expect(() => reviewedHandoff(missing, true)).toThrow("model-extracted policyholder_id");
    const unsupported = structuredClone(result);
    delete unsupported.result.contents[0].fields?.claim_id.source;
    expect(() => reviewedHandoff(unsupported, true)).toThrow("Source evidence");
  });
  it("blocks conflicting IDs and bounds display of untrusted model output", () => {
    const conflict = structuredClone(result);
    conflict.result.contents.push({ fields: { claim_id: field("CLM-OTHER") } });
    expect(() => reviewedHandoff(conflict, true)).toThrow("Conflicting");
    expect(boundedDisplay("x".repeat(30000))).toContain("[Display truncated");
    expect(boundedDisplay("x".repeat(30000)).length).toBeLessThan(24100);
  });
  it("rejects empty, oversized, mismatched types and path/remote URL inputs before upload", () => {
    for (const file of [
      { name: "input.pdf", size: 0, type: "application/pdf" },
      { name: "input.pdf", size: 11 * 1024 * 1024, type: "application/pdf" },
      { name: "input.jpg", size: 10, type: "application/pdf" },
      { name: "../input.pdf", size: 10, type: "application/pdf" },
      { name: "https://example.test/input.pdf", size: 10, type: "application/pdf" },
      { name: "input.md", size: 10, type: "text/markdown" },
    ]) expect(() => uploadType(file)).toThrow();
  });
});
