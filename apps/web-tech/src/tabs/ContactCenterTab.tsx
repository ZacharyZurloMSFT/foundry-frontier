import { useEffect, useRef, useState } from "react";
import { HandoffPayload } from "../api";
import { InsuranceRuntimeNotice, useInsurance } from "../InsuranceContext";

const API_BASE = ((import.meta as any).env?.VITE_API_BASE_URL ||
  "https://ca-api.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io").replace(/\/$/, "");

export function confirmedTextHandoff(draft: HandoffPayload, asset: string, confirmed: boolean): HandoffPayload {
  if (!confirmed) throw new Error("Review and confirm the intake before handoff.");
  if (!draft.summary.trim() || draft.summary.length > 12000 || !draft.policy_id?.trim() ||
      !draft.policyholder_id?.trim() || !draft.loss_date || !draft.state || !asset.trim()) {
    throw new Error("Enter summary, policy and policyholder IDs, loss date, state and asset. Never guess missing values.");
  }
  const date = new Date(`${draft.loss_date}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(draft.loss_date) || !Number.isFinite(date.getTime()) ||
      date.toISOString().slice(0, 10) !== draft.loss_date || !/^[A-Z]{2}$/.test(draft.state)) {
    throw new Error("Enter a valid loss date and two-letter state.");
  }
  const payload: HandoffPayload = {
    summary: `${draft.summary.trim()}\nOperator-confirmed insured asset: ${asset.trim()}. Human-reviewed next step only; no coverage decision or payment.`,
  };
  for (const key of ["caller", "intent", "urgency", "claim_id", "policy_id", "policyholder_id", "loss_date", "state"] as const) {
    if (draft[key]?.trim()) payload[key] = draft[key]!.trim();
  }
  return payload;
}

export async function* streamTextHandoff(payload: HandoffPayload, signal: AbortSignal): AsyncGenerator<Record<string, any>> {
  const response = await fetch(`${API_BASE}/handoff`, {
    method: "POST", signal,
    headers: { "Content-Type": "application/json", "X-Department": "contact-center" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : `Handoff failed (${response.status}). Check IDs, date and state.`);
  }
  if (!response.body) throw new Error("Handoff response stream is unavailable.");
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let pending = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      if (pending.length > 1024 * 1024) throw new Error("Handoff stream buffer limit reached.");
      let index: number;
      while ((index = pending.indexOf("\n")) >= 0) {
        const line = pending.slice(0, index).trim(); pending = pending.slice(index + 1);
        if (line) yield JSON.parse(line);
      }
      if (done) { if (pending.trim()) yield JSON.parse(pending); break; }
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

export function ContactCenterReview({ records, ready, busy, onConfirm }: {
  records: ReturnType<typeof useInsurance>["fixtures"]; ready: boolean; busy: boolean;
  onConfirm: (payload: HandoffPayload) => void;
}) {
  const [draft, setDraft] = useState<HandoffPayload>({ summary: "", intent: "claim_status", urgency: "P3" });
  const [asset, setAsset] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  function update(key: keyof HandoffPayload, value: string) {
    setDraft((d) => ({ ...d, [key]: value })); setConfirmed(false); setError("");
  }
  return <form onSubmit={(event) => {
    event.preventDefault();
    if (!ready || busy) return;
    try { onConfirm(confirmedTextHandoff(draft, asset, confirmed)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }}>
    <h3>Review insurance intake</h3>
    <p>Caller-provided facts are not verified identity. The service validates linked IDs, date and state before any Claims run.</p>
    <fieldset disabled={busy} style={{ display: "grid", gap: 12 }}>
      <label>Load a synthetic claim record <select defaultValue="" onChange={(event) => {
        const claim = records?.claims.find((c) => c.claim_id === event.target.value);
        const policy = records?.policies.find((p) => p.policy_id === claim?.policy_id);
        const holder = records?.policyholders.find((p) => p.policyholder_id === claim?.policyholder_id);
        if (!claim || !policy || !holder) return;
        setDraft({
          summary: `Caller requests claim-status review for ${claim.claim_id}. Recorded incident: ${claim.description}. Prepare a human-reviewed next step only.`,
          caller: holder.name, claim_id: claim.claim_id, policy_id: policy.policy_id, policyholder_id: holder.policyholder_id,
          loss_date: claim.loss_date, state: policy.state, intent: "claim_status", urgency: "P3",
        });
        setAsset(Object.values(policy.insured_asset).filter((v) => typeof v === "string" || typeof v === "number").join(" "));
        setConfirmed(false);
      }}>
        <option value="">Select a record or enter caller details</option>
        {records?.claims.map((claim) => <option key={claim.claim_id} value={claim.claim_id}>{claim.claim_id} · {claim.policy_id}</option>)}
      </select></label>
      <label>Intent <select value={draft.intent} onChange={(e) => update("intent", e.target.value)}>
        {["fnol", "claim_status", "policy_question", "evidence_submission", "billing", "escalation", "other"].map((intent) => <option key={intent}>{intent}</option>)}
      </select></label>
      <label>Urgency <select value={draft.urgency} onChange={(e) => update("urgency", e.target.value)}>
        {["P1", "P2", "P3", "P4"].map((urgency) => <option key={urgency}>{urgency}</option>)}
      </select></label>
      {([
        ["caller", "Caller name", "text", false], ["claim_id", "Claim ID (optional for first notice)", "text", false],
        ["policy_id", "Policy ID", "text", true], ["policyholder_id", "Policyholder ID", "text", true],
        ["loss_date", "Loss date", "date", true], ["state", "State (two-letter)", "text", true],
      ] as const).map(([key, label, type, required]) => <label key={key}>{label} <input type={type} required={required}
        maxLength={key === "state" ? 2 : 120} value={draft[key] || ""}
        onChange={(e) => update(key, key === "state" ? e.target.value.toUpperCase() : e.target.value)} /></label>)}
      <label>Insured vehicle / asset <input required maxLength={500} value={asset}
        onChange={(e) => { setAsset(e.target.value); setConfirmed(false); }} /></label>
      <label>Summary, incident facts, missing evidence and requested next step <textarea required rows={6}
        maxLength={12000} value={draft.summary} onChange={(e) => update("summary", e.target.value)} /></label>
      <label><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
        I reviewed the summary and confirmed caller-provided IDs, date, state and insured asset.
        I authorize a Claims Concierge handoff for human review only.</label>
    </fieldset>
    {error && <p role="alert">{error}</p>}
    <button type="submit" disabled={!ready || busy || !confirmed}>{busy ? "Handoff in progress…" : "Confirm handoff to Claims Concierge"}</button>
    <p>No handoff is triggered by loading a record or editing this form. No coverage decision or payment is executed.</p>
  </form>;
}

export default function ContactCenterTab() {
  const { fixtures, loading, error } = useInsurance();
  const ready = fixtures?.agent_runtime_ready === true && !loading && !error;
  const [busy, setBusy] = useState(false);
  const [feed, setFeed] = useState<string[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const [intakeVersion, setIntakeVersion] = useState(0);
  const locked = useRef(false);
  const controller = useRef<AbortController>();
  useEffect(() => () => controller.current?.abort(), []);
  async function handoff(payload: HandoffPayload) {
    if (!ready || locked.current) return;
    locked.current = true; setSubmitted(true); setBusy(true);
    setFeed(["Sending confirmed text intake to Claims Concierge…"]);
    const abort = new AbortController(); controller.current = abort;
    const timeout = setTimeout(() => abort.abort(), 180000);
    let session = false, done = false;
    try {
      const status = await fetch(`${API_BASE}/insurance/fixtures`, { signal: abort.signal });
      if (!status.ok) throw new Error(`Insurance runtime readiness check failed (${status.status}).`);
      const current = await status.json();
      if (current.business_domain !== "insurance" || current.agent_runtime_ready !== true) throw new Error("Insurance runtime is not ready.");
      for await (const event of streamTextHandoff(payload, abort.signal)) {
        if (event.kind === "error") throw new Error(event.message || "Handoff failed.");
        if (event.kind === "session") { session = true; setFeed((f) => [...f, `Claims session: ${event.session_id}`]); }
        if (event.kind === "assistant_delta" && event.text) setFeed((f) => [...f.slice(0, -1), `${f.at(-1)}${event.text}`]);
        if (event.kind === "agent") setFeed((f) => [...f, `\n${event.agent}: `]);
        if (event.kind === "done") done = true;
      }
      if (!session || !done) throw new Error("Handoff stream ended without completion. Check the Claims session before retrying.");
      setFeed((f) => [...f, "Claims review completed. Review proposed drafts in Approvals; no coverage decision or payment was executed."]);
    } catch (reason) {
      setFeed((f) => [...f, `${reason instanceof Error ? reason.message : String(reason)} No automatic retry was made. Check the prior Claims session before resubmitting.`]);
    } finally { clearTimeout(timeout); setBusy(false); }
  }
  return <div className="cc-tab">
    <h2>Policyholder Contact Center</h2>
    <p>Text intake and confirmed Claims Concierge handoff. This console does not capture live audio.
      For microphone intake, open the <a href="https://ca-web-voice.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io" target="_blank" rel="noreferrer">Voice Live app</a>.
      Voice Live readiness is checked independently there.</p>
    <InsuranceRuntimeNotice />
    <ContactCenterReview key={intakeVersion} records={fixtures} ready={ready && !submitted} busy={busy}
      onConfirm={(payload) => { void handoff(payload); }} />
    {feed.length > 0 && <section className="cc-feed" aria-label="Claims Concierge handoff stream" aria-live="polite">
      {feed.map((line, i) => <p key={i} style={{ whiteSpace: "pre-wrap" }}>{line}</p>)}
      {!busy && <button onClick={() => { locked.current = false; setSubmitted(false); setIntakeVersion((v) => v + 1); setFeed([]); }}>Start another intake (check prior session first)</button>}
    </section>}
  </div>;
}
