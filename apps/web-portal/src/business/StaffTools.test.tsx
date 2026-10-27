import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AccountNavigation, AccountTab, accountTabFor } from "./BusinessPortal";
import Claims, { ClaimDetail } from "./Claims";
import { BusinessApi } from "./api";
import { Role } from "./models";
import { StaffClaimAssistance, StaffTaskContent } from "./StaffTools";
import { claimExample, configExample, identityExample } from "./testData";

const noop = () => {};
const api = new BusinessApi(configExample, async () => "test");
const navigation = (role: Role, tab: AccountTab = "policies") =>
  renderToStaticMarkup(createElement(AccountNavigation, { role, tab, onOpen: noop }));

describe("staff tools inside the insurance journey", () => {
  it("replaces the adjuster workspace entry with direct task navigation", () => {
    const html = navigation("adjuster", "reviews");
    for (const label of ["Policies", "Claim queue", "Document review", "Claim reviews", "Contact center"]) expect(html).toContain(label);
    expect(html).toContain('aria-current="page">Claim reviews');
    expect(html).not.toMatch(/AI workspace|Departments|Report a loss|My claims/);
  });
  it.each<Role>(["policyholder", "presenter"])("does not offer staff mutation tools to a %s", role => {
    const html = navigation(role);
    expect(html).not.toMatch(/AI workspace|Document review|Claim reviews|Contact center/);
    if (role === "policyholder") {
      expect(html).toContain("My claims"); expect(html).toContain("Report a loss");
    } else {
      expect(html).toContain("Claim queue"); expect(html).not.toContain("Report a loss");
    }
    for (const task of ["documents", "reviews", "contact"] as const) {
      expect(accountTabFor(role, task)).toBe("policies");
      expect(renderToStaticMarkup(createElement(StaffTaskContent, { role, task }))).toBe("");
    }
  });
  it("limits customer-only pages and preserves direct adjuster task selection", () => {
    expect(accountTabFor("adjuster", "report")).toBe("claims");
    expect(accountTabFor("presenter", "help")).toBe("claims");
    for (const task of ["documents", "reviews", "contact"] as const) expect(accountTabFor("adjuster", task)).toBe(task);
    expect(accountTabFor("policyholder", "help")).toBe("help");
  });
  it("puts claim draft assistance and procedural guidance beside the queue and case", () => {
    const identity = { ...identityExample, role: "adjuster" as const, policyholder_id: null };
    const queue = renderToStaticMarkup(createElement(Claims, {
      api, identity, selected: null, onSelect: noop, createdNotice: "", onError: noop,
    }));
    const detail = renderToStaticMarkup(createElement(ClaimDetail, {
      api, identity, initialClaim: claimExample, claimId: claimExample.claim_id, onError: noop,
    }));
    for (const html of [queue, detail]) {
      expect(html).toContain("Claims Concierge — draft assistance");
      expect(html).toContain("Adjuster guidance — procedures and review boundaries");
      expect(html).toContain("<details");
      expect(html).not.toMatch(/AI workspace|Northwind Insurance landing|Departments/);
      expect(html).toContain("<summary>Claims Concierge — draft assistance</summary></details>");
      expect(html).toContain("<summary>Adjuster guidance — procedures and review boundaries</summary></details>");
    }
  });
  it("keeps procedural guidance read-only for presenters and out of customer pages", () => {
    const presenter = renderToStaticMarkup(createElement(StaffClaimAssistance, { role: "presenter" }));
    expect(presenter).toContain("Adjuster guidance");
    expect(presenter).not.toContain("draft assistance");
    expect(renderToStaticMarkup(createElement(StaffClaimAssistance, { role: "policyholder" }))).toBe("");
  });
  it("keeps human review and evidence attachment boundaries visible in their task sections", () => {
    const reviews = renderToStaticMarkup(createElement(StaffTaskContent, { task: "reviews", role: "adjuster" }));
    for (const label of ["Prepare a draft", "Pending review", "Reviewed drafts", "nothing approves coverage"]) expect(reviews).toContain(label);
    const documents = renderToStaticMarkup(createElement(StaffTaskContent, { task: "documents", role: "adjuster" }));
    expect(documents).toContain("open that case in Claim queue");
    expect(documents).toContain("Documents and evidence review");
    expect(reviews + documents).not.toMatch(/AI workspace|Departments/);
  });
});
