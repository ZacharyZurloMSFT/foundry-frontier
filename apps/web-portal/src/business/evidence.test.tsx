import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BusinessApi } from "./api";
import { EvidenceApi, evidenceCommand, evidenceStorageSlot, sha256 } from "./evidenceApi";
import { EvidenceDocument, EvidenceJob, parseEvidenceDocument, parseEvidenceJob, parseEvidenceReceipt, parseEvidenceStatus } from "./evidenceModels";
import DocumentsPanel, { fieldReview, FieldReviewForm } from "./DocumentsPanel";
import { claimExample, configExample, identityExample } from "./testData";
import { RetryStorage } from "./reportRetry";

const docId = "doc-11111111-1111-4111-8111-111111111111";
const jobId = "job-22222222-2222-4222-8222-222222222222";
const document: EvidenceDocument = {
  document_id: docId, claim_id: claimExample.claim_id, filename: "synthetic.pdf", media_type: "application/pdf",
  size_bytes: 17, sha256: "a".repeat(64), version: 1, created_at: "2026-10-16T15:00:00Z",
  source_mode: "synthetic_sample", uploaded_by: null, job_id: jobId, review_version: 0,
  fields: { estimate_total_usd: { type: "number", value: 250, confidence: .74, missing: false, source_spans: [{ page: 1, span: "untrusted text" }] } },
  confirmed_fields: {}, notice: "Synthetic source. Extracted suggestions require human review.",
};
const job: EvidenceJob = {
  job_id: jobId, document_id: docId, claim_id: claimExample.claim_id, status: "queued", version: 1, attempts: 0, max_attempts: 3,
  error_code: null, error_message: null, created_at: document.created_at, updated_at: document.created_at, notice: "Durable job; no facts confirmed.",
};
function storage(): RetryStorage {
  const values = new Map<string, string>();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => { values.set(key, value); }, removeItem: key => { values.delete(key); } };
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const business = () => new BusinessApi(configExample, async () => "synthetic-test-access-token");
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("durable evidence contracts", () => {
  it("validates linked document/job receipts and preserves available source spans", () => {
    expect(parseEvidenceDocument(document)).toEqual(document);
    expect(parseEvidenceJob(job)).toEqual(job);
    expect(parseEvidenceReceipt({ document, job, replayed: false }).document.fields.estimate_total_usd?.source_spans).toEqual([{ page: 1, span: "untrusted text" }]);
    expect(() => parseEvidenceReceipt({ document, job: { ...job, document_id: "other" }, replayed: true })).toThrow();
  });
  it.each([
    { sha256: "invalid" }, { size_bytes: 10485761 }, { version: 0 }, { media_type: "text/html" },
    { fields: { payment_approved: { type: "boolean", value: true } } },
    { fields: { estimate_total_usd: { type: "number", value: Number.NaN, confidence: null, missing: false, source_spans: [] } } },
    { fields: { loss_date: { type: "date", value: "2026-10-01", confidence: 1.1, missing: false, source_spans: [] } } },
  ])("rejects malformed document data without success-shaped fallbacks: %j", changes => {
    expect(() => parseEvidenceDocument({ ...document, ...changes })).toThrow();
  });
  it("rejects invalid job states, attempts, and advertised upload limits", () => {
    expect(() => parseEvidenceJob({ ...job, status: "approved" })).toThrow();
    expect(() => parseEvidenceJob({ ...job, attempts: 4 })).toThrow();
    expect(() => parseEvidenceStatus({ enabled: true, uploads_enabled: true, extraction_enabled: false, worker_enabled: false, max_upload_bytes: 10485761, notice: "Bad limit" })).toThrow();
  });
});

describe("authorised evidence transport", () => {
  it("uses only scoped business routes and verifies returned claim and job relationships", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ items: [document], next_offset: null }))
      .mockResolvedValueOnce(json({ document, job, replayed: false }, 202))
      .mockResolvedValueOnce(json(job));
    vi.stubGlobal("fetch", fetch);
    const api = new EvidenceApi(business());
    expect((await api.documents(claimExample.claim_id)).items[0]).toEqual(document);
    await api.sample(claimExample.claim_id, "synthetic.pdf", 1, "evidence-one");
    await api.job(jobId, claimExample.claim_id, docId);
    expect(new Headers(fetch.mock.calls[1][1].headers).get("Idempotency-Key")).toBe("evidence-one");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ filename: "synthetic.pdf", expected_version: 1, synthetic_acknowledged: true });
    expect(fetch.mock.calls.every(call => call[0].includes("/api/v1/") && !call[0].includes("token"))).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(3);
  });
  it("streams bounded upload bytes with explicit synthetic acknowledgement and the original key", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ document: { ...document, source_mode: "synthetic_upload" }, job, replayed: true }, 202));
    vi.stubGlobal("fetch", fetch);
    const file = new File(["%PDF-1.7\n%%EOF"], "synthetic.pdf", { type: "application/pdf" });
    await new EvidenceApi(business()).upload(claimExample.claim_id, file, 3, "same-command");
    const [url, init] = fetch.mock.calls[0], headers = new Headers(init.headers);
    expect(url).toContain("expected_version=3"); expect(init.body).toBe(file);
    expect(headers.get("X-Synthetic-Data")).toBe("true");
    expect(headers.get("Idempotency-Key")).toBe("same-command");
    expect(headers.get("Content-Type")).toBe("application/pdf");
  });
  it("binds raw download bytes to recorded MIME, size and SHA before opening", async () => {
    const bytes = new TextEncoder().encode("%PDF-1.7\n%%EOF"), recorded = { ...document, size_bytes: bytes.length, sha256: await sha256(bytes.buffer) };
    const fetch = vi.fn().mockResolvedValueOnce(new Response(bytes, { headers: { "Content-Type": "application/pdf" } }))
      .mockResolvedValueOnce(new Response(bytes, { headers: { "Content-Type": "text/html" } }))
      .mockResolvedValueOnce(new Response(bytes, { headers: { "Content-Type": "application/pdf" } }));
    vi.stubGlobal("fetch", fetch);
    const api = new EvidenceApi(business());
    expect((await api.source(recorded)).size).toBe(bytes.length);
    await expect(api.source(recorded)).rejects.toThrow("invalid type");
    await expect(api.source({ ...recorded, sha256: "b".repeat(64) })).rejects.toThrow("integrity");
  });
  it("preserves safe errors and never retries uncertain mutations", async () => {
    const fetch = vi.fn().mockResolvedValue(json({ error: { code: "cu_submission_uncertain", message: "Operator reconciliation required.", correlation_id: "safe-id" } }, 409));
    vi.stubGlobal("fetch", fetch);
    await expect(new EvidenceApi(business()).jobCommand("retry", jobId, claimExample.claim_id, docId, 1)).rejects.toMatchObject({ status: 409, code: "cu_submission_uncertain" });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("rejects a source that exceeds its recorded length before producing a download", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("too many bytes", { headers: { "Content-Type": "application/pdf" } })));
    await expect(new EvidenceApi(business()).source({ ...document, size_bytes: 2 })).rejects.toThrow("recorded size");
  });
  it("does not permit caller headers to replace the authenticated identity or run", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response()); vi.stubGlobal("fetch", fetch);
    await business().exchange("/evidence/status", "GET", undefined, { Authorization: "spoofed", "X-Demo-Run": "other-run" });
    const headers = new Headers(fetch.mock.calls[0][1].headers);
    expect(headers.get("Authorization")).not.toBe("spoofed");
    expect(headers.get("X-Demo-Run")).toBe(configExample.runId);
  });
});

