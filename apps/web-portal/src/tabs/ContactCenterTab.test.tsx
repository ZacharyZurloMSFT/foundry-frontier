import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import ContactCenterTab, { confirmedTextHandoff, ContactCenterReview, streamTextHandoff } from "./ContactCenterTab";

vi.mock("../InsuranceContext", () => ({
  useInsurance: () => ({ fixtures: null, loading: false, error: null }),
  InsuranceRuntimeNotice: () => createElement("p", null, "Insurance rollout not ready"),
}));
afterEach(() => vi.unstubAllGlobals());
describe("contact-center confirmed text intake", () => {
  it("labels the fallback as text, not fake live audio, and links the real Voice Live app", () => {
    const html = renderToStaticMarkup(createElement(ContactCenterTab));
    expect(html).toContain("Text intake");
    expect(html).toContain("does not capture live audio");
    expect(html).toContain("https://ca-insurance-voice.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io");
    expect(html).toContain("Confirm handoff to Claims Concierge");
    expect(html).toMatch(/<button type="submit" disabled=""/);
  });
  it("keeps confirmation disabled even when ready until the operator has reviewed the editable form", () => {
    const onConfirm = vi.fn();
    const html = renderToStaticMarkup(createElement(ContactCenterReview, { records: null, ready: true, busy: false, onConfirm }));
    expect(html).toContain("textarea");
    expect(html).toContain("Policyholder ID");
    expect(html).toContain("Insured vehicle / asset");
    expect(html).toContain("No handoff is triggered");
    expect(html).toMatch(/<button type="submit" disabled=""/);
    expect(onConfirm).not.toHaveBeenCalled();
  });
  it("sends the reviewed typed text contract to the existing handoff with the contact-center header", async () => {
    const draft = { summary: "Review only", claim_id: "CLM-2001", policy_id: "POL-1001", policyholder_id: "PH-1001", loss_date: "2026-09-18", state: "WA", intent: "claim_status", urgency: "P3" };
    expect(() => confirmedTextHandoff(draft, "2022 sedan", false)).toThrow("confirm");
    const payload = confirmedTextHandoff(draft, "2022 sedan", true);
    const fetch = vi.fn().mockResolvedValue(new Response('{"kind":"session","session_id":"claims-one"}\n{"kind":"done"}'));
    vi.stubGlobal("fetch", fetch);
    const events = [];
    for await (const event of streamTextHandoff(payload, new AbortController().signal)) events.push(event);
    expect(events.at(-1)).toEqual({ kind: "done" });
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("https://ca-insurance-api.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/handoff");
    expect(init.headers["X-Department"]).toBe("contact-center");
    expect(JSON.parse(init.body)).toEqual(payload);
  });
  it("surfaces mismatched records without retrying or calling a separate claims handler", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Claim and policy do not match" }), { status: 422 }));
    vi.stubGlobal("fetch", fetch);
    await expect((async () => { for await (const _ of streamTextHandoff({ summary: "Review" }, new AbortController().signal)) {} })()).rejects.toThrow("do not match");
    expect(fetch).toHaveBeenCalledOnce();
  });
});
