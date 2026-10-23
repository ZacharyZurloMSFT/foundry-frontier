/** Live insurance CU. Source/model output stays read-only until human review. */
import { useEffect, useState } from "react";
import { apiFetch, HandoffPayload, streamHandoff } from "../api";

export interface CUSample {
  id: string;
  source_file: string;
  title: string;
  synthetic: boolean;
  media_type: string;
}
export interface CUField {
  type: string;
  valueString?: string;
  valueDate?: string;
  confidence?: number;
  source?: string;
  spans?: { offset: number; length: number }[];
  [key: string]: unknown;
}
export interface CUResult {
  status: "succeeded";
  extraction_mode: "live_cu";
  source_file: string;
  source_sha256: string;
  operation_id: string;
  source_text: string;
  review_required: true;
  result: { analyzerId: string; contents: { fields?: Record<string, CUField>; markdown?: string; [key: string]: unknown }[]; [key: string]: unknown };
}
interface Availability { available: boolean; message: string; max_upload_bytes?: number; analysis_verified?: boolean }
const MAX_BYTES = 10 * 1024 * 1024;
const DISPLAY_LIMIT = 24000;
const types: Record<string, string> = { pdf: "application/pdf", jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png" };

export function uploadType(file: Pick<File, "name" | "size" | "type">, limit = MAX_BYTES): string {
  const expected = types[file.name.split(".").pop()?.toLowerCase() || ""];
  if (!/^[A-Za-z0-9][A-Za-z0-9._ -]{0,159}$/.test(file.name) || file.name.includes("..")) throw new Error("Use a plain PDF/JPEG/PNG filename, not a path or URL.");
  if (!expected || (file.type && file.type !== expected)) throw new Error("Only matching PDF, JPEG or PNG files are supported.");
  if (!file.size) throw new Error("The document is empty.");
  if (file.size > Math.min(limit, MAX_BYTES)) throw new Error("Document exceeds the upload limit (at most 10 MiB).");
  return expected;
}

export function parseLiveResult(value: unknown): CUResult {
  const r = value as CUResult;
  if (!r || r.extraction_mode !== "live_cu" || r.status !== "succeeded" || r.review_required !== true ||
      typeof r.source_file !== "string" || typeof r.operation_id !== "string" || typeof r.source_text !== "string" ||
      !/^[a-f0-9]{64}$/.test(r.source_sha256 || "") || r.result?.analyzerId !== "northwindInsuranceClaimsV1" ||
      !Array.isArray(r.result.contents) || r.result.contents.length === 0 ||
      r.result.contents.some(c => !c || (c.fields !== undefined && (
        typeof c.fields !== "object" || c.fields === null || Array.isArray(c.fields) ||
        Object.values(c.fields).some(f => !f || typeof f !== "object" || typeof f.type !== "string")
      )))) {
    throw new Error("No verified live CU response was returned. Authored/expected extraction samples are not used.");
  }
  return r;
}

function uniqueField(result: CUResult, key: string): CUField | undefined {
  const fields = result.result.contents.map(c => c.fields?.[key]).filter((f): f is CUField => !!f);
  const values = new Set(fields.map(f => f.valueString ?? f.valueDate));
  if (values.size > 1) throw new Error(`Conflicting extracted ${key}; review the source before handoff.`);
  return fields[0];
}

export function reviewedHandoff(result: CUResult, reviewed: boolean): HandoffPayload {
  parseLiveResult(result);
  if (!reviewed) throw new Error("Review the original document, identifiers and evidence before handoff.");
  const ids: Record<string, string> = {};
  for (const [key, prefix] of [["claim_id", "CLM"], ["policy_id", "POL"], ["policyholder_id", "PH"]]) {
    const field = uniqueField(result, key);
    const value = field?.valueString;
    if (field?.type !== "string" || typeof value !== "string" || !new RegExp(`^${prefix}-[A-Za-z0-9-]{1,40}$`).test(value)) {
      throw new Error(`A valid model-extracted ${key} is required; no fixture identifier will be substituted.`);
    }
    const source = typeof field.source === "string" && /^D\([0-9]+,[0-9., -]+\)(?:;D\([0-9]+,[0-9., -]+\))*$/.test(field.source);
    const spans = Array.isArray(field.spans) && field.spans.length > 0 && field.spans.every(span =>
      Number.isInteger(span.offset) && span.offset >= 0 && Number.isInteger(span.length) && span.length > 0);
    if (!source && !spans) throw new Error(`Source evidence for ${key} was not supplied by CU; handoff is unavailable.`);
    ids[key] = value;
  }
  const loss = uniqueField(result, "loss_date")?.valueDate;
  const state = uniqueField(result, "state")?.valueString;
  return {
    summary: `Human-reviewed, untrusted CU source identifiers: claim ${ids.claim_id}, policy ${ids.policy_id}, policyholder ${ids.policyholder_id}. Verify matching authorised records before preparing an internal human-review handoff. Model output is not ground truth. No coverage decision, cause/fraud finding, policy update or payment is authorised.`,
    claim_id: ids.claim_id, policy_id: ids.policy_id, policyholder_id: ids.policyholder_id,
    ...(loss && /^\d{4}-\d{2}-\d{2}$/.test(loss) ? { loss_date: loss } : {}),
    ...(state && /^[A-Z]{2}$/.test(state) ? { state } : {}),
    intent: "evidence_submission",
  };
}

export function boundedDisplay(text: string): string {
  return text.length > DISPLAY_LIMIT ? text.slice(0, DISPLAY_LIMIT) + "\n[Display truncated; review the original input.]" : text;
}

async function jsonResponse(response: Response): Promise<any> {
  if (!response.body) throw new Error("CU returned an empty response.");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > 8 * 1024 * 1024) { await reader.cancel(); throw new Error("CU response exceeded the result limit."); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  let body;
  try { body = JSON.parse(new TextDecoder().decode(bytes)); } catch { throw new Error(`Invalid CU response (${response.status}).`); }
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : body.detail?.message;
    throw new Error(typeof detail === "string" ? detail.slice(0, 500) : `Live CU unavailable (${response.status}); no sample fallback.`);
  }
  return body;
}

