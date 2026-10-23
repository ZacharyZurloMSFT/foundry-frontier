import { useState, type FormEvent } from "react";
import { useInsurance } from "../InsuranceContext";
import { ACTION_LABELS, actionDraft, defaultDraftReferences, draftInsuranceAction, type ActionType, type DraftReceipt, type InsuranceClaim } from "../insuranceApi";
import InsuranceEndorsement from "../components/InsuranceEndorsement";

const usd = (value: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value);
const display = (value: string | Record<string, unknown>) => typeof value === "string" ? value : JSON.stringify(value);

export default function InsuranceRecordsTab({ onApprovals }: { onApprovals: () => void }) {
  const { fixtures, loading, error, refresh } = useInsurance();
  const [selected, setSelected] = useState("");
  const claim = fixtures?.claims.find((c) => c.claim_id === selected) ?? fixtures?.claims[0];
  const policy = fixtures?.policies.find((p) => p.policy_id === claim?.policy_id);
  const holder = fixtures?.policyholders.find((h) => h.policyholder_id === claim?.policyholder_id);
  const wording = fixtures?.policy_wordings?.find((w) => w.wording_id === policy?.wording_id && w.state === policy?.state);

  return <section className="claims-tab insurance-records">
    <div className="claims-header">
      <h2>Claims / Policies</h2>
      <p className="subtitle">Northwind Insurance · fictional property-and-casualty records. Policy terms are illustrative, not a coverage determination or legal advice.</p>
      <button className="chip" disabled={loading} onClick={refresh}>Refresh records</button>
    </div>
    {loading && <div className="claims-empty" role="status">Loading insurance records…</div>}
    {error && <div className="claims-error" role="alert">{error} No offline fallback is used.</div>}
    {!loading && fixtures && <div className="claims-body">
      <div className="claims-list">
        <div className="claims-list-header">Auto claims</div>
        {fixtures.claims.map((c) => <button key={c.claim_id} className={`claims-list-item ${claim?.claim_id === c.claim_id ? "active" : ""}`}
          onClick={() => setSelected(c.claim_id)}>
          <div className="claims-item-title">{c.claim_id} · {c.incident_type.replaceAll("_", " ")}</div>
          <div className="claims-item-sub">{c.policy_id} · Loss {c.loss_date} · {c.status.replaceAll("_", " ")}</div>
        </button>)}
        {!fixtures.claims.length && <p className="claims-empty">No insurance claims returned.</p>}
        <details className="insurance-policy-list">
          <summary>All policies ({fixtures.policies.length})</summary>
          {fixtures.policies.map((p) => <div key={p.policy_id} className="record-card">
            <strong>{p.policy_id}</strong> · {p.status} · {p.state}
            <p>{p.effective_from} – {p.effective_to}</p>
            <p>{fixtures.policyholders.find((h) => h.policyholder_id === p.policyholder_id)?.name} · {display(p.insured_asset)}</p>
            <pre className="claims-json">{JSON.stringify({ coverages: p.coverages, endorsements: p.endorsements }, null, 2)}</pre>
          </div>)}
        </details>
      </div>
      {claim && policy && holder && <div className="claims-detail insurance-detail">
        <div className="record-card">
          <h3>{claim.claim_id} · {holder.name}</h3>
          <p>{holder.policyholder_id} · {holder.email} · {holder.phone}</p>
          <p><strong>Loss date:</strong> {claim.loss_date} · <strong>Status:</strong> {claim.status.replaceAll("_", " ")}</p>
          <p>{claim.description}</p>
          <h4>Outstanding evidence</h4>
          {claim.outstanding_evidence.length ? <ul>{claim.outstanding_evidence.map((e) => <li key={e}>{e}</li>)}</ul> : <p>No outstanding evidence listed. This does not establish coverage or liability.</p>}
        </div>
        <div className="record-card">
          <h3>Policy {policy.policy_id}</h3>
          <p>{policy.status} · Jurisdiction {policy.state} · Effective {policy.effective_from} through {policy.effective_to}</p>
          <p><strong>Insured vehicle:</strong> {display(policy.insured_asset)}</p>
          <table className="insurance-coverages"><caption>Illustrative coverage schedule (USD)</caption>
            <thead><tr><th>Coverage</th><th>Limit</th><th>Deductible</th></tr></thead>
            <tbody>{policy.coverages.map((c) => <tr key={c.coverage_type}><td>{c.coverage_type.replaceAll("_", " ")}</td><td>{usd(c.limit_usd)}</td><td>{usd(c.deductible_usd)}</td></tr>)}</tbody>
          </table>
          <h4>Endorsements</h4>
          {policy.endorsements.length ? <ul>{policy.endorsements.map((e, i) => <li key={i}><InsuranceEndorsement endorsement={e} /></li>)}</ul> : <p>None listed.</p>}
          <p className="claims-loop-note">Record citations: <code>{policy.policy_id}</code>, <code>{claim.claim_id}</code> from <code>GET /insurance/fixtures</code>. Compare loss date, form version and jurisdiction with the complete policy before any human decision.</p>
          {wording && <details open className="insurance-sources">
            <summary>Versioned policy wording · {wording.wording_id}</summary>
            <p>{wording.title} · Version {wording.version} · Jurisdiction {wording.state} · Effective {wording.effective_from} – {wording.effective_to}</p>
            {wording.sections.map((source) => <div key={source.policy_ref}>
              <h4>{source.section_id.replaceAll("-", " ")}</h4><p>{source.text}</p>
              <p className="claims-loop-note">Citation: <code>{source.policy_ref}</code><br />Source: <code>{source.source_uri}</code></p>
            </div>)}
          </details>}
        </div>
        <DraftForm key={`${claim.claim_id}:${fixtures.dataset_version ?? "v1"}`} claim={claim}
          sourceRefs={wording?.sections.map((s) => s.policy_ref) ?? []}
          defaultRefs={defaultDraftReferences(claim, wording)} onApprovals={onApprovals} />
      </div>}
    </div>}
  </section>;
}

