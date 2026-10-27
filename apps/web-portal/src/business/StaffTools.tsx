import { lazy, ReactNode, Suspense, useEffect, useState } from "react";
import { setApiTokenProvider } from "../api";
import { InsuranceProvider } from "../InsuranceContext";
import { Identity } from "./models";
import { Loading } from "./shared";

const Chat = lazy(() => import("../tabs/ChatTab"));
const Guidance = lazy(() => import("../tabs/AdjusterSupportTab"));
const Documents = lazy(() => import("../tabs/ClaimsInboxTab"));
const Approvals = lazy(() => import("../tabs/HITLTab"));
const ReviewedDrafts = lazy(() => import("../tabs/ReviewedDraftsTab"));
const ContactCenter = lazy(() => import("../tabs/ContactCenterTab"));
const DraftPreparation = lazy(() => import("../tabs/InsuranceRecordsTab"));

export type StaffTask = "documents" | "reviews" | "contact";

export function StaffToolsProvider({ token, children }: { token: () => Promise<string>; children: ReactNode }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    setApiTokenProvider(token);
    setReady(true);
    return () => setApiTokenProvider(null);
  }, [token]);
  return ready ? <InsuranceProvider>{children}</InsuranceProvider> : <Loading>Preparing signed-in staff tools…</Loading>;
}

function ToolDisclosure({ title, children }: { title: string; children: ReactNode }) {
  const [opened, setOpened] = useState(false);
  return <details className="business-panel" onToggle={event => { if (event.currentTarget.open) setOpened(true); }}>
    <summary>{title}</summary>
    {opened && <div className="business-staff-tool"><Suspense fallback={<Loading>{`Opening ${title.toLowerCase()}…`}</Loading>}>{children}</Suspense></div>}
  </details>;
}

export function StaffClaimAssistance({ role }: { role: Identity["role"] }) {
  if (role === "policyholder") return null;
  return <section className="business-stack" aria-label="Claim assistance">
    {role === "adjuster" && <ToolDisclosure title="Claims Concierge — draft assistance">
      <p className="business-callout">Staff assistance uses the synthetic insurance tools. Identify the claim in your question; opening a case does not automatically send its contents. Agents prepare drafts only. Review them in Claim reviews.</p>
      <Chat />
    </ToolDisclosure>}
    <ToolDisclosure title="Adjuster guidance — procedures and review boundaries"><Guidance /></ToolDisclosure>
  </section>;
}

export function StaffTaskContent({ task, role }: { task: StaffTask; role: Identity["role"] }) {
  const [review, setReview] = useState<"prepare" | "pending" | "reviewed">("pending");
  if (role !== "adjuster") return null;
  return <section className="business-stack">
    <div><p className="business-eyebrow">Connected staff tasks</p><h2>{task === "documents" ? "Document review" : task === "reviews" ? "Claim reviews" : "Contact center"}</h2></div>
    {task === "documents" && <p>Extract and review synthetic source documents here. To attach evidence to a connected case, open that case in Claim queue and use Documents and evidence review.</p>}
    {task === "reviews" && <>
      <p>Prepare and review claim-action drafts. A human records each review; nothing approves coverage, pays a claim, or sends an external communication.</p>
      <nav className="business-account-nav" aria-label="Claim review stages">
        <button aria-current={review === "prepare" ? "page" : undefined} onClick={() => setReview("prepare")}>Prepare a draft</button>
        <button aria-current={review === "pending" ? "page" : undefined} onClick={() => setReview("pending")}>Pending review</button>
        <button aria-current={review === "reviewed" ? "page" : undefined} onClick={() => setReview("reviewed")}>Reviewed drafts</button>
      </nav>
    </>}
    <div className="business-staff-tool"><Suspense fallback={<Loading>Opening staff task…</Loading>}>
      {task === "documents" && <Documents />}
      {task === "contact" && <ContactCenter />}
      {task === "reviews" && review === "prepare" && <DraftPreparation onApprovals={() => setReview("pending")} />}
      {task === "reviews" && review === "pending" && <Approvals />}
      {task === "reviews" && review === "reviewed" && <ReviewedDrafts />}
    </Suspense></div>
  </section>;
}
