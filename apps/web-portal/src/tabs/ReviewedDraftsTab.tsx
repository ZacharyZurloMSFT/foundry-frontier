import { useEffect, useState } from "react";
import { deleteReviewedDraft, getReviewedDrafts } from "../api";
import type { DraftRecord } from "../insuranceApi";
import { onDraftReviewed } from "../notifications";
import { renderField } from "../format";

/**
 * Reviewed claim-action drafts: the human approve / reject decisions recorded
 * in Approvals. Backed by `GET /hitl/resolved` and `DELETE /hitl/{hitl_id}`.
 */

function formatAmount(v: unknown): string {
  if (typeof v === "number") return `$${v.toFixed(2)}`;
  if (typeof v === "string" && v.trim() !== "" && !isNaN(Number(v))) {
    return `$${Number(v).toFixed(2)}`;
  }
  return "—";
}

export default function ReviewedDraftsTab() {
  const [items, setItems] = useState<DraftRecord[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [deleting, setDeleting] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const data = await getReviewedDrafts();
      setItems(data.items ?? []);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoaded(true);
    }
  }

  async function handleDelete(w: DraftRecord) {
    const label = String(w.payload?.action_type || "this draft");
    if (!window.confirm(`Delete the "${label}" reviewed draft? This cannot be undone.`)) return;

    setDeleting((prev) => {
      const next = new Set(prev);
      next.add(w.id);
      return next;
    });
    const snapshot = items;
    setItems((cur) => cur.filter((x) => x.id !== w.id));
    try {
      await deleteReviewedDraft(w.id, w.workflowId);
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
    const off = onDraftReviewed(() => refresh());
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
          records.
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
            <div key={w.id} className={`record-card ${isDeleting ? "record-deleting" : ""}`}>
              <div className="record-header">
                <div>
                  <div className="record-title">
                    <strong>{renderField(w.payload?.action_type, "Draft")}</strong>{" "}
                    <span className="record-ref">for {renderField(w.payload?.claim_id, "(unknown record)")}</span>
                  </div>
                  <div className="record-sub">{renderField(w.payload?.summary)}</div>
                </div>
                <div className="record-meta">
                  <span className={`record-status ${w.status}`}>{w.status.toUpperCase()}</span>
                  <button
                    className="record-delete"
                    onClick={() => handleDelete(w)}
                    disabled={isDeleting}
                    title="Delete this reviewed draft"
                    aria-label="Delete reviewed draft"
                  >
                    {isDeleting ? "…" : "🗑"}
                  </button>
                </div>
              </div>
              <div className="record-body">
                <div className="record-row">
                  <span className="record-label">Policyholder</span>
                  <span>{renderField(w.payload?.policyholder_id)}</span>
                </div>
                <div className="record-row">
                  <span className="record-label">Proposed amount</span>
                  <span>{formatAmount(w.payload?.amount_usd)}</span>
                </div>
                <div className="record-row">
                  <span className="record-label">Policy ref</span>
                  <span>{renderField(w.payload?.policy_refs)}</span>
                </div>
                <div className="record-row">
                  <span className="record-label">Resolved</span>
                  <span>{w.resolvedAt ? new Date(w.resolvedAt).toLocaleString() : "—"}</span>
                </div>
                <div className="record-row">
                  <span className="record-label">Workflow</span>
                  <span className="record-mono">{w.workflowId}</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
