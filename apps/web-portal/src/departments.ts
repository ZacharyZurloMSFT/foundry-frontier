/**
 * Department slugs. The canonical slugs are what the API, the `/company`
 * manifest and the `X-Department` header use. Deprecated slugs can still arrive
 * from persisted browser state, a bookmarked `?dept=` URL or an older `/company`
 * response; they are mapped to the canonical slug here and nowhere else.
 */
export const DEFAULT_DEPARTMENT_SLUG = "claims-concierge";

/** Deprecated slug -> canonical slug. Mirrors `LEGACY_DEPARTMENT_SLUGS` in apps/api/app/company.py. */
export const LEGACY_DEPARTMENT_SLUGS: Readonly<Record<string, string>> = {
  "order-concierge": "claims-concierge",
  "employee-help-desk": "adjuster-support",
  "claims-warranty": "claims-documents",
};

/** Trims, lowercases and maps a deprecated alias to its canonical slug. Returns "" for an empty value. */
export function canonicalDepartmentSlug(slug: string | null | undefined): string {
  const value = (slug ?? "").trim().toLowerCase();
  return Object.hasOwn(LEGACY_DEPARTMENT_SLUGS, value) ? LEGACY_DEPARTMENT_SLUGS[value] : value;
}
