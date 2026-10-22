import { useEffect, useMemo, useRef, useState } from "react";
import { getBuild, getTrace, TraceReport } from "../api";

declare global { interface Window { mermaid?: any } }
async function ensureMermaid(): Promise<any> {
  if (window.mermaid) return window.mermaid;
  await new Promise<void>((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js";
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("mermaid load failed"));
    document.head.appendChild(s);
  });
  window.mermaid.initialize({ startOnLoad: false, theme: "dark",
    sequence: { showSequenceNumbers: true, mirrorActors: false } });
  return window.mermaid;
}

const REPO_URL_ENV = (import.meta as any).env.VITE_REPO_URL || "";

export default function TracePanel({ sessionId }: { sessionId: string | null }) {
  const [report, setReport] = useState<TraceReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const mermaidRef = useRef<HTMLDivElement | null>(null);

  async function refresh() {
    if (!sessionId) return;
    setBusy(true); setError(null);
    try { setReport(await getTrace(sessionId)); }
    catch (e: any) { setError(String(e?.message || e)); }
    finally { setBusy(false); }
  }

  useEffect(() => { refresh(); /* eslint-disable-next-line */ }, [sessionId]);

  useEffect(() => {
    if (!expanded || !report?.handoff_mermaid || !mermaidRef.current) return;
    (async () => {
      try {
        const m = await ensureMermaid();
        const id = `mmd-${Date.now()}`;
        const { svg } = await m.render(id, report.handoff_mermaid);
        if (mermaidRef.current) mermaidRef.current.innerHTML = svg;
      } catch {
        if (mermaidRef.current)
          mermaidRef.current.innerHTML = `<pre>${report.handoff_mermaid}</pre>`;
      }
    })();
  }, [expanded, report?.handoff_mermaid]);

  const totals = report?.totals;

  return (
    <div className={`trace-panel ${expanded ? "expanded" : ""}`}>
      <div className="trace-toggle" onClick={() => setExpanded(v => !v)}
           title={expanded ? "Collapse trace panel" : "Show trace + cost + handoffs"}>
        <span>{expanded ? "▾" : "▸"} Trace</span>
        {totals && (
          <span className="trace-mini">
            {totals.agent_hops} agents · {totals.tool_calls} tools ·{" "}
            {totals.latency_ms}ms · ${totals.cost_usd.toFixed(5)}
          </span>
        )}
        <button className="trace-refresh" onClick={(e) => { e.stopPropagation(); refresh(); }}
                disabled={busy || !sessionId}>
          {busy ? "…" : "↻"}
        </button>
      </div>

      {expanded && (
        <div className="trace-body">
          {!sessionId && <div className="empty">Send a message first to generate a trace.</div>}
          {error && <div className="error">✗ {error}</div>}
          {report?.note && <div className="warn">{report.note}</div>}

          {report && (
            <>
              <BuildFooter build={report.build} />
              <div className="trace-mermaid" ref={mermaidRef} />
              <SpanTable spans={report.spans} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

function BuildFooter({ build }: { build: TraceReport["build"] }) {
  const sha = (build.git_sha || "local").slice(0, 8);
  const url = REPO_URL_ENV && build.git_sha !== "local"
    ? `${REPO_URL_ENV}/commit/${build.git_sha}` : "";
  const promptShas = Object.entries(build).filter(([k]) => k.startsWith("prompt_sha."));
  return (
    <div className="build-footer">
      <div>
        <span className="build-label">build</span>{" "}
        {url ? <a href={url} target="_blank" rel="noreferrer"><code>{sha}</code></a>
             : <code>{sha}</code>}
        {build.build_env && <span className="build-env">@ {build.build_env}</span>}
      </div>
      {promptShas.length > 0 && (
        <details>
          <summary>Prompt versions ({promptShas.length})</summary>
          <ul>
            {promptShas.map(([k, v]) => (
              <li key={k}><code>{k.split(".", 2)[1]}</code> · <code>{String(v).slice(0, 8)}</code></li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function SpanTable({ spans }: { spans: TraceReport["spans"] }) {
  const rows = useMemo(() => spans.filter(s => s.agent || s.tool || s.model), [spans]);
  if (!rows.length) return <div className="empty">No agent/tool/model spans in this window.</div>;
  return (
    <table className="spans">
      <thead>
        <tr>
          <th>#</th><th>name</th><th>agent</th><th>tool</th><th>model</th>
          <th className="num">ms</th><th className="num">tok in</th>
          <th className="num">tok out</th><th className="num">$</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((s, i) => (
          <tr key={i}>
            <td className="dim">{i + 1}</td>
            <td><code>{s.name}</code></td>
            <td>{s.agent || "—"}</td>
            <td>{s.tool || "—"}</td>
            <td>
              {s.model || "—"}
              {s.model && s.gateway_model && (
                <span className="route-badge" title={s.gateway_model}>AI Gateway</span>
              )}
            </td>
            <td className="num">{Math.round(s.duration_ms)}</td>
            <td className="num">{s.prompt_tokens || "—"}</td>
            <td className="num">{s.completion_tokens || "—"}</td>
            <td className="num">{s.cost_usd ? s.cost_usd.toFixed(5) : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function CostHUD() {
  const [build, setBuild] = useState<TraceReport["build"] | null>(null);
  useEffect(() => { getBuild().then(r => setBuild(r.build)).catch(() => {}); }, []);
  if (!build) return null;
  const sha = (build.git_sha || "local").slice(0, 8);
  const url = REPO_URL_ENV && build.git_sha !== "local"
    ? `${REPO_URL_ENV}/commit/${build.git_sha}` : "";
  return (
    <div className="cost-hud">
      <span className="hud-item">Foundry Frontier</span>
      <span className="hud-item">
        {url ? <a href={url} target="_blank" rel="noreferrer"><code>{sha}</code></a>
             : <code>{sha}</code>}
        {build.build_env && ` · ${build.build_env}`}
      </span>
    </div>
  );
}