export async function extractLiveSample(filename: string, signal?: AbortSignal): Promise<CUResult> {
  return parseLiveResult(await jsonResponse(await apiFetch("/claims/extract", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename }), signal,
  })));
}

export async function uploadLiveDocument(file: File, limit = MAX_BYTES, signal?: AbortSignal): Promise<CUResult> {
  const mediaType = uploadType(file, limit);
  return parseLiveResult(await jsonResponse(await apiFetch(`/claims/upload?filename=${encodeURIComponent(file.name)}`, {
    method: "POST", headers: { "Content-Type": mediaType }, body: file, signal,
  })));
}

export default function ClaimsInboxTab() {
  const [samples, setSamples] = useState<CUSample[]>([]);
  const [availability, setAvailability] = useState<Availability | null>(null);
  const [result, setResult] = useState<CUResult | null>(null);
  const [preview, setPreview] = useState<{ url: string; filename: string } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [error, setError] = useState("");
  const [handoff, setHandoff] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    apiFetch("/claims/samples", { signal: controller.signal }).then(jsonResponse).then(body => {
      if (!Array.isArray(body.items) || typeof body.availability?.available !== "boolean") throw new Error("Live CU catalog/readiness is unavailable.");
      setSamples(body.items); setAvailability(body.availability);
    }).catch(e => { if (!controller.signal.aborted) { setError(String(e.message || e)); setAvailability({ available: false, message: "Live CU unavailable." }); } });
    return () => controller.abort();
  }, []);
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview.url); }, [preview]);

  async function extract(sample?: CUSample) {
    if (!sample && !file) return;
    setBusy(true); setError(""); setResult(null); setReviewed(false); setHandoff(""); setPreview(null);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    try {
      if (sample) {
        const source = await apiFetch(`/claims/samples/${encodeURIComponent(sample.source_file)}/content`, { signal: controller.signal });
        if (!source.ok) { await jsonResponse(source); throw new Error("Original source document unavailable."); }
        const blob = await source.blob();
        if (blob.size > MAX_BYTES) throw new Error("Original source document exceeds the input limit.");
        setPreview({ url: URL.createObjectURL(blob), filename: sample.source_file });
        setResult(await extractLiveSample(sample.source_file, controller.signal));
      } else if (file) {
        uploadType(file, availability?.max_upload_bytes);
        setPreview({ url: URL.createObjectURL(file), filename: file.name });
        setResult(await uploadLiveDocument(file, availability?.max_upload_bytes, controller.signal));
      }
    } catch (e: any) {
      setError(e?.name === "AbortError" ? "Request timed out/stopped. Service compute may continue; no result or fallback is claimed." : String(e?.message || e));
    } finally { clearTimeout(timer); setBusy(false); }
  }

  async function sendHandoff() {
    if (!result) return;
    setError(""); setBusy(true); setHandoff("");
    try {
      const payload = reviewedHandoff(result, reviewed);
      let text = "";
      for await (const event of streamHandoff(payload)) {
        if (event.kind === "error") throw new Error(event.message || "Handoff failed.");
        if (event.text) { text = boundedDisplay(text + event.text + "\n"); setHandoff(text); }
        if (event.session_id && !text) setHandoff(`Internal handoff session: ${event.session_id}. No claim decision or payment was made.`);
      }
    } catch (e: any) { setError(String(e?.message || e)); }
    finally { setBusy(false); }
  }

  let handoffReady = false;
  if (result && reviewed) { try { reviewedHandoff(result, true); handoffReady = true; } catch { /* Missing/conflicting source evidence blocks submission. */ } }
  return (
    <div className="claims-tab">
      <div className="claims-header">
        <h2>Claims Document Intelligence</h2>
        <p className="subtitle">Live insurance Content Understanding · FNOL, loss reports, repair estimates and invoices.
          Documents and model output are untrusted evidence, not ground truth. No coverage, cause, fault, fraud or payment determination.</p>
      </div>
      <div className="claims-empty" role="status">
        {!availability ? "Checking live CU configuration…" : availability.available ? "Live CU analyzer configured — each extraction is a new model request." : `Live CU unavailable: ${availability.message}`}
      </div>
      {error && <div className="claims-error" role="alert">{boundedDisplay(error)}</div>}
      <div className="claims-body">
        <div className="claims-list">
          <div className="claims-list-header">Synthetic source PDFs · no saved extraction</div>
          {samples.map(sample => (
            <button key={sample.id} className="claims-list-item" disabled={busy || !availability?.available} onClick={() => extract(sample)}>
              <div className="claims-item-title">{sample.title}</div><div className="claims-item-sub">{sample.source_file}</div>
            </button>
          ))}
          <label className="claims-list-header" htmlFor="cu-file">Or choose a PDF/JPEG/PNG (at most 10 MiB)</label>
          <input id="cu-file" type="file" accept=".pdf,.jpg,.jpeg,.png" disabled={busy} onChange={event => {
            const selected = event.target.files?.[0] || null;
            setFile(null); setError("");
            if (selected) { try { uploadType(selected, availability?.max_upload_bytes); setFile(selected); } catch (e: any) { setError(String(e.message)); } }
          }} />
          <button className="chip" disabled={!file || busy || !availability?.available} onClick={() => extract()}>Extract uploaded document live</button>
          <div className="claims-loop-note">Input is sent to Azure CU, not saved or indexed by this API. Cloud processing may incur charges. No public blob URL or background worker is required.</div>
        </div>
        <div className="claims-detail">
          {preview && <div className="claims-pane-header">Original input · <a href={preview.url} download={preview.filename}>Download {preview.filename} for review</a></div>}
          {!result && <div className="claims-empty">{busy ? "Waiting for actual CU analysis (bounded polling)…" : "Choose a source PDF or upload a document. If CU is unavailable, no authored extraction is substituted."}</div>}
          {result && <>
            <div className="claims-pane-header">LIVE CU result · <code>{result.result.analyzerId}</code> · operation <code>{result.operation_id}</code></div>
            <div className="claims-loop-note">SHA-256: <code>{result.source_sha256}</code>. Missing confidence/grounding stays missing; never assumed to be 100%. Review all low-confidence and unsupported fields.</div>
            <div className="claims-panes">
              <div className="claims-pane"><div className="claims-pane-header">CU document readback (not the original)</div><pre className="claims-source">{boundedDisplay(result.source_text || "CU did not return document text.")}</pre></div>
              <div className="claims-pane"><div className="claims-pane-header">Actual fields, confidence, source pages / spans</div><pre className="claims-json">{boundedDisplay(JSON.stringify(result.result, null, 2))}</pre></div>
            </div>
            <div className="claims-loop">
              <label><input type="checkbox" checked={reviewed} disabled={busy} onChange={e => setReviewed(e.target.checked)} /> I reviewed the original, extracted IDs and source evidence; identifiers are not identity verification.</label>
              <button className="chip" disabled={busy || !handoffReady} onClick={sendHandoff}>Send reviewed identifiers to Claims Concierge</button>
              <span className="claims-loop-note">Requires all three extracted claim/policy/policyholder IDs with CU grounding. Only identifiers are handed off, not document instructions. No automatic claim approval or policy update.</span>
            </div>
            {handoff && <pre className="claims-source" aria-label="Internal handoff response">{boundedDisplay(handoff)}</pre>}
          </>}
        </div>
      </div>
    </div>
  );
}
