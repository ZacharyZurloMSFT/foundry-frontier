import { lazy, Suspense, useEffect, useState } from "react";
import { CompanyProvider, useCompany, Department } from "./CompanyContext";
import ChatTab from "./tabs/ChatTab";
import HITLTab from "./tabs/HITLTab";
import ReviewedDraftsTab from "./tabs/ReviewedDraftsTab";
import AdjusterSupportTab from "./tabs/AdjusterSupportTab";
import ClaimsInboxTab from "./tabs/ClaimsInboxTab";
import ContactCenterTab from "./tabs/ContactCenterTab";
import LandingPage from "./components/LandingPage";
import { CostHUD } from "./components/TracePanel";
import InsuranceRecordsTab from "./tabs/InsuranceRecordsTab";
import { InsuranceProvider } from "./InsuranceContext";

const BusinessPortal = lazy(() => import("./business/BusinessPortal"));
export const insuranceBusinessEnabled = (value: unknown) => value !== "false" && value !== false;

/**
 * Every tab id maps to an enabled_tabs entry in company.yaml so the sidebar
 * only renders what the active department opts into.
 */
type TabId =
  | "insurance_records" | "chat" | "reviewed_drafts" | "hitl"
  | "adjuster_support"                                                 // Adjuster Support
  | "claims_inbox"                                                     // Claims Document Intelligence
  | "voice_console";                                                   // Contact Center

/** Claims Concierge (flagship) navigation. */
const CLAIMS_TABS: { id: TabId; label: string; divide?: boolean }[] = [
  { id: "insurance_records", label: "Claims / Policies" },
  { id: "chat", label: "Claims Concierge chat" },
  { id: "reviewed_drafts", label: "Reviewed drafts", divide: true },
  { id: "hitl", label: "Approvals" },
];

/**
 * Map an `enabled_tabs` slug to its default id + label. `hr_chat` is the
 * deprecated manifest slug for the adjuster support tab; older `/company`
 * responses still carry it.
 */
const DEPT_TABS: Record<string, { id: TabId; label: string }> = {
  insurance_records: { id: "insurance_records", label: "Claims / Policies" },
  agent_chat: { id: "chat", label: "Claims Concierge chat" },
  reviewed_drafts: { id: "reviewed_drafts", label: "Reviewed drafts" },
  hitl: { id: "hitl", label: "Approvals" },
  adjuster_support: { id: "adjuster_support", label: "Adjuster support" },
  hr_chat: { id: "adjuster_support", label: "Adjuster support" },
  claims_inbox: { id: "claims_inbox", label: "Document Intelligence" },
  voice_console: { id: "voice_console", label: "Policyholder Contact Center" },
};

interface ShellProps {
  department: Department; onBack: () => void;
  tabRequest: { id: TabId; version: number } | null;
}

function DepartmentShell({ department, onBack, tabRequest }: ShellProps) {
  // Compute the sidebar tab list for this department. Claims Concierge
  // renders the flagship CLAIMS_TABS. For anything else we translate its
  // enabled_tabs list through DEPT_TABS.
  const isClaimsConcierge = department.slug === "claims-concierge";
  const tabs = isClaimsConcierge
    ? CLAIMS_TABS
    : department.enabled_tabs
        .map((t) => DEPT_TABS[t])
        .filter(Boolean) as { id: TabId; label: string; divide?: boolean }[];

  const [tab, setTab] = useState<TabId>(tabs[0]?.id ?? "chat");
  useEffect(() => { if (tabRequest && isClaimsConcierge) setTab(tabRequest.id); }, [tabRequest, isClaimsConcierge]);

  return (
    <div className="app">
      <div className="sidebar">
        <div className="dept-header" style={{ borderLeftColor: department.brand_color }}>
          <button className="back-button" onClick={onBack} title="Back to Northwind Insurance landing">
            ← Departments
          </button>
          <div className="dept-icon" style={{ color: department.brand_color }}>{department.icon}</div>
          <div className="dept-name">{department.name}</div>
          <div className="dept-project">
            <code>{department.foundry_project}</code>
          </div>
        </div>

        <CostHUD />

        {tabs.map((t) => (
          <div key={t.id}>
            {t.divide && <div className="sidebar-divider" />}
            <button
              className={`tab ${tab === t.id ? "active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          </div>
        ))}
      </div>

      <div className="content">
        {/* All Claims Concierge tabs stay mounted (CSS display toggle) so their
            in-memory chat history survives sidebar navigation. New-department
            tabs are mounted conditionally so they don't run background fetches
            on the flagship. */}
        {isClaimsConcierge && (
          <>
            <div style={{ display: tab === "chat" ? "block" : "none", height: "100%" }}><ChatTab /></div>
            <div style={{ display: tab === "insurance_records" ? "block" : "none", height: "100%" }}><InsuranceRecordsTab onApprovals={() => setTab("hitl")} /></div>
            <div style={{ display: tab === "reviewed_drafts" ? "block" : "none", height: "100%" }}><ReviewedDraftsTab /></div>
            <div style={{ display: tab === "hitl" ? "block" : "none", height: "100%" }}><HITLTab /></div>
          </>
        )}
        {department.slug === "adjuster-support" && tab === "adjuster_support" && <AdjusterSupportTab />}
        {department.slug === "claims-documents" && tab === "claims_inbox" && <ClaimsInboxTab />}
        {department.slug === "contact-center" && tab === "voice_console" && <ContactCenterTab />}
      </div>
    </div>
  );
}

function AppShell() {
  const { department, setDepartment, loading } = useCompany();
  const [tabRequest, setTabRequest] = useState<{ id: TabId; version: number } | null>(null);

  if (loading) {
    return <div className="landing-empty">Loading Northwind Insurance…</div>;
  }

  return department
    ? <DepartmentShell key={department.slug} department={department} tabRequest={tabRequest}
        onBack={() => { setDepartment(null); setTabRequest(null); }} />
    : <LandingPage onPick={(d) => { setDepartment(d.slug); setTabRequest(null); }} />;
}

/** Staff AI workspace: the department shell (Claims Concierge, Adjuster Support, Document Intelligence, Contact Center). */
export function AiWorkspace() {
  return (
    <CompanyProvider>
      <InsuranceProvider><AppShell /></InsuranceProvider>
    </CompanyProvider>
  );
}

export default function App() {
  if (insuranceBusinessEnabled(import.meta.env.VITE_INSURANCE_BUSINESS_ENABLED)) {
    return <Suspense fallback={<div role="status">Loading Northwind Insurance…</div>}><BusinessPortal /></Suspense>;
  }
  return <AiWorkspace />;
}
