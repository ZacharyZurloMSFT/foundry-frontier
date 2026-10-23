import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch, currentDepartmentSlug } from "./api";
import { normalizeCompany, readInitialSlug, type Company } from "./CompanyContext";
import { canonicalDepartmentSlug, DEFAULT_DEPARTMENT_SLUG } from "./departments";

const KEY = "northwind.activeDepartment";

function browser(stored: string | null, href = "http://localhost:5173/") {
  const store = new Map<string, string>(stored === null ? [] : [[KEY, stored]]);
  const replaceState = vi.fn();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => { store.set(key, value); },
    removeItem: (key: string) => { store.delete(key); },
  });
  vi.stubGlobal("window", { location: { href }, history: { replaceState } });
  return { store, replaceState };
}

afterEach(() => vi.unstubAllGlobals());

describe("department slug aliases", () => {
  it("maps each deprecated slug to its canonical slug and leaves canonical slugs alone", () => {
    expect(canonicalDepartmentSlug("order-concierge")).toBe("claims-concierge");
    expect(canonicalDepartmentSlug("employee-help-desk")).toBe("adjuster-support");
    expect(canonicalDepartmentSlug("claims-warranty")).toBe("claims-documents");
    for (const slug of ["claims-concierge", "adjuster-support", "claims-documents", "contact-center"]) {
      expect(canonicalDepartmentSlug(slug)).toBe(slug);
    }
  });

  it("trims, lowercases and never resolves inherited object keys", () => {
    expect(canonicalDepartmentSlug("  Order-Concierge ")).toBe("claims-concierge");
    expect(canonicalDepartmentSlug("")).toBe("");
    expect(canonicalDepartmentSlug(null)).toBe("");
    expect(canonicalDepartmentSlug(undefined)).toBe("");
    expect(canonicalDepartmentSlug("constructor")).toBe("constructor");
    expect(canonicalDepartmentSlug("__proto__")).toBe("__proto__");
  });

  it("migrates a deprecated slug persisted in localStorage and writes the canonical one back", () => {
    const { store, replaceState } = browser("employee-help-desk");
    expect(readInitialSlug()).toBe("adjuster-support");
    expect(store.get(KEY)).toBe("adjuster-support");
    expect(replaceState).toHaveBeenCalledOnce();
    expect(String(replaceState.mock.calls[0][2])).toContain("dept=adjuster-support");
  });

  it("migrates a deprecated ?dept= parameter, which wins over persisted state", () => {
    const { store } = browser("contact-center", "http://localhost:5173/?dept=claims-warranty");
    expect(readInitialSlug()).toBe("claims-documents");
    expect(store.get(KEY)).toBe("claims-documents");
  });

  it("does not rewrite state that is already canonical or empty", () => {
    const canonical = browser("claims-concierge");
    expect(readInitialSlug()).toBe("claims-concierge");
    expect(canonical.replaceState).not.toHaveBeenCalled();
    const empty = browser(null);
    expect(readInitialSlug()).toBeNull();
    expect(empty.store.size).toBe(0);
  });

  it("maps deprecated slugs in a /company response", () => {
    const department = (slug: string) => ({
      slug, name: slug, tagline: "", pillar: "rag", icon: "", brand_color: "", foundry_project: "p",
      search_index: "i", enabled_tabs: [], customer_archetype: "",
    });
    const company: Company = {
      slug: "northwind", name: "Northwind Insurance", tagline: "", brand_color: "", hero: "",
      platform_tabs: [{ id: "agent_harness", label: "Agent Harness", hosted_in_project: "p" }],
      departments: ["order-concierge", "employee-help-desk", "claims-warranty", "contact-center"].map(department),
    };
    const normalized = normalizeCompany(company);
    expect(normalized.departments.map((d) => d.slug)).toEqual([
      "claims-concierge", "adjuster-support", "claims-documents", "contact-center",
    ]);
    expect(normalized.platform_tabs).toEqual(company.platform_tabs);
    expect(company.departments[0].slug).toBe("order-concierge");
  });
});

describe("X-Department header", () => {
  const sent = async (path = "/health", init: RequestInit = {}) => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    await apiFetch(path, init);
    return new Headers(fetch.mock.calls[0][1].headers).get("X-Department");
  };

  it("sends the canonical slug for a deprecated persisted slug", async () => {
    browser("order-concierge");
    expect(currentDepartmentSlug()).toBe("claims-concierge");
    expect(await sent()).toBe("claims-concierge");
  });

  it("canonicalizes an explicit header and defaults when nothing is persisted", async () => {
    browser("adjuster-support");
    expect(await sent("/adjuster-support/status", { headers: { "X-Department": "employee-help-desk" } })).toBe("adjuster-support");
    browser(null);
    expect(await sent()).toBe(DEFAULT_DEPARTMENT_SLUG);
  });

  it("falls back to the default when browser storage is unavailable", () => {
    vi.stubGlobal("localStorage", { getItem: () => { throw new Error("blocked"); } });
    expect(currentDepartmentSlug()).toBe("claims-concierge");
  });
});
