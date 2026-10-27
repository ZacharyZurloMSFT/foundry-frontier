import { useAssistantAccess } from "./AssistantContext";
import AssistantPanel from "./AssistantPanel";

/** A collapsed, optional assistant beside a form. It never sees the form and appears only when the server lists the surface. */
export default function AssistantDisclosure({ surface, summary, note }: { surface: string; summary: string; note: string }) {
  const access = useAssistantAccess();
  if (!access.api || !access.surfaces[surface]) return null;
  return <details className="business-assistant-disclosure">
    <summary>{summary}</summary>
    <p className="business-muted">{note}</p>
    <AssistantPanel surface={surface} />
  </details>;
}
