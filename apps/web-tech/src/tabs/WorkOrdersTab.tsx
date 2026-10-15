import { useEffect, useState } from "react";
import { deleteWorkOrder, getWorkOrders } from "../api";
import { onWorkOrderResolved } from "../notifications";
import { renderField } from "../format";

/**
 * Reviewed internal drafts and explicitly identified legacy history.
 * `/work-orders` remains stable for backwards compatibility.
 */
interface Resolution {
  id: string;
  workflowId: string;
  status: "approved" | "rejected";
  decision?: string;
  note?: string;
  resolvedAt?: string;
  createdAt?: string;
  payload?: {
    order_id?: unknown;
    customer_id?: unknown;
    resolution_type?: unknown;
    amount_usd?: unknown;
    reason?: unknown;
    policy_ref?: unknown;
    urgency?: unknown;
    claim_id?: unknown;
    policyholder_id?: unknown;
    action_type?: unknown;
    summary?: unknown;
    policy_refs?: unknown;
  };
}

function formatAmount(v: unknown): string {
  if (typeof v === "number") return `$${v.toFixed(2)}`;
  if (typeof v === "string" && v.trim() !== "" && !isNaN(Number(v))) {
    return `$${Number(v).toFixed(2)}`;
  }
  return "—";
}

export default function WorkOrdersTab() {
  const [items, setItems] = useState<Resolution[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [deleting, setDeleting] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const data = await getWorkOrders();
      setItems(data.items ?? []);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoaded(true);
    }
  }

  async function handleDelete(w: Resolution) {
    const label = String(
      w.payload?.action_type || w.payload?.resolution_type || "this draft"
    );
    if (!window.confirm(`Delete the "${label}" resolution? This cannot be undone.`)) return;

    setDeleting((prev) => {
      const next = new Set(prev);
      next.add(w.id);
      return next;
    });
    const snapshot = items;
    setItems((cur) => cur.filter((x) => x.id !== w.id));
    try {
      await deleteWorkOrder(w.id, w.workflowId);
    } catch (e: any) {
      setItems(snapshot);
      alert(`Delete failed: ${e?.message ?? "unknown error"}`);
    } finally {
      setDeleting((prev) => {
        const next = new Set(prev);
        next.delete(w.id);
        return next;
      });
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    const off = onWorkOrderResolved(() => refresh());
    return () => {
      clearInterval(t);
      off();
    };
  }, []);

  return (
    <div className="chat-pane">
      <div className="chat-header">
        <strong>Reviewed drafts</strong>
        <span className="chat-header-sub">
          — internal human-review decisions. These are not coverage decisions or payment
          records. Legacy retail history is identified separately for compatibility.
        </span>
      </div>
      <div className="chat-log">
        {error && <div className="claims-error" role="alert">{error}</div>}
        {!loaded && <div className="empty-hint" role="status">Loading reviewed drafts…</div>}
        {loaded && !error && items.length === 0 && (
          <div className="empty-hint">
            No reviewed drafts yet. Prepare one in Claims / Policies, then review it in Approvals.
          </div>
        )}
        {items.map((w) => {
          const isDeleting = deleting.has(w.id);
          return (
            <div key={w.id} className={`wo-card ${isDeleting ? "wo-deleting" : ""}`}>
              <div className="wo-header">
                <div>
                  <div className="wo-title">
                    <strong>{renderField(w.payload?.action_type ?? w.payload?.resolution_type, "Draft")}</strong>{" "}
                    <span className="wo-site">for {renderField(w.payload?.claim_id ?? w.payload?.order_id, "(unknown record)")}{!w.payload?.claim_id && " · legacy retail history"}</span>
                  </div>
                  <div className="wo-sub">{renderField(w.payload?.summary ?? w.payload?.reason)}</div>
                </div>
                <div className="wo-meta">
                  <span className={`wo-status ${w.status}`}>{w.status.toUpperCase()}</span>
                  {w.payload?.urgency != null && <span className="wo-priority">{renderField(w.payload.urgency)}</span>}
                  <button
                    className="wo-delete"
                    onClick={() => handleDelete(w)}
                    disabled={isDeleting}
                    title="Delete this resolution"
                    aria-label="Delete resolution"
                  >
                    {isDeleting ? "…" : "🗑"}
                  </button>
                </div>
              </div>
              <div className="wo-body">
                <div className="wo-row">
                  <span className="wo-label">{w.payload?.claim_id ? "Policyholder" : "Legacy customer"}</span>
                  <span>{renderField(w.payload?.policyholder_id ?? w.payload?.customer_id)}</span>
                </div>
                <div className="wo-row">
                  <span className="wo-label">Proposed amount</span>
                  <span>{formatAmount(w.payload?.amount_usd)}</span>
                </div>
                <div className="wo-row">
                  <span className="wo-label">Policy ref</span>
                  <span>{renderField(w.payload?.policy_refs ?? w.payload?.policy_ref)}</span>
                </div>
                <div className="wo-row">
                  <span className="wo-label">Resolved</span>
                  <span>{w.resolvedAt ? new Date(w.resolvedAt).toLocaleString() : "—"}</span>
                </div>
                <div className="wo-row">
                  <span className="wo-label">Workflow</span>
                  <span className="wo-mono">{w.workflowId}</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