function DraftForm({ claim, sourceRefs, defaultRefs, onApprovals }: { claim: InsuranceClaim; sourceRefs: string[]; defaultRefs: string[]; onApprovals: () => void }) {
  const [action, setAction] = useState<ActionType>("request_evidence");
  const [summary, setSummary] = useState("");
  const [refs, setRefs] = useState(defaultRefs.join(", "));
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState<DraftReceipt | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || receipt) return;
    setError(null); setBusy(true);
    try {
      const draft = actionDraft(claim, action, summary, refs, amount);
      setReceipt(await draftInsuranceAction(draft));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  }
  return <form className="record-card insurance-draft" onSubmit={submit}>
    <h3>Prepare an action draft</h3>
    <p>No claim is approved, denied or paid here. A saved draft enters the existing human approval queue; approving it records an internal decision only, never a payment or external message.</p>
    <fieldset disabled={busy || !!receipt}>
      <label>Proposed action<select value={action} onChange={(e) => { setAction(e.target.value as ActionType); setAmount(""); }}>
        {Object.entries(ACTION_LABELS).map(([value, label]) => <option key={value} value={value}
          disabled={value === "settlement_recommendation" && !sourceRefs.length}>{label}</option>)}
      </select></label>
      <label>Summary / evidence rationale<textarea required maxLength={2000} rows={4} value={summary} onChange={(e) => setSummary(e.target.value)}
        placeholder="Describe the proposed next step, missing evidence and what a human should review." /></label>
      <label>Source references (comma-separated)<input value={refs} maxLength={2000} onChange={(e) => setRefs(e.target.value)} /></label>
      <p className="claims-loop-note">{sourceRefs.length ? `Available form citations (confirm relevance to your draft): ${sourceRefs.join(", ")}` : "No versioned wording returned. Non-financial drafts may omit references; settlement recommendations are unavailable."}</p>
      {action === "settlement_recommendation" && <label>Proposed amount (USD, required; not a payment)<input required type="number" min="0.01" max="100000" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} /></label>}
      <button className="btn" disabled={!summary.trim()} type="submit">{busy ? "Saving draft…" : "Save draft for human review"}</button>
    </fieldset>
    {error && <p className="claims-error" role="alert">{error} If submission was interrupted, check Approvals before retrying.</p>}
    {receipt && <div className="insurance-notice" role="status">
      <strong>Draft saved · {receipt.status.replaceAll("_", " ")}</strong>
      <p>Approval record <code>{receipt.hitl_id}</code> · Workflow <code>{receipt.workflow_id}</code></p>
      <button className="chip" type="button" onClick={onApprovals}>Open Approvals</button>
    </div>}
  </form>;
}
