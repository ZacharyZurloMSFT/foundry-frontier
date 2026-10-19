import { ReactNode, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { BusinessApi, BusinessError } from "./api";
import { getInsuranceAuth, InsuranceAuth } from "./auth";
import { BusinessConfig, businessTheme, readBusinessConfig } from "./config";
import { ClaimRecord, Identity, PolicySnapshot } from "./models";
import Policies from "./Policies";
import Claims from "./Claims";
import ReportLoss from "./ReportLoss";
import { ErrorNotice, Loading } from "./shared";
import "./business.css";

type AccountTab = "policies" | "claims" | "report";
interface Intent { tab: AccountTab; revision: number }
function useBusinessTheme() {
  useEffect(() => {
    const previous = document.documentElement.getAttribute("data-theme");
    const previousTitle = document.title;
    document.title = "Northwind Insurance | Your policy and claims";
    document.documentElement.setAttribute("data-insurance-business", "true");
    (() => {
      const param = new URLSearchParams(window.location.search).get("scoutTheme");
      const theme = businessTheme(param, window.matchMedia("(prefers-color-scheme: dark)").matches);
      document.documentElement.setAttribute("data-theme", theme);
    })();
    return () => {
      document.documentElement.removeAttribute("data-insurance-business");
      document.title = previousTitle;
      if (previous === null) document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", previous);
    };
  }, []);
}
export function PublicHome({ onAccount, onCoverage, onHelp }: {
  onAccount: (tab: AccountTab) => void; onCoverage: () => void; onHelp: () => void;
}) {
  return <>
    <section className="business-hero" aria-labelledby="business-home-title">
      <div><p className="business-eyebrow">Northwind Insurance · Auto protection</p><h1 id="business-home-title">Here for the<br />road ahead.</h1>
        <p className="business-lead">Understand your policy. Report a loss. Know what happens next. A straightforward place to manage your insurance, with people in control.</p>
        <div className="business-actions"><button className="business-primary" onClick={() => onAccount("report")}>Report a loss <span aria-hidden="true">↗</span></button><button onClick={() => onAccount("policies")}>View my policy</button></div>
        <p className="business-muted">Secure Microsoft sign-in is required for account records.</p>
      </div>
      <aside className="business-roadmap" aria-label="The claims journey"><span className="business-mark" aria-hidden="true">N</span><p className="business-eyebrow">Clarity at every step</p><h2>A report is the beginning.<br />Not a decision.</h2>
        <ol><li><strong>Tell us what happened</strong><span>Report a synthetic auto loss using your policy.</span></li><li><strong>Follow the next steps</strong><span>See requested evidence and case updates.</span></li><li><strong>Human-led review</strong><span>An adjuster manages the case, with a recorded timeline.</span></li></ol>
      </aside>
    </section>
    <section className="business-home-services" aria-labelledby="services-title"><p className="business-eyebrow">Make yourself at home</p><h2 id="services-title">What can we help you with?</h2>
      <div className="business-grid business-three">
        <article className="business-panel"><span className="business-service-number">01 / COVERAGE</span><h3>Know your protection</h3><p>See what an auto policy schedule and its wording can tell you — and why the details matter.</p><button onClick={onCoverage}>Explore coverage information →</button></article>
        <article className="business-panel"><span className="business-service-number">02 / CLAIMS</span><h3>A clear next step</h3><p>Understand the manual reporting process, evidence requests, and your case timeline.</p><button onClick={onHelp}>Read claims help →</button></article>
        <article className="business-panel"><span className="business-service-number">03 / MY ACCOUNT</span><h3>Your records, together</h3><p>Sign in to see policies, follow a claim, or access an employee case queue when authorized.</p><button onClick={() => onAccount("claims")}>Open my account →</button></article>
      </div>
    </section>
  </>;
}
function CoverageInfo({ onAccount }: { onAccount: () => void }) {
  return <section className="business-copy"><p className="business-eyebrow">Understand before you need it</p><h1>Coverage information</h1><p className="business-lead">Your policy schedule, wording, and endorsements belong together.</p>
    <div className="business-grid"><article className="business-panel"><h2>Collision</h2><p>Collision wording describes protection related to damage from a collision. Your schedule lists the applicable limit and deductible; exclusions and duties still matter.</p></article>
      <article className="business-panel"><h2>Comprehensive</h2><p>Comprehensive wording may address events other than a collision, such as hail. Applicability depends on the policy, dates, jurisdiction, facts, and endorsements.</p></article></div>
    <div className="business-panel"><h2>Read your actual policy</h2><p>Sign in to read the connected schedule, versioned policy wording, endorsements, and citations for your account. This general information is not a coverage determination or a promise to pay.</p><button className="business-primary" onClick={onAccount}>See my policies</button></div>
    <p className="business-muted">Quotes and policy purchases are not offered in this demonstration.</p>
  </section>;
}
function ClaimsHelp({ onAccount }: { onAccount: () => void }) {
  return <section className="business-copy"><p className="business-eyebrow">When something happens</p><h1>Claims help</h1><p className="business-lead">A conventional, human-led process. No automated coverage or payment decisions.</p>
    <div className="business-panel"><h2>Before reporting a loss</h2><p>In a real emergency, move to safety and contact local emergency services. This synthetic portal is not an emergency service.</p>
      <ol className="business-help-steps"><li>Sign in using the Microsoft account linked to your policyholder record.</li><li>Select the insured vehicle and policy, then enter the loss date, state, incident type, and synthetic facts.</li><li>Submit once. Keep the returned claim number. If the connection fails, check your claims before explicitly retrying the exact report.</li><li>Read your case for evidence requests, human-reviewed updates, and its timeline.</li></ol><button className="business-primary" onClick={onAccount}>Report a synthetic loss</button></div>
    <aside className="business-callout"><h2>Available here: manual reporting and case tracking</h2><p>Evidence storage and asynchronous processing are available only when explicitly enabled. An assistant and voice are not integrated with this journey. This portal does not send external communications, approve coverage, settle claims, or make payments. A prepared customer update is only an internal workflow stage.</p></aside>
  </section>;
}
function PortalLayout({ account, authLabel }: { account: (intent: Intent) => ReactNode; authLabel: string }) {
  const [view, setView] = useState<"home" | "coverage" | "help" | "account">("home");
  const [intent, setIntent] = useState<Intent>({ tab: "policies", revision: 0 });
  const main = useRef<HTMLElement>(null);
  useEffect(() => { main.current?.focus(); }, [view]);
  function openAccount(tab: AccountTab) { setIntent(v => ({ tab, revision: v.revision + 1 })); setView("account"); }
  return <div className="insurance-business">
    <a className="business-skip" href="#business-main">Skip to main content</a>
    <div className="business-disclosure"><strong>Synthetic demonstration</strong><span>No real insurance transactions. Use synthetic information only.</span></div>
    <header className="business-header"><button className="business-brand" onClick={() => setView("home")} aria-label="Northwind Insurance home"><span className="business-logo" aria-hidden="true">N</span><span>Northwind<small>INSURANCE</small></span></button>
      <nav aria-label="Main navigation"><button aria-current={view === "home" ? "page" : undefined} onClick={() => setView("home")}>Home</button><button aria-current={view === "coverage" ? "page" : undefined} onClick={() => setView("coverage")}>Coverage information</button><button aria-current={view === "help" ? "page" : undefined} onClick={() => setView("help")}>Claims help</button><button className="business-primary" aria-current={view === "account" ? "page" : undefined} onClick={() => openAccount("policies")}>{authLabel}</button></nav>
    </header>
    <main className="business-main" id="business-main" tabIndex={-1} ref={main}>
      {view === "home" && <PublicHome onAccount={openAccount} onCoverage={() => setView("coverage")} onHelp={() => setView("help")} />}
      {view === "coverage" && <CoverageInfo onAccount={() => openAccount("policies")} />}
      {view === "help" && <ClaimsHelp onAccount={() => openAccount("report")} />}
      {view === "account" && account(intent)}
    </main>
    <footer className="business-footer"><div><strong>Northwind Insurance</strong><p>A connected, manual insurance demonstration.</p></div><p>All records in this journey are synthetic. Policy information is not a coverage decision. No quotes, purchases, external communications, or payments.</p></footer>
  </div>;
}
function AccountWorkspace({ api, auth, revision, intent }: { api: BusinessApi; auth: InsuranceAuth; revision: number; intent: Intent }) {
  const [identity, setIdentity] = useState<Identity | null>(null), [error, setError] = useState<unknown>(null), [loading, setLoading] = useState(false);
  const [reload, setReload] = useState(0), [tab, setTab] = useState<AccountTab>(intent.tab);
  const [policy, setPolicy] = useState<PolicySnapshot | null>(null), [selected, setSelected] = useState<ClaimRecord | string | null>(null);
  const [createdNotice, setCreatedNotice] = useState("");
  const onError = useCallback((e: unknown) => { if (e instanceof BusinessError && e.status === 401) auth.requireInteraction(); }, [auth]);
  useEffect(() => { setTab(intent.tab); setPolicy(null); }, [intent]);
  useEffect(() => {
    let current = true; setLoading(true); setError(null); setIdentity(null);
    api.me().then(me => { if (current) setIdentity(me); }).catch(e => { if (current) { setError(e); onError(e); } }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [api, revision, reload, onError]);
  if (loading) return <Loading>Verifying your account with the insurance service…</Loading>;
  if (!identity) return <><ErrorNotice error={error} /><button onClick={() => setReload(v => v + 1)}>Retry account verification</button><p>No records or roles are inferred from a demo persona.</p></>;
  const activeTab = tab === "report" && identity.role !== "policyholder" ? "claims" : tab;
  return <div className="business-stack">
    <div className="business-account-meta"><span>Server-authorized role: <strong>{identity.role}</strong></span><span>Run: <code>{identity.run_id}</code></span><span>Source: <code>{identity.source_version}</code></span></div>
    <nav aria-label="Account navigation" className="business-account-nav">
      <button aria-current={activeTab === "policies" ? "page" : undefined} onClick={() => setTab("policies")}>Policies</button>
      <button aria-current={activeTab === "claims" ? "page" : undefined} onClick={() => { setSelected(null); setCreatedNotice(""); setTab("claims"); }}>{identity.role === "policyholder" ? "My claims" : "Claim queue"}</button>
      {identity.role === "policyholder" && <button aria-current={activeTab === "report" ? "page" : undefined} onClick={() => { setPolicy(null); setTab("report"); }}>Report a loss</button>}
    </nav>
    {identity.role === "presenter" && <p className="business-callout">Presenter account · read-only access. Roles and assignments are provided by the server, not selected in this portal.</p>}
    {activeTab === "policies" && <Policies api={api} canReport={identity.role === "policyholder"} onReport={p => { setPolicy(p); setTab("report"); }} onError={onError} />}
    {activeTab === "claims" && <Claims api={api} identity={identity} selected={selected} onSelect={value => { setSelected(value); setCreatedNotice(""); }} createdNotice={createdNotice} onError={onError} />}
    {activeTab === "report" && identity.role === "policyholder" && <ReportLoss api={api} identity={identity} initialPolicy={policy} onClaims={() => { setSelected(null); setTab("claims"); }} onCreated={(claim, replayed, warning) => {
      setSelected(claim); setCreatedNotice(`${replayed ? "Original report recovered" : "Loss report received"} — claim ${claim.claim_id}. The connected case is being read for its current state.${warning ? ` ${warning}` : ""}`); setTab("claims");
    }} onError={onError} />}
  </div>;
}
function ConfiguredPortal({ config }: { config: BusinessConfig }) {
  const auth = useMemo(() => getInsuranceAuth(config), [config]);
  const state = useSyncExternalStore(auth.subscribe, auth.snapshot, auth.snapshot);
  const api = useMemo(() => new BusinessApi(config, auth.token), [config, auth]);
  useEffect(() => { void auth.initialize(); }, [auth]);
  return <PortalLayout authLabel="My account" account={intent => <section className="business-stack">
    <div className="business-heading"><div><p className="business-eyebrow">A secure place for your records</p><h1>My account</h1>{state.authenticated && <p>Signed in as {state.accountName}</p>}</div>
      {state.authenticated && <button disabled={state.busy} onClick={() => void auth.signOut()}>Sign out of this portal</button>}
    </div>
    {state.error && <ErrorNotice error={new Error(state.error)} />}
    {!state.ready && <Loading>Preparing secure Microsoft sign-in…</Loading>}
    {(!state.authenticated || state.interactionRequired) && <div className="business-panel"><h2>{state.interactionRequired ? "Your Microsoft session needs attention" : "Sign in to continue"}</h2>
      <p>Use your actual Microsoft account. Your linked policy records and access role are checked by the insurance service. Tokens stay in memory and are never stored in browser storage.</p>
      <button className="business-primary" disabled={!state.ready || state.busy} onClick={() => void auth.signIn()}>{state.busy ? "Waiting for Microsoft…" : state.interactionRequired ? "Continue with Microsoft" : "Sign in with Microsoft"}</button>
      {state.interactionRequired && <p>No action will be resubmitted after sign-in. Review the current record and explicitly choose what to do next.</p>}
    </div>}
    {state.authenticated && <AccountWorkspace api={api} auth={auth} revision={state.revision} intent={intent} />}
  </section>} />;
}
export default function BusinessPortal() {
  useBusinessTheme();
  const result = useMemo(() => readBusinessConfig(import.meta.env), []);
  return result.ok ? <ConfiguredPortal config={result.value} /> : <PortalLayout authLabel="My account" account={() => <section className="business-panel"><h1>My account is unavailable</h1><p role="status">{result.message}</p><p>The public coverage information and claims help are available. No sign-in is simulated, and no fixture account is loaded.</p></section>} />;
}
