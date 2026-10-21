/**
 * Northwind Insurance landing page — "pick a department" tile view.
 *
 * The tech app opens here. Each tile is one department (which is one
 * Foundry pillar). Clicking a tile sets the active department in
 * CompanyContext and switches to that department's default tab.
 *
 * Platform tabs (Bake-off, Safety) live in the sidebar regardless of the
 * active department — they're cross-cutting Foundry proofs.
 */
import { useCompany, Department } from "../CompanyContext";

interface Props {
  onPick: (dept: Department) => void;
}

const PILLAR_LABEL: Record<string, string> = {
  multi_agent: "Multi-agent + HITL",
  rag: "Grounded retrieval (RAG)",
  content_understanding: "Content Understanding",
  voice_live: "Voice Live",
};

export default function LandingPage({ onPick }: Props) {
  const { company, loading, error } = useCompany();

  if (loading) return <div className="landing-empty">Loading Northwind Insurance…</div>;
  if (error) return <div className="landing-empty">Failed to load company manifest: {error}</div>;
  if (!company) return <div className="landing-empty">No company data.</div>;

  return (
    <div className="landing">
      <div className="landing-hero">
        <h1>{company.name}</h1>
        <p className="landing-tagline">{company.tagline}</p>
        <p className="landing-hero-body">{company.hero}</p>
      </div>

      <div className="landing-grid">
        {company.departments.map((d) => (
          <button
            key={d.slug}
            className="landing-tile"
            style={{ borderColor: d.brand_color }}
            onClick={() => onPick(d)}
          >
            <div className="tile-icon" style={{ color: d.brand_color }}>{d.icon}</div>
            <div className="tile-name">{d.name}</div>
            <div className="tile-tagline">{d.tagline}</div>
            <div className="tile-meta">
              <span className="tile-pillar" style={{ borderColor: d.brand_color, color: d.brand_color }}>
                {PILLAR_LABEL[d.pillar] ?? d.pillar}
              </span>
              <span className="tile-project"><code>{d.foundry_project}</code></span>
            </div>
            <div className="tile-archetype">Best for: {d.customer_archetype}</div>
          </button>
        ))}
      </div>

      <div className="landing-footer">
        <span>Fictional insurer and synthetic records. Human review is required; no claim adjudication or payments are executed.</span>
        <span>Every department is its own Foundry project inside a shared AI Services account.</span>
        <span>Model deployments, UAMI, VNet, and observability are shared.</span>
      </div>
    </div>
  );
}
