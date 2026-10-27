import { ReactNode, createContext, useContext, useEffect, useMemo, useState } from "react";
import { BusinessApi } from "../api";
import { AssistantApi } from "./assistantApi";
import { AssistantStatus, SurfaceStatus } from "./assistantModels";

export interface AssistantAccess { api: AssistantApi | null; notice: string; surfaces: Record<string, SurfaceStatus> }
export const noAssistants: AssistantAccess = { api: null, notice: "", surfaces: {} };
export const AssistantContext = createContext<AssistantAccess>(noAssistants);
export const useAssistantAccess = () => useContext(AssistantContext);

/** Asks the server which assistants this signed-in role may use. Nothing renders unless the server says so. */
export function AssistantProvider({ api, onError, children }: { api: BusinessApi; onError: (error: unknown) => void; children: ReactNode }) {
  const assistants = useMemo(() => new AssistantApi(api), [api]);
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  useEffect(() => {
    let current = true; setStatus(null);
    assistants.status().then(value => { if (current) setStatus(value); }).catch(error => { if (current) onError(error); });
    return () => { current = false; };
  }, [assistants, onError]);
  const value = useMemo<AssistantAccess>(() => status?.enabled
    ? { api: assistants, notice: status.notice, surfaces: Object.fromEntries(status.surfaces.map(surface => [surface.surface, surface])) }
    : noAssistants, [assistants, status]);
  return <AssistantContext.Provider value={value}>{children}</AssistantContext.Provider>;
}
