export interface BusinessConfig { tenantId: string; clientId: string; scope: string; runId: string; apiBase: string }
export type ConfigResult = { ok: true; value: BusinessConfig } | { ok: false; message: string };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function businessTheme(param: string | null, prefersDark: boolean): "light" | "dark" {
  return param === "light" || param === "dark" ? param : prefersDark ? "dark" : "light";
}
export function readBusinessConfig(env: Record<string, unknown>): ConfigResult {
  const tenantId = env.VITE_INSURANCE_AUTH_TENANT_ID, clientId = env.VITE_INSURANCE_AUTH_CLIENT_ID;
  const scope = env.VITE_INSURANCE_AUTH_SCOPE, runId = env.VITE_INSURANCE_RUN_ID;
  if (typeof tenantId !== "string" || !uuid.test(tenantId) || typeof clientId !== "string" || !uuid.test(clientId)
    || typeof scope !== "string" || !/^(api:\/\/|https:\/\/)[^\s]+\/[^\s/]+$/.test(scope)
    || typeof runId !== "string" || !/^[A-Za-z0-9_-]{1,64}$/.test(runId)) {
    return { ok: false, message: "My account is unavailable. Configure VITE_INSURANCE_AUTH_TENANT_ID and VITE_INSURANCE_AUTH_CLIENT_ID (UUIDs), VITE_INSURANCE_AUTH_SCOPE (full delegated API scope URI), and VITE_INSURANCE_RUN_ID. No demo account is substituted." };
  }
  const base = typeof env.VITE_API_BASE_URL === "string" && env.VITE_API_BASE_URL ? env.VITE_API_BASE_URL : "http://localhost:8000";
  try {
    const url = new URL(base);
    if (url.username || url.password || url.search || url.hash || !["http:", "https:"].includes(url.protocol)) throw new Error();
    if (url.protocol !== "https:" && !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) throw new Error();
    return { ok: true, value: { tenantId, clientId, scope, runId, apiBase: base.replace(/\/+$/, "") } };
  } catch {
    return { ok: false, message: "My account is unavailable: VITE_API_BASE_URL must be an HTTPS API URL (HTTP is allowed only for localhost), without credentials, query, or fragment." };
  }
}
