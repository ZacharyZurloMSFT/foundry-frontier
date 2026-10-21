import { useEffect } from "react";
import { LegacyWorkspace } from "../App";
import { setApiTokenProvider } from "../api";

/** Employee AI workspace: claims copilot, document intelligence, approvals and platform tools. */
export default function StaffWorkspace({ token }: { token: () => Promise<string> }) {
  setApiTokenProvider(token);
  useEffect(() => () => setApiTokenProvider(null), []);
  return <div className="business-staff-workspace"><LegacyWorkspace /></div>;
}