describe("evidence retry and human boundaries", () => {
  it("retains only key/hash, binds the command to source content, run, actor and claim", async () => {
    const values = storage(), slot = evidenceStorageSlot("run-a", identityExample.actor_key, claimExample.claim_id);
    const first = await evidenceCommand(values, slot, "sample", "synthetic.pdf");
    expect(await evidenceCommand(values, slot, "sample", "synthetic.pdf")).toEqual(first);
    expect(Object.keys(JSON.parse(values.getItem(slot)!))).toEqual(["key", "hash"]);
    expect(values.getItem(slot)).not.toContain("synthetic.pdf");
    await expect(evidenceCommand(values, slot, "sample", "different.pdf")).rejects.toThrow("document differs");
    expect(evidenceStorageSlot("run-b", identityExample.actor_key, claimExample.claim_id)).not.toBe(slot);
    expect(evidenceStorageSlot("run-a", "other-actor", claimExample.claim_id)).not.toBe(slot);
    expect(evidenceStorageSlot("run-a", identityExample.actor_key, "other-claim")).not.toBe(slot);
  });
  it("allows only explicit accepted/corrected/cleared field decisions", () => {
    expect(fieldReview("estimate_total_usd", "correct", "123.45")).toEqual({ name: "estimate_total_usd", decision: "correct", value: 123.45 });
    expect(fieldReview("claim_id", "accept", "ignored")).toEqual({ name: "claim_id", decision: "accept" });
    expect(fieldReview("loss_date", "clear", "ignored")).toEqual({ name: "loss_date", decision: "clear" });
    for (const value of ["", "-1", "Infinity", "123.456", "true"]) expect(() => fieldReview("estimate_total_usd", "correct", value)).toThrow();
  });
  it("detects changed upload bytes even when the filename is unchanged", async () => {
    const values = storage(), firstFile = new File(["synthetic source one"], "same.pdf", { type: "application/pdf" });
    const command = await evidenceCommand(values, "source-slot", "upload", firstFile.name, firstFile);
    expect(await evidenceCommand(values, "source-slot", "upload", firstFile.name, firstFile)).toEqual(command);
    const changed = new File(["synthetic source two"], "same.pdf", { type: "application/pdf" });
    await expect(evidenceCommand(values, "source-slot", "upload", changed.name, changed)).rejects.toThrow("differs");
    expect(values.getItem("source-slot")).not.toContain("synthetic source");
  });
  it("refuses empty and oversized uploads without buffering or hashing them", async () => {
    const values = storage();
    await expect(evidenceCommand(values, "slot", "upload", "empty.pdf", new File([], "empty.pdf"))).rejects.toThrow("nonempty");
    const oversized = new File([new Uint8Array(10485761)], "large.pdf");
    await expect(evidenceCommand(values, "slot", "upload", oversized.name, oversized)).rejects.toThrow("10 MiB");
    expect(values.getItem("slot")).toBeNull();
  });
  it("renders raw suggestions separately from confirmed facts with manual review available", () => {
    const html = renderToStaticMarkup(createElement(FieldReviewForm, { document, name: "estimate_total_usd", busy: false, locked: false, review: async () => {} }));
    for (const phrase of ["Untrusted extracted suggestion", "Human-confirmed value", "Not reviewed", "74%", "Inspect source spans", "Enter a manually confirmed value"]) expect(html).toContain(phrase);
    expect(html).not.toContain("Payment approved");
  });
  it("renders capability discovery without dummy upload/model controls", () => {
    const html = renderToStaticMarkup(createElement(DocumentsPanel, {
      claim: claimExample, identity: identityExample, api: business(), caseBusy: false, caseLocked: false,
      onCaseChanged: () => {}, onError: () => {},
    }));
    expect(html).toContain("Documents and evidence review");
    expect(html).not.toContain("Store document and queue processing");
    expect(html).not.toContain("Start voice");
  });
});
