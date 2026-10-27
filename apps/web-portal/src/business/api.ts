import { BusinessConfig } from "./config";
import { ClaimRecord, ClaimStatus, LossReport, parseClaim, parseCreated, parseIdentity, parsePage, parsePolicy, isObject } from "./models";

export class BusinessError extends Error {
  constructor(message: string, public readonly status: number, public readonly code: string,
    public readonly correlationId: string | null, public readonly uncertain = false) { super(message); this.name = "BusinessError"; }
}
export function errorMessage(error: unknown): string { return error instanceof Error ? error.message : "The request could not be completed."; }
export type TokenProvider = () => Promise<string>;
export interface ExchangeOptions { signal?: AbortSignal; readOnly?: boolean }
export class BusinessApi {
  constructor(private readonly config: BusinessConfig, private readonly token: TokenProvider) {}
  async exchange(path: string, method = "GET", body?: BodyInit, extraHeaders?: HeadersInit, options: ExchangeOptions = {}): Promise<Response> {
    // Acquire before sending; interactive-required must never automatically replay a command.
    const token = await this.token();
    const headers = new Headers({ Authorization: `Bearer ${token}`, "X-Demo-Run": this.config.runId, Accept: "application/json" });
    new Headers(extraHeaders).forEach((value, name) => {
      if (name !== "authorization" && name !== "x-demo-run") headers.set(name, value);
    });
    // A read-only POST (for example an assistant question) changes no record, so a lost connection has no unknown outcome.
    const write = method !== "GET" && !options.readOnly;
    let response: Response;
    try {
      response = await fetch(`${this.config.apiBase}/api/v1${path}`, {
        method, headers, credentials: "omit", cache: "no-store", redirect: "error",
        ...(options.signal ? { signal: options.signal } : {}),
        ...(body !== undefined ? { body } : {}),
      });
    } catch (error) {
      if (options.signal?.aborted) throw error;
      throw new BusinessError(write ? "The connection ended. The outcome is unknown; check the current case before another action." : "Unable to reach the insurance service.", 0, "network_error", null, write);
    }
    return response;
  }
  async readJson<T>(response: Response, parser: (value: unknown) => T, write = false): Promise<T> {
    const correlationId = response.headers.get("X-Correlation-ID");
    let value: unknown;
    try { value = await response.json(); } catch {
      throw new BusinessError("The service returned an unreadable response.", response.status, "invalid_response", correlationId, write && response.status !== 401 && response.status !== 403 && response.status !== 409);
    }
    if (!response.ok) {
      const err = isObject(value) && isObject(value.error) ? value.error : null;
      if (!err || typeof err.code !== "string" || typeof err.message !== "string" || typeof err.correlation_id !== "string") {
        throw new BusinessError(`Insurance service error (${response.status}); invalid error envelope.`, response.status, "invalid_error", correlationId, write && response.status >= 500);
      }
      throw new BusinessError(err.message, response.status, err.code, correlationId || err.correlation_id, write && response.status >= 500);
    }
    try {
      const result = parser(value);
      const entity = isObject(result) && isObject(result.claim) ? result.claim : result;
      if (isObject(entity) && typeof entity.version === "number") {
        const etag = response.headers.get("ETag");
        if (etag !== null && etag !== `"${entity.version}"`) throw new Error("Claim ETag does not match version.");
      }
      return result;
    } catch {
      throw new BusinessError("The service response did not match the insurance contract. No substitute records were loaded.", response.status, "invalid_response", correlationId, write);
    }
  }
  async request<T>(path: string, parser: (value: unknown) => T, method = "GET", body?: unknown, idempotencyKey?: string): Promise<T> {
    const headers = new Headers();
    if (body !== undefined) headers.set("Content-Type", "application/json");
    if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);
    return this.readJson(await this.exchange(path, method, body === undefined ? undefined : JSON.stringify(body), headers), parser, method !== "GET");
  }
  me() {
    return this.request("/me", value => {
      const me = parseIdentity(value);
      if (me.run_id !== this.config.runId) throw new Error("Run mismatch");
      return me;
    });
  }
  policies(offset = 0) { return this.request(`/me/policies?offset=${offset}&limit=20`, v => parsePage(v, parsePolicy)); }
  claims(offset = 0) { return this.request(`/me/claims?offset=${offset}&limit=20`, v => parsePage(v, item => this.claimForRun(item))); }
  policy(id: string) { return this.request(`/policies/${encodeURIComponent(id)}`, value => {
    const policy = parsePolicy(value);
    if (policy.policy.policy_id !== id) throw new Error("Policy mismatch");
    return policy;
  }); }
  claim(id: string) { return this.request(`/claims/${encodeURIComponent(id)}`, value => this.claimForId(value, id)); }
  private claimForId(value: unknown, id: string): ClaimRecord {
    const claim = this.claimForRun(value);
    if (claim.claim_id !== id) throw new Error("Claim mismatch");
    return claim;
  }
  private claimForRun(value: unknown): ClaimRecord {
    const claim = parseClaim(value);
    if (claim.run_id !== this.config.runId) throw new Error("Run mismatch");
    return claim;
  }
  create(report: LossReport, key: string) {
    return this.request("/claims", value => {
      const result = parseCreated(value);
      if (result.claim.run_id !== this.config.runId || result.claim.policy_id !== report.policy_id) throw new Error("Loss context mismatch");
      return result;
    }, "POST", report, key);
  }
  transition(id: string, expected_version: number, target: ClaimStatus, reason: string) {
    return this.request(`/claims/${encodeURIComponent(id)}/transitions`, v => this.claimForId(v, id), "POST", { target, expected_version, reason });
  }
  task(id: string, expected_version: number, title: string, requested_evidence: string, assigned_to: string | null) {
    return this.request(`/claims/${encodeURIComponent(id)}/tasks`, v => this.claimForId(v, id), "POST", { expected_version, title, requested_evidence, assigned_to });
  }
  complete(id: string, taskId: string, expected_version: number) {
    return this.request(`/claims/${encodeURIComponent(id)}/tasks/${encodeURIComponent(taskId)}`, v => this.claimForId(v, id), "PATCH", { expected_version, status: "completed" });
  }
  note(id: string, expected_version: number, body: string, visibility: "customer" | "internal") {
    return this.request(`/claims/${encodeURIComponent(id)}/notes`, v => this.claimForId(v, id), "POST", { expected_version, body, visibility });
  }
  assign(id: string, expected_version: number, adjuster_id: string | null) {
    return this.request(`/claims/${encodeURIComponent(id)}/assignment`, v => this.claimForId(v, id), "POST", { expected_version, adjuster_id });
  }
}
