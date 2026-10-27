import { useEffect, useState } from "react";
import { BusinessApi } from "./api";
import { Page, PolicySnapshot, statusLabel } from "./models";
import { DateText, ErrorNotice, Loading, Pager } from "./shared";
import AssistantPanel from "./assistants/AssistantPanel";
import { wordingAnchor } from "./assistants/wording";

const money = (value: number) => new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(value);
export function PolicyView({ snapshot }: { snapshot: PolicySnapshot }) {
  const { policy: p, policyholder, policy_wording: wording } = snapshot;
  return <section aria-label={`Policy ${p.policy_id}`} className="business-stack">
    <div className="business-panel">
      <div className="business-heading"><div><p className="business-eyebrow">Policy schedule</p><h3>{p.policy_id}</h3></div><span className="business-badge">{statusLabel(p.status)}</span></div>
      <p>{p.insured_asset.description}</p>
      <dl className="business-facts">
        <div><dt>Policyholder</dt><dd>{policyholder.name}</dd></div>
        <div><dt>Jurisdiction</dt><dd>{p.state}</dd></div>
        <div><dt>Effective period</dt><dd><DateText value={p.effective_from} /> – <DateText value={p.effective_to} /></dd></div>
        <div><dt>Vehicle identification</dt><dd><code>{p.insured_asset.vin}</code></dd></div>
        <div><dt>Source version</dt><dd><code>{snapshot.source_version}</code></dd></div>
        <div><dt>Wording ID</dt><dd><code>{p.wording_id}</code></dd></div>
      </dl>
      <h4>Coverage schedule</h4>
      <div className="business-table-wrap"><table><caption>Scheduled limits and deductibles, not a coverage determination</caption><thead><tr><th scope="col">Coverage</th><th scope="col">Limit</th><th scope="col">Deductible</th></tr></thead>
        <tbody>{p.coverages.map((c, i) => <tr key={`${c.coverage_type}-${i}`}><th scope="row">{statusLabel(c.coverage_type)}</th><td>{money(c.limit_usd)}</td><td>{money(c.deductible_usd)}</td></tr>)}</tbody></table></div>
      <p className="business-muted">The schedule alone does not establish coverage for a loss. Applicable wording, endorsements, dates, facts, and human review are required.</p>
      <h4>Endorsements</h4>
      {!p.endorsements.length && <p>No endorsements returned by the service.</p>}
      <ul>{p.endorsements.map(e => <li key={e.endorsement_id}><strong>{e.title}</strong> · {e.endorsement_id} · Version {e.version} · {e.state}<br />
        <DateText value={e.effective_from} /> – <DateText value={e.effective_to} /></li>)}</ul>
    </div>
    <details className="business-panel">
      <summary>Read policy wording and source references</summary>
      <h4>{wording.title}</h4><p>Version {wording.version} · {wording.state} · <DateText value={wording.effective_from} /> – <DateText value={wording.effective_to} /></p>
      {wording.sections.map(s => <section className="business-source" key={s.section_id} id={wordingAnchor(s.policy_ref)} tabIndex={-1}><h5>{s.section_id}</h5><code>{s.policy_ref}</code><p className="business-prewrap">{s.text}</p><p className="business-muted">Source: <code>{s.source_uri}</code></p></section>)}
      <h4>Versioned citations</h4>
      {snapshot.citations.map((s, i) => <section className="business-source" key={`${s.section_id}-${i}`}><h5>{s.title} · Version {s.version}</h5><code>{s.policy_ref}</code>
        <p>{s.state} · <DateText value={s.effective_from} /> – <DateText value={s.effective_to} /></p><p className="business-prewrap">{s.text}</p><p>Source: <code>{s.source_uri}</code></p></section>)}
    </details>
  </section>;
}
export default function Policies({ api, canReport, onReport, onError }: {
  api: BusinessApi; canReport: boolean; onReport: (policy: PolicySnapshot) => void; onError: (error: unknown) => void;
}) {
  const [page, setPage] = useState<Page<PolicySnapshot> | null>(null);
  const [offset, setOffset] = useState(0), [history, setHistory] = useState<number[]>([]);
  const [selected, setSelected] = useState<PolicySnapshot | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null), [reload, setReload] = useState(0);
  useEffect(() => {
    let current = true; setBusy(true); setError(null); setPage(null); setSelected(null);
    api.policies(offset).then(data => { if (current) setPage(data); }).catch(e => { if (current) { setError(e); onError(e); } }).finally(() => { if (current) setBusy(false); });
    return () => { current = false; };
  }, [api, offset, reload, onError]);
  async function open(id: string) {
    setBusy(true); setError(null); setSelected(null);
    try { setSelected(await api.policy(id)); } catch (e) { setError(e); onError(e); } finally { setBusy(false); }
  }
  return <section aria-labelledby="policies-title">
    <div className="business-heading"><div><p className="business-eyebrow">Your protection, in detail</p><h2 id="policies-title">Policies</h2></div><button disabled={busy} onClick={() => setReload(v => v + 1)}>Refresh policies</button></div>
    <AssistantPanel surface="concierge" context={selected ? { policy_id: selected.policy.policy_id } : undefined} onError={onError} />
    <ErrorNotice error={error} />{busy && <Loading />}
    {selected ? <><button className="business-back" onClick={() => setSelected(null)}>← Back to policies</button><PolicyView snapshot={selected} />
      {canReport && <button className="business-primary" onClick={() => onReport(selected)}>Report a loss on this policy</button>}</> : page && <>
      {!page.items.length && <p>No policies returned for your account on this page.</p>}
      <div className="business-grid">{page.items.map(p => <article className="business-panel" key={p.policy.policy_id}>
        <p className="business-eyebrow">Auto policy · {p.policy.state}</p><h3>{p.policy.insured_asset.description}</h3>
        <p><code>{p.policy.policy_id}</code> <span className="business-badge">{statusLabel(p.policy.status)}</span></p>
        <p><DateText value={p.policy.effective_from} /> – <DateText value={p.policy.effective_to} /></p>
        <p className="business-muted">Source version: {p.source_version}</p>
        <button disabled={busy} onClick={() => void open(p.policy.policy_id)}>View schedule and wording</button>
      </article>)}</div>
      <Pager offset={offset} next={page.next_offset} history={history} busy={busy} onPage={(next, back) => {
        setHistory(h => back ? h.slice(0, -1) : [...h, offset]); setOffset(next);
      }} />
    </>}
  </section>;
}
