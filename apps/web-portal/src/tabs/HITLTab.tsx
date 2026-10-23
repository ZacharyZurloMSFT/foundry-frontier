import { useEffect, useState } from "react";
import { decideHitl, getHitl } from "../api";
import type { DraftRecord } from "../insuranceApi";
import { emitDraftReviewed } from "../notifications";
import { renderField } from "../format";

/**
 * Approvals tab — pending claim-action drafts awaiting human review.
 *
 * Drafts from the claim-action-draft agent or prepared in Claims / Policies
 * are written to the `hitl_queue` Cosmos container. This tab polls the /hitl
 * endpoint and records a human approve / reject decision without insurance
 * adjudication.
 */

function formatAmount(v: unknown): string {
  if (typeof v === "number") return `$${v.toFixed(2)}`;
  if (typeof v === "string" && v.trim() !== "" && !isNaN(Number(v))) {
    return `$${Number(v).toFixed(2)}`;
  }
  return "—";
}

export default function HITLTab() {
  const [items, setItems] = useState<DraftRecord[]>([]);
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [deciding, setDeciding] = useState<string | null>(null);

  async function refresh() {
    try {
      const data = await getHitl();
      setItems(data.items ?? []);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setLoading(false); }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  async function decide(item: DraftRecord, decision: "approve" | "reject") {
    if (deciding) return;
    setDeciding(item.id);
    try {
    const result = await decideHitl(item.id, item.workflowId, decision);
    emitDraftReviewed({
      hitlId: item.id,
      workflowId: item.workflowId,
      sessionId: result?.session_id ?? null,
      decision,
      chatMessage: result?.chat_message ?? null,
      payload: item.payload,
      resolvedAt: result?.resolvedAt ?? new Date().toISOString(),
    });
    const action = renderField(item.payload?.action_type, "draft");
    const claim = renderField(item.payload?.claim_id, "the record");
    setToast(
      decision === "approve"
        ? `Draft reviewed — ${action} for ${claim}. No insurance claim approval, payment or external action was executed.`
        : `Draft rejected — ${action} for ${claim}. No external action was executed.`
    );
    await refresh();
    } catch (error: unknown) {
      setToast(error instanceof Error ? error.message : "Approval failed. Refresh before retrying.");
    } finally { setDeciding(null); }
  }

  return (
    <div className="chat-pane">
      <div className="chat-header">
        <strong>Pending human approvals</strong>
        <span className="chat-header-sub">
          — review internal action drafts, not insurance coverage. Approval never approves
          a claim, denies a claim, sends a policyholder message or executes a payment.
        </span>
      </div>
      <div className="chat-log">
        {loading && <div className="empty-hint" role="status">Loading approval queue…</div>}
        {error && <div className="claims-error" role="alert">{error}</div>}
        {!loading && !error && items.length === 0 && (
          <div className="empty-hint">No pending approvals. 🎉</div>
        )}
        {items.map((item) => (
          <div key={item.id} className="record-card">
            <div className="record-header">
              <div>
                <div className="record-title">
                  <strong>{renderField(item.payload?.action_type)}</strong>{" "}
                  <span className="record-ref">for {renderField(item.payload?.claim_id)}</span>
                </div>
                <div className="record-sub">
                  {renderField(item.payload?.summary)} · {formatAmount(item.payload?.amount_usd)} (proposed only)
                </div>
              </div>
              <div className="record-meta">
                <span className="record-status pending">PENDING</span>
              </div>
            </div>
            <div className="record-body">
              <div className="record-row">
                <span className="record-label">Policyholder</span>
                <span>{renderField(item.payload?.policyholder_id)}</span>
              </div>
              <div className="record-row">
                <span className="record-label">Policy ref</span>
                <span>{renderField(item.payload?.policy_refs)}</span>
              </div>
              <div className="record-actions">
                <button className="btn approve" disabled={!!deciding || !!error} onClick={() => decide(item, "approve")}>Approve draft</button>
                <button className="btn reject" disabled={!!deciding || !!error} onClick={() => decide(item, "reject")}>Reject draft</button>
              </div>
            </div>
          </div>
        ))}
      </div>
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
