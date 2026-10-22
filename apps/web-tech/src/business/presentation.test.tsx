import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import BusinessPortal, { PublicHome } from "./BusinessPortal";
import { ClaimDetail } from "./Claims";
import { PolicyView } from "./Policies";
import ReportLoss from "./ReportLoss";
import { ErrorNotice, Pager } from "./shared";
import { BusinessApi, BusinessError } from "./api";
import { claimExample, configExample, identityExample, policyExample } from "./testData";
import App, { insuranceBusinessEnabled } from "../App";

const api = new BusinessApi(configExample, async () => "test");
const noop = () => {};
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
describe("business portal presentation and boundaries", () => {
  it("preserves the disabled legacy providers and never fetches or initializes MSAL while server-rendering", () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("window", { location: { href: "http://localhost:5173/" } });
    vi.stubEnv("VITE_INSURANCE_BUSINESS_ENABLED", "false");
    expect(renderToStaticMarkup(createElement(App))).toContain("Loading Northwind Insurance");
    expect(insuranceBusinessEnabled(undefined)).toBe(true); expect(insuranceBusinessEnabled("false")).toBe(false);
    expect(insuranceBusinessEnabled(false)).toBe(false); expect(insuranceBusinessEnabled("true")).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("renders a public insurance home with no fixture, agent, fake quote or purchase links", () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const html = renderToStaticMarkup(createElement(PublicHome, { onAccount: noop, onCoverage: noop, onHelp: noop }));
    expect(html).toContain("road ahead"); expect(html).toContain("Report a loss"); expect(html).toContain("coverage information");
    expect(html).not.toMatch(/Agent Harness|Claims Concierge|Model bake-off|Get a quote|Buy now/);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("offers only the honest public path with unavailable config", () => {
    for (const key of ["VITE_INSURANCE_AUTH_TENANT_ID", "VITE_INSURANCE_AUTH_CLIENT_ID", "VITE_INSURANCE_AUTH_SCOPE", "VITE_INSURANCE_RUN_ID"]) vi.stubEnv(key, "");
    const html = renderToStaticMarkup(createElement(BusinessPortal));
    expect(html).toContain("Synthetic demonstration"); expect(html).toContain("Claims help"); expect(html).toContain("My account");
    expect(html).not.toContain("persona");
  });
  it("renders source versions, schedule, wording, endorsements, and citations", () => {
    const html = renderToStaticMarkup(createElement(PolicyView, { snapshot: policyExample }));
    for (const text of ["source-v1", "Policy schedule", "Scheduled limits", "Synthetic endorsement", "WORD-TEST#collision", "Version 1", "does not establish coverage"]) expect(html).toContain(text);
    expect(html).not.toContain("href=");
  });
  it("shows conventional synthetic reporting fields with no dummy integration buttons", () => {
    const html = renderToStaticMarkup(createElement(ReportLoss, { api, identity: identityExample, initialPolicy: policyExample, onCreated: noop, onClaims: noop, onError: noop }));
    for (const text of ["loss-policy", "loss-date", "loss-state", "loss-description", "Auto collision", "Auto hail", "synthetic information only", "not part of this form"]) expect(html).toContain(text);
    expect(html).not.toMatch(/Upload file|Extract now|Start voice|Ask assistant/);
  });
  it("keeps customer case views read-only and filters employee-only note and timeline contents defensively", () => {
    const claim = { ...claimExample,
      notes: [{ note_id: "private", body: "employee-only-secret", visibility: "internal" as const, author_id: "employee", created_at: "2026-10-15T15:00:00Z" }],
      timeline: [...claimExample.timeline, { ...claimExample.timeline[0], event_id: "private-event", summary: "private-event-content", internal_note: "private-internal-note", visibility: "internal" as const }],
    };
    const html = renderToStaticMarkup(createElement(ClaimDetail, { api, identity: identityExample, claimId: claim.claim_id, initialClaim: claim, onError: noop }));
    expect(html).toContain("Next evidence steps"); expect(html).toContain("Report received");
    expect(html).not.toMatch(/employee-only-secret|private-event-content|private-internal-note|Adjuster actions|Move workflow/);
    expect(html).not.toContain("Not yet assigned");
  });
  it("provides versioned employee forms only for server-authorized adjusters, never presenter commands", () => {
    const props = { api, claimId: claimExample.claim_id, initialClaim: claimExample, onError: noop };
    const adjuster = renderToStaticMarkup(createElement(ClaimDetail, { ...props, identity: { ...identityExample, role: "adjuster", policyholder_id: null, adjusters: ["adjuster-a"] } }));
    for (const text of ["Adjuster actions", "version 1", "Move workflow", "Create evidence task", "Add case note", "Assign case", "adjuster-a"]) expect(adjuster).toContain(text);
    expect(adjuster).not.toMatch(/value="approved"|value="denied"|value="paid"|value="settled"/);
    const presenter = renderToStaticMarkup(createElement(ClaimDetail, { ...props, identity: { ...identityExample, role: "presenter", policyholder_id: null, adjusters: [] } }));
    expect(presenter).toContain("Presenter access is read-only"); expect(presenter).not.toContain("Adjuster actions");
  });
  it("exposes conflict and service support references in accessible alerts", () => {
    const html = renderToStaticMarkup(createElement(ErrorNotice, { error: new BusinessError("Version changed", 409, "version_conflict", "support-123") }));
    expect(html).toContain('role="alert"'); expect(html).toContain("Conflict"); expect(html).toContain("support-123"); expect(html).toContain("nothing was retried");
  });
  it("disables unusable pagination rather than silently truncating the collection", () => {
    const html = renderToStaticMarkup(createElement(Pager, { offset: 20, next: 20, history: [0], busy: false, onPage: noop }));
    expect(html).toContain("Previous page"); expect(html).toContain("Next page"); expect(html).toContain("invalid next page");
  });
});
