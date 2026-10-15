import { useEffect, useMemo, useRef, useState } from "react";
import {
  BakeoffEvent, BakeoffModel, getBakeoffModels, streamBakeoff,
} from "../api";

type Column = {
  model: BakeoffModel;
  text: string;
  status: "idle" | "streaming" | "done" | "error";
  latency_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  cost_usd?: number;
  error?: string;
};

const PROMPT_PLACEHOLDER =
  "Compare simulated insurance responses across configured Azure-direct models.";

export default function BakeoffTab() {
  const [models, setModels] = useState<BakeoffModel[] | null>(null);
  const [columns, setColumns] = useState<Column[]>([]);
  const [input, setInput] = useState(
    "For fictional claim CLM-2001 / policy POL-1001, reported loss 2026-09-18, draft an intake checklist and an empathetic update requesting outstanding evidence. Cite the applicable synthetic wording, version, effective dates and jurisdiction. Do not infer coverage, fault, fraud or payment; flag human review.");
  const [running, setRunning] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    getBakeoffModels().then(r => {
      setModels(r.models);
      setColumns(r.models.map(m => ({ model: m, text: "", status: "idle" })));
    }).catch((error: unknown) => setLoadError(error instanceof Error ? error.message : String(error)));
  }, []);

  const cheapest = useMemo(() => {
    const done = columns.filter(c => c.status === "done" && (c.cost_usd ?? Infinity) > 0);
    if (!done.length) return null;
    return done.reduce((a, b) => (a.cost_usd! < b.cost_usd! ? a : b)).model.deployment;
  }, [columns]);
  const fastest = useMemo(() => {
    const done = columns.filter(c => c.status === "done" && c.latency_ms);
    if (!done.length) return null;
    return done.reduce((a, b) => (a.latency_ms! < b.latency_ms! ? a : b)).model.deployment;
  }, [columns]);

  async function run() {
    if (!input.trim() || !models?.length) return;
    setRunning(true);
    setColumns(cs => cs.map(c => ({ ...c, text: "", status: "streaming",
      latency_ms: undefined, prompt_tokens: undefined,
      completion_tokens: undefined, cost_usd: undefined, error: undefined })));
    try {
      for await (const ev of streamBakeoff(input) as AsyncGenerator<BakeoffEvent>) {
        setColumns(cs => applyEvent(cs, ev));
      }
    } catch (e: any) {
      setColumns(cs => cs.map(c => c.status === "streaming"
        ? { ...c, status: "error", error: String(e?.message || e) } : c));
    } finally {
      setRunning(false);
    }
  }

  if (loadError) return <div className="claims-error" role="alert">{loadError} Refresh the page to retry; no simulated models are substituted.</div>;
  if (models === null) return <div className="empty">Loading bake-off models…</div>;
  if (!models.length) return (
    <div className="empty">
      No bake-off deployments configured. Set <code>FOUNDRY_BAKEOFF_DEPLOYMENTS</code>
      (comma-separated Azure-direct deployment names, e.g. <code>gpt-5.4,o4-mini,gpt-5.4-mini</code>).
    </div>
  );

  return (
    <div className="bakeoff">
      <div className="bakeoff-header">
        <div>
          <h2>Model bake-off</h2>
          <p className="subtitle">
            Same prompt, {models.length} <b>Azure-direct</b> deployments, one Foundry project,
            one UAMI, one SDK. No third-party keys, no re-plumbing.
            {" "}Insurance comparison simulation: no agent tools, actions, claims decisions
            or payments. Model output is illustrative and requires human review.
          </p>
        </div>
        <div className="bakeoff-badges">
          {fastest && <span className="badge fast">⚡ fastest: {fastest}</span>}
          {cheapest && <span className="badge cheap">💰 cheapest: {cheapest}</span>}
        </div>
      </div>

      <div className="bakeoff-input">
        <textarea value={input} onChange={e => setInput(e.target.value)}
                  placeholder={PROMPT_PLACEHOLDER} rows={3} disabled={running} />
        <button className="primary" onClick={run} disabled={running || !input.trim()}>
          {running ? "Racing…" : "Race all models"}
        </button>
      </div>

      <div className="bakeoff-grid" ref={listRef}
           style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}>
        {columns.map(c => (
          <ColumnCard key={c.model.deployment} col={c}
                      isFastest={c.model.deployment === fastest}
                      isCheapest={c.model.deployment === cheapest} />
        ))}
      </div>
    </div>
  );
}

function ColumnCard({ col, isFastest, isCheapest }:
                    { col: Column; isFastest: boolean; isCheapest: boolean }) {
  const price = `$${col.model.price_in_per_mtok.toFixed(2)}/M in · $${col.model.price_out_per_mtok.toFixed(2)}/M out`;
  return (
    <div className={`bakeoff-col status-${col.status}`}>
      <div className="col-header">
        <div className="col-title">
          <span className="deployment">{col.model.deployment}</span>
          {isFastest && <span className="pip fast">⚡</span>}
          {isCheapest && <span className="pip cheap">💰</span>}
        </div>
        <div className="col-pricing">{price}</div>
      </div>
      <div className="col-body">
        {col.status === "error"
          ? <div className="error">✗ {col.error}</div>
          : <pre className="stream">{col.text || (col.status === "streaming" ? "…" : "")}</pre>}
      </div>
      <div className="col-footer">
        {col.status === "done" && (
          <>
            <span>{col.latency_ms}ms</span>
            <span>{col.prompt_tokens ?? 0}→{col.completion_tokens ?? 0} tok</span>
            <span>${(col.cost_usd ?? 0).toFixed(5)}</span>
          </>
        )}
        {col.status === "streaming" && <span className="streaming-tag">streaming…</span>}
        {col.status === "idle" && <span className="idle-tag">idle</span>}
      </div>
    </div>
  );
}

function applyEvent(cs: Column[], ev: BakeoffEvent): Column[] {
  if (!ev.model) return cs;
  return cs.map(c => {
    if (c.model.deployment !== ev.model) return c;
    if (ev.kind === "delta") return { ...c, text: c.text + (ev.text ?? "") };
    if (ev.kind === "model_done") return {
      ...c, status: "done",
      latency_ms: ev.latency_ms, prompt_tokens: ev.prompt_tokens,
      completion_tokens: ev.completion_tokens, cost_usd: ev.cost_usd,
    };
    if (ev.kind === "model_error") return {
      ...c, status: "error", error: ev.message || "unknown error",
    };
    return c;
  });
}
