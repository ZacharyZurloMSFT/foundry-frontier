import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { getInsuranceFixtures, type InsuranceFixtures } from "./insuranceApi";

interface InsuranceContextValue {
  fixtures: InsuranceFixtures | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}
const InsuranceContext = createContext<InsuranceContextValue>({
  fixtures: null, loading: true, error: null, refresh: () => {},
});

export function InsuranceProvider({ children }: { children: ReactNode }) {
  const [fixtures, setFixtures] = useState<InsuranceFixtures | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const refresh = useCallback(() => setVersion((v) => v + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null); setFixtures(null);
    getInsuranceFixtures(controller.signal)
      .then((data) => { if (!controller.signal.aborted) setFixtures(data); })
      .catch((e: unknown) => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [version]);
  return <InsuranceContext.Provider value={{ fixtures, loading, error, refresh }}>{children}</InsuranceContext.Provider>;
}
export function useInsurance() { return useContext(InsuranceContext); }

export function InsuranceRuntimeNotice() {
  const { fixtures, loading, error, refresh } = useInsurance();
  if (fixtures?.agent_runtime_ready && !loading && !error) return null;
  return <div className="insurance-notice" role={error ? "alert" : "status"}>
    <strong>{loading ? "Checking insurance runtime…" : error ? "Insurance runtime cannot be verified." : "Insurance agents are not available yet."}</strong>
    <p>{error || "The converted Foundry agents require an authorised rollout. Insurance prompts are never sent to the legacy retail agents. Browse Claims / Policies and prepare a draft for human review instead."}</p>
    <button className="chip" disabled={loading} onClick={refresh}>Check again</button>
  </div>;
}
