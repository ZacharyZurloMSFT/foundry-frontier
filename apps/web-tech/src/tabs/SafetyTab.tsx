import { useEffect, useState } from "react";
import {
  SafetyAttack, SafetyVerdict, checkSafety, getSafetyAttacks,
} from "../api";

export default function SafetyTab() {
  const [attacks, setAttacks] = useState<SafetyAttack[]>([]);
  const [input, setInput] = useState("");
  const [verdict, setVerdict] = useState<SafetyVerdict | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { getSafetyAttacks().then(r => setAttacks(r.attacks)).catch(() => {}); }, []);

  async function run(text: string) {
    setBusy(true); setVerdict(null);
    try { setVerdict(await checkSafety(text)); }
    finally { setBusy(false); }
  }

  return (
    <div className="safety">
      <div className="safety-header">
        <h2>Try to jailbreak</h2>
        <p className="subtitle">
          Prompt Shields + Protected Material + PII detection — first-party
          content-safety guarantees baked into the Foundry runtime. Pick a
          canned attack or paste your own and watch the shield fire.
        </p>
      </div>

      <div className="safety-canned">
        {attacks.map(a => (
          <button key={a.id} className="canned" onClick={() => { setInput(a.prompt); run(a.prompt); }}>
            <div className="canned-label">{a.label}</div>
            <div className="canned-preview">{a.prompt.slice(0, 100)}{a.prompt.length > 100 ? "…" : ""}</div>
          </button>
        ))}
      </div>

      <div className="safety-input">
        <textarea rows={4} placeholder="Paste any user turn to check…"
                  value={input} onChange={e => setInput(e.target.value)} disabled={busy} />
        <button className="primary" disabled={busy || !input.trim()} onClick={() => run(input)}>
          {busy ? "Checking…" : "Run safety check"}
        </button>
      </div>

      {verdict && <VerdictCard v={verdict} />}
    </div>
  );
}

function VerdictCard({ v }: { v: SafetyVerdict }) {
  return (
    <div className={`verdict verdict-${v.allowed ? "allowed" : "blocked"}`}>
      <div className="verdict-top">
        <span className={`pill ${v.allowed ? "ok" : "block"}`}>
          {v.allowed ? "✓ allowed" : "🛡 shielded"}
        </span>
              <span className="engine">engine: {v.engine}{v.guardrail ? ` · ${v.guardrail}` : ""}</span>
      </div>

      <div className="verdict-grid">
        <Section title="Prompt Shields">
          {Object.entries(v.shields).length === 0 && <em>no shield data</em>}
          {Object.entries(v.shields).map(([k, hit]) => (
            <div key={k} className={`row ${hit ? "hit" : "clean"}`}>
              <span>{k}</span>
              <span>{hit ? "detected" : "clean"}</span>
            </div>
          ))}
        </Section>

        <Section title="Harm categories">
          {Object.entries(v.categories).length === 0 && <em>none</em>}
          {Object.entries(v.categories).map(([cat, sev]) => (
            <div key={cat} className="row">
              <span>{cat}</span><span>severity {sev}</span>
            </div>
          ))}
        </Section>

        <Section title="Protected material">
          <div className={`row ${v.protected_material ? "hit" : "clean"}`}>
            <span>copyright / lyrics / code</span>
            <span>{v.protected_material ? "detected" : "clean"}</span>
          </div>
        </Section>

        <Section title={`PII findings (${v.pii_findings.length})`}>
          {v.pii_findings.length === 0 && <em>none</em>}
          {v.pii_findings.map((f, i) => (
            <div key={i} className="row hit">
              <span>{f.kind}</span>
              <span><code>{f.text}</code></span>
            </div>
          ))}
        </Section>
      </div>

      {v.pii_findings.length > 0 && (
        <div className="redacted">
          <div className="redacted-label">Redacted text (safe to persist)</div>
          <pre>{v.redacted_text}</pre>
        </div>
      )}

      {(v.model_response || v.blocked_reason) && (
        <div className={`model-reply ${v.allowed ? "" : "blocked"}`}>
          <div className="model-reply-label">
            {v.blocked_reason
              ? `🛡 Model reply — blocked (${v.model_deployment || "unknown"})`
              : `💬 Model reply — ${v.model_deployment || "unknown"}`}
            {v.finish_reason && (
              <span className="finish-reason">finish_reason: {v.finish_reason}</span>
            )}
          </div>
          {v.blocked_reason
            ? <p className="blocked-explainer">{v.blocked_reason}</p>
            : <pre className="model-reply-text">{v.model_response || "(empty)"}</pre>}
        </div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: any }) {
  return (
    <div className="verdict-section">
      <div className="verdict-section-title">{title}</div>
      <div>{children}</div>
    </div>
  );
}
