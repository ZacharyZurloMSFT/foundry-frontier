/**
 * Live handoff graph for Claims Concierge, keyed by the orchestrator's stable tool IDs.
 *
 * Renders a compact 5-node topology (manager at the center, four
 * specialist agents on the rim). When the `active` prop matches
 * one of the specialist tool names, the corresponding edge + node
 * highlight so the demo audience can see which specialist the manager
 * is currently talking to. Recent hand-offs render as dimmed accents so
 * the audience can trace the path of a turn at a glance.
 *
 * Deliberately hand-rolled SVG (no mermaid) — it's small, self-contained,
 * and re-paints instantly on each state change without a runtime library.
 */
import { useMemo } from "react";

export type HandoffAgent =
  | "triage"
  | "policy_claim_lookup"
  | "policy_lookup"
  | "claim_action_draft";

interface Props {
  /** Currently-running handoff. `null` while the manager is thinking. */
  active: HandoffAgent | null;
  /** Names of sub-agents visited earlier in the turn (for dimmed accent). */
  visited: HandoffAgent[];
}

interface NodeLayout {
  id: HandoffAgent;
  label: string;
  x: number;
  y: number;
  color: string;
}

const NODES: NodeLayout[] = [
  { id: "triage",              label: "triage",                 x: 210, y: 30,  color: "#4f8cff" },
  { id: "policy_claim_lookup", label: "claim / policy records", x: 340, y: 130, color: "#ffb84f" },
  { id: "policy_lookup",       label: "policy wording",         x: 210, y: 230, color: "#7bd88f" },
  { id: "claim_action_draft",  label: "claim action draft",     x:  80, y: 130, color: "#b48cff" },
];

const MANAGER = { x: 210, y: 130, r: 34 };

export default function HandoffGraph({ active, visited }: Props) {
  const visitedSet = useMemo(() => new Set(visited), [visited]);

  return (
    <svg
      className="handoff-graph"
      viewBox="0 0 420 260"
      preserveAspectRatio="xMidYMid meet"
      aria-label="Orchestrator handoff graph"
    >
      {/* Edges — drawn first so nodes sit on top. */}
      {NODES.map((n) => {
        const isActive = active === n.id;
        const isVisited = visitedSet.has(n.id);
        const cls = `handoff-edge ${isActive ? "active" : isVisited ? "visited" : ""}`;
        return (
          <line
            key={`e-${n.id}`}
            x1={MANAGER.x}
            y1={MANAGER.y}
            x2={n.x}
            y2={n.y}
            className={cls}
            stroke={isActive ? n.color : isVisited ? n.color : "#2a3040"}
            strokeWidth={isActive ? 3 : 1.5}
            strokeDasharray={isActive ? "0" : isVisited ? "0" : "4 4"}
            strokeOpacity={isActive ? 1 : isVisited ? 0.55 : 0.7}
          />
        );
      })}

      {/* Manager node — always highlighted, sits at the center. */}
      <g className="handoff-node manager">
        <circle cx={MANAGER.x} cy={MANAGER.y} r={MANAGER.r} />
        <text x={MANAGER.x} y={MANAGER.y + 4} textAnchor="middle">manager</text>
      </g>

      {/* Sub-agent nodes. */}
      {NODES.map((n) => {
        const isActive = active === n.id;
        const isVisited = visitedSet.has(n.id);
        return (
          <g
            key={`n-${n.id}`}
            className={`handoff-node ${isActive ? "active" : isVisited ? "visited" : "idle"}`}
            style={{ ["--node-color" as any]: n.color }}
          >
            <circle cx={n.x} cy={n.y} r={26} />
            <text x={n.x} y={n.y + 4} textAnchor="middle">{n.label}</text>
          </g>
        );
      })}
    </svg>
  );
}
