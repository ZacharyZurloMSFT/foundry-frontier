/**
 * Company / department context — the top of the "one company, four
 * Foundry projects" UI reshape. Fetches the manifest from `GET /company`
 * at startup and exposes:
 *   - `company` — the full manifest (departments, platform tabs, brand)
 *   - `department` — the currently active department (default:
 *     `claims-concierge`, persisted in localStorage + `?dept=` in URL)
 *   - `setDepartment(slug)` — swap the active department
 *
 * Department slugs are always canonical here: deprecated slugs read from
 * localStorage, the `?dept=` URL parameter or an older `/company` response
 * are mapped by `canonicalDepartmentSlug` (see departments.ts).
 *
 * Every API call in the app that wants to be department-scoped should
 * pass the active department via the `X-Department` header — see
 * `apiFetch()` in api.ts.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { canonicalDepartmentSlug, DEFAULT_DEPARTMENT_SLUG } from "./departments";

export interface Department {
  slug: string;
  name: string;
  tagline: string;
  pillar: "multi_agent" | "rag" | "content_understanding" | "voice_live" | string;
  icon: string;
  brand_color: string;
  foundry_project: string;
  search_index: string;
  enabled_tabs: string[];
  customer_archetype: string;
}

export interface PlatformTab {
  id: string;
  label: string;
  hosted_in_project: string;
}

export interface Company {
  slug: string;
  name: string;
  tagline: string;
  brand_color: string;
  hero: string;
  platform_tabs: PlatformTab[];
  departments: Department[];
}

interface DepartmentCtx {
  company: Company | null;
  department: Department | null;
  setDepartment: (slug: string | null) => void;
  loading: boolean;
  error: string | null;
}

const CompanyContext = createContext<DepartmentCtx>({
  company: null,
  department: null,
  setDepartment: () => {},
  loading: true,
  error: null,
});

const LS_KEY = "northwind.activeDepartment";
const API_BASE = (import.meta as any).env.VITE_API_BASE_URL || "http://localhost:8000";

/**
 * Initial department: the `?dept=` URL parameter, else the persisted slug.
 * A deprecated slug is mapped to its canonical slug and the canonical value
 * is written back, so the migration happens once per browser.
 */
export function readInitialSlug(): string | null {
  try {
    const url = new URL(window.location.href);
    const requested = url.searchParams.get("dept") || localStorage.getItem(LS_KEY);
    const slug = canonicalDepartmentSlug(requested) || null;
    if (slug && slug !== requested) writeSlug(slug);
    return slug;
  } catch {
    return null;
  }
}

function writeSlug(slug: string | null) {
  try {
    if (slug) localStorage.setItem(LS_KEY, slug);
    else localStorage.removeItem(LS_KEY);
    const url = new URL(window.location.href);
    if (slug) url.searchParams.set("dept", slug);
    else url.searchParams.delete("dept");
    window.history.replaceState({}, "", url.toString());
  } catch { /* ignore — offline dev */ }
}

/** Maps any deprecated department slug in a `/company` response to its canonical slug. */
export function normalizeCompany(company: Company): Company {
  return {
    ...company,
    departments: company.departments.map((d) => ({ ...d, slug: canonicalDepartmentSlug(d.slug) || d.slug })),
  };
}

export function CompanyProvider({ children }: { children: ReactNode }) {
  const [company, setCompany] = useState<Company | null>(null);
  const [slug, setSlug] = useState<string | null>(readInitialSlug);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`${API_BASE}/company`);
        if (!r.ok) throw new Error(`company fetch failed: ${r.status}`);
        const c = normalizeCompany((await r.json()) as Company);
        if (!cancelled) {
          setCompany(c);
          setLoading(false);
        }
      } catch (e: any) {
        if (!cancelled) {
          setError(String(e?.message || e));
          setLoading(false);
        }
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const setDepartment = useCallback((s: string | null) => {
    const canonical = s ? canonicalDepartmentSlug(s) : null;
    setSlug(canonical);
    writeSlug(canonical);
  }, []);

  const department = useMemo<Department | null>(() => {
    if (!company || !slug) return null;
    return company.departments.find((d) => d.slug === slug) ?? null;
  }, [company, slug]);

  return (
    <CompanyContext.Provider value={{ company, department, setDepartment, loading, error }}>
      {children}
    </CompanyContext.Provider>
  );
}

export function useCompany() {
  return useContext(CompanyContext);
}

/** Returns the active department slug, or 'claims-concierge' as the safe default. */
export function useActiveDepartmentSlug(): string {
  const { department } = useContext(CompanyContext);
  return department?.slug ?? DEFAULT_DEPARTMENT_SLUG;
}
