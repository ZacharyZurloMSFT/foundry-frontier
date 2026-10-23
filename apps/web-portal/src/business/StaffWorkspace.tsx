import { useEffect } from "react";
import { AiWorkspace } from "../App";
import { setApiTokenProvider } from "../api";

/** Staff AI workspace: claims copilot, document intelligence, approvals, reviewed drafts, adjuster support and contact center. */
export default function StaffWorkspace({ token }: { token: () => Promise<string> }) {
  setApiTokenProvider(token);
  useEffect(() => () => setApiTokenProvider(null), []);
  return <div className="business-staff-workspace"><AiWorkspace /></div>;
}
