import { useEffect, useState } from "react";
import { confirmedHandoff, HandoffPayload, InsuranceRecords } from "./api";

export interface HandoffReviewProps {
  records: InsuranceRecords | null;
  ready: boolean;
  busy: boolean;
  summarySuggestion?: string;
  onConfirm: (payload: HandoffPayload) => void;
}
export default function HandoffReview({ records, ready, busy, summarySuggestion, onConfirm }: HandoffReviewProps) {
  const [draft, setDraft] = useState<HandoffPayload>({ summary: "", intent: "claim_status", urgency: "P3" });
  const [asset, setAsset] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (summarySuggestion) { setDraft((d) => ({ ...d, summary: summarySuggestion })); setConfirmed(false); }
  }, [summarySuggestion]);
  function update(key: keyof HandoffPayload, value: string) {
    setDraft((d) => ({ ...d, [key]: value })); setConfirmed(false); setError("");
  }
  function selectClaim(id: string) {
    const claim = records?.claims.find((c) => c.claim_id === id);
    const policy = records?.policies.find((p) => p.policy_id === claim?.policy_id);
    const holder = records?.policyholders.find((p) => p.policyholder_id === claim?.policyholder_id);
    if (!claim || !policy || !holder) return;
    setDraft({
      summary: summarySuggestion || `Caller requests claim-status review for ${claim.claim_id}. Recorded incident: ${claim.description}. Ask Claims Concierge to review the matching record and prepare a human-reviewed next step.`,
      intent: "claim_status", urgency: "P3", caller: holder.name,
      claim_id: claim.claim_id, policy_id: policy.policy_id, policyholder_id: holder.policyholder_id,
      loss_date: claim.loss_date, state: policy.state,
    });
    setAsset(Object.values(policy.insured_asset).filter((v) => typeof v === "string" || typeof v === "number").join(" "));
    setConfirmed(false); setError("");
  }
  return (
    <form className="handoff-review" onSubmit={(event) => {
      event.preventDefault();
      if (!ready || busy) return;
      try { onConfirm(confirmedHandoff(draft, confirmed, asset)); }
      catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    }}>
      <h2>Review insurance intake</h2>
      <p>Editable text intake, not a coverage decision. Caller-provided facts are not verified identity.
        Matching IDs, loss date and state are validated by Claims Concierge before any downstream run.</p>
      <fieldset disabled={busy}>
        <label>Load a synthetic claim record
          <select defaultValue="" onChange={(event) => selectClaim(event.target.value)}>
            <option value="">Select a record or enter details below</option>
            {records?.claims.map((claim) => <option key={claim.claim_id} value={claim.claim_id}>{claim.claim_id} · {claim.policy_id}</option>)}
          </select>
        </label>
        <label>Intent<select value={draft.intent} onChange={(e) => update("intent", e.target.value)}>
          {["fnol", "claim_status", "policy_question", "evidence_submission", "billing", "escalation", "other"].map((intent) =>
            <option key={intent} value={intent}>{intent.replaceAll("_", " ")}</option>)}
        </select></label>
        <label>Urgency<select value={draft.urgency} onChange={(e) => update("urgency", e.target.value)}>
          {["P1", "P2", "P3", "P4"].map((urgency) => <option key={urgency}>{urgency}</option>)}
        </select></label>
        {([
          ["caller", "Caller name", "text", false], ["claim_id", "Claim ID (optional for first notice)", "text", false],
          ["policy_id", "Policy ID", "text", true], ["policyholder_id", "Policyholder ID", "text", true],
          ["loss_date", "Loss date", "date", true], ["state", "State (two-letter)", "text", true],
        ] as const).map(([key, label, type, required]) => <label key={key}>{label}
          <input value={draft[key] || ""} type={type} maxLength={key === "state" ? 2 : 120} required={required}
            onChange={(e) => update(key, key === "state" ? e.target.value.toUpperCase() : e.target.value)} />
        </label>)}
        <label>Insured vehicle / asset<input required maxLength={500} value={asset}
          onChange={(e) => { setAsset(e.target.value); setConfirmed(false); }} /></label>
        <label>Summary, incident facts, missing evidence and requested next step
          <textarea required maxLength={12000} rows={6} value={draft.summary} onChange={(e) => update("summary", e.target.value)} />
        </label>
        <label className="confirmation"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
          I reviewed this summary and confirmed the caller-provided IDs, loss date, state and insured asset.
          I authorize a Claims Concierge handoff for human review only.</label>
      </fieldset>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={!ready || busy || !confirmed}>{busy ? "Handoff in progress…" : "Confirm handoff to Claims Concierge"}</button>
      {!ready && <p>End the call and wait for insurance runtime readiness before confirming.</p>}
      <p>No handoff is triggered by speech, partial transcripts, loading a record or editing this form.</p>
    </form>
  );
}
