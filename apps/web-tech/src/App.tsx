import { lazy, Suspense, useEffect, useState } from "react";
import { CompanyProvider, useCompany, Department } from "./CompanyContext";
import ChatTab from "./tabs/ChatTab";
import HITLTab from "./tabs/HITLTab";
import WorkOrdersTab from "./tabs/WorkOrdersTab";
import HRChatTab from "./tabs/HRChatTab";
import ClaimsInboxTab from "./tabs/ClaimsInboxTab";
import ContactCenterTab from "./tabs/ContactCenterTab";
import LandingPage from "./components/LandingPage";
import { CostHUD } from "./components/TracePanel";
import AgentHarnessTab from "./tabs/AgentHarnessTab";
import InsuranceRecordsTab from "./tabs/InsuranceRecordsTab";
import BakeoffTab from "./tabs/BakeoffTab";
import SafetyTab from "./tabs/SafetyTab";
import { InsuranceProvider } from "./InsuranceContext";

const BusinessPortal = lazy(() => import("./business/BusinessPortal"));
export const insuranceBusinessEnabled = (value: unknown) => value === "true";

/**
 * Every tab id maps to an enabled_tabs entry in company.yaml so the sidebar
 * only renders what the active department opts into.
 */
type TabId =
  | "insurance_records" | "chat" | "orders" | "hitl" | "bakeoff" | "safety"
  | "hr_chat"                                                          // Employee Help Desk
  | "claims_inbox"                                                     // Claims & Warranty
  | "voice_console";                                                   // Contact Center

/** Stable department slug; insurance-facing navigation. */
const FS_TABS: { id: TabId; label: string; divide?: boolean }[] = [
  { id: "insurance_records", label: "Claims / Policies" },
  { id: "chat", label: "Claims Concierge chat" },
  { id: "orders", label: "Reviewed drafts", divide: true },
  { id: "hitl", label: "Approvals" },
];

/** Map an `enabled_tabs` slug to its default id + label. */
const DEPT_TABS: Record<string, { id: TabId; label: string }> = {
  insurance_records: { id: "insurance_records", label: "Claims / Policies" },
  agent_chat: { id: "chat", label: "Claims Concierge chat" },
  work_orders: { id: "orders", label: "Reviewed drafts" },
  hitl: { id: "hitl", label: "Approvals" },
  hr_chat: { id: "hr_chat", label: "Ask Northwind" },
  claims_inbox: { id: "claims_inbox", label: "Document Intelligence" },
  voice_console: { id: "voice_console", label: "Policyholder Contact Center" },
};

interface ShellProps {
  department: Department; onBack: () => void; onHarness: () => void;
  tabRequest: { id: TabId; version: number } | null;
}

function DepartmentShell({ department, onBack, onHarness, tabRequest }: ShellProps) {
  // Compute the sidebar tab list for this department. For Claims Concierge
  // we render the flagship FS_TABS. For anything else we translate its
  // enabled_tabs list through DEPT_TABS.
  const isFieldServices = department.slug === "order-concierge";
  const tabs = isFieldServices
    ? FS_TABS
    : department.enabled_tabs
        .map((t) => DEPT_TABS[t])
        .filter(Boolean) as { id: TabId; label: string; divide?: boolean }[];

  const [tab, setTab] = useState<TabId>(tabs[0]?.id ?? "chat");
  useEffect(() => { if (tabRequest && isFieldServices) setTab(tabRequest.id); }, [tabRequest, isFieldServices]);

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
        <div className="sidebar-divider" />
        <button className="tab" onClick={onHarness}>Agent Harness</button>
        <button className={`tab ${tab === "bakeoff" ? "active" : ""}`} onClick={() => setTab("bakeoff")}>Model bake-off</button>
        <button className={`tab ${tab === "safety" ? "active" : ""}`} onClick={() => setTab("safety")}>Safety</button>
      </div>

      <div className="content">
        {/* All Claims Concierge tabs stay mounted (CSS display toggle) so their
            in-memory chat history survives sidebar navigation. New-department
            tabs are mounted conditionally so they don't run background fetches
            on the flagship. */}
        {isFieldServices && (
          <>
            <div style={{ display: tab === "chat" ? "block" : "none", height: "100%" }}><ChatTab /></div>
            <div style={{ display: tab === "insurance_records" ? "block" : "none", height: "100%" }}><InsuranceRecordsTab onApprovals={() => setTab("hitl")} /></div>
            <div style={{ display: tab === "orders" ? "block" : "none", height: "100%" }}><WorkOrdersTab /></div>
            <div style={{ display: tab === "hitl" ? "block" : "none", height: "100%" }}><HITLTab /></div>
          </>
        )}
        {department.slug === "employee-help-desk" && tab === "hr_chat" && <HRChatTab />}
        {department.slug === "claims-warranty" && tab === "claims_inbox" && <ClaimsInboxTab />}
        {department.slug === "contact-center" && tab === "voice_console" && <ContactCenterTab />}
        {tab === "bakeoff" && <BakeoffTab />}
        {tab === "safety" && <SafetyTab />}
      </div>
    </div>
  );
}

function AppShell() {
  const { department, setDepartment, loading } = useCompany();
  const [harness, setHarness] = useState(() => new URL(window.location.href).searchParams.get("section") === "agent-harness");
  const [visitedHarness, setVisitedHarness] = useState(harness);
  const [tabRequest, setTabRequest] = useState<{ id: TabId; version: number } | null>(null);

  function showHarness(show: boolean) {
    setHarness(show);
    if (show) setVisitedHarness(true);
    const url = new URL(window.location.href);
    if (show) url.searchParams.set("section", "agent-harness");
    else url.searchParams.delete("section");
    window.history.replaceState({}, "", url);
  }

  if (loading) {
    return <div className="landing-empty">Loading Northwind Insurance…</div>;
  }

  return (
    <>
      <div style={{ display: harness ? "none" : "block" }}>
        {department
          ? <DepartmentShell key={department.slug} department={department} tabRequest={tabRequest}
              onBack={() => { setDepartment(null); setTabRequest(null); }}
              onHarness={() => showHarness(true)} />
          : <LandingPage onPick={(d) => { setDepartment(d.slug); setTabRequest(null); }} onHarness={() => showHarness(true)} />}
      </div>
      {visitedHarness && <div style={{ display: harness ? "block" : "none" }}>
        <AgentHarnessTab active={harness} onBack={() => showHarness(false)} onApprovals={() => {
          setDepartment("order-concierge");
          setTabRequest({ id: "hitl", version: Date.now() });
          showHarness(false);
        }} />
      </div>}
    </>
  );
}

export default function App() {
  if (insuranceBusinessEnabled(import.meta.env.VITE_INSURANCE_BUSINESS_ENABLED)) {
    return <Suspense fallback={<div role="status">Loading Northwind Insurance…</div>}><BusinessPortal /></Suspense>;
  }
  return (
    <CompanyProvider>
      <InsuranceProvider><AppShell /></InsuranceProvider>
    </CompanyProvider>
  );
}
