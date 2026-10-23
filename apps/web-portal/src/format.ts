/**
 * Defensive helpers for rendering agent-produced payloads. The
 * claim-action-draft agent can emit fields as objects (e.g.,
 * `insured_asset: {make, model}`) or arrays (e.g., `policy_refs`) instead of
 * scalars, which blows up React with "Objects are not valid as a React child".
 * These helpers coerce any shape into a safe display string.
 */
export function renderField(value: unknown, fallback = "—"): string {
  if (value == null || value === "") return fallback;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    const parts = value.map((v) => renderField(v, "")).filter(Boolean);
    return parts.length ? parts.join(", ") : fallback;
  }
  if (typeof value === "object") {
    // For an object like {make: "Demo", model: "Sedan"} render "Demo Sedan".
    // Nulls/empty values are skipped so `{note: null, claim: "CLM-2001"}` → "CLM-2001".
    const parts = Object.values(value as Record<string, unknown>)
      .map((v) => renderField(v, ""))
      .filter(Boolean);
    if (parts.length) return parts.join(" ");
    try {
      return JSON.stringify(value);
    } catch {
      return fallback;
    }
  }
  return fallback;
}

export function renderList(value: unknown, fallback = "—"): string {
  if (value == null) return fallback;
  if (Array.isArray(value)) {
    const parts = value.map((v) => renderField(v, "")).filter(Boolean);
    return parts.length ? parts.join(", ") : fallback;
  }
  return renderField(value, fallback);
}
