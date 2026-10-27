import { useAssistantAccess } from "./assistants/AssistantContext";
import AssistantPanel from "./assistants/AssistantPanel";

export const HELP_SURFACE = "help_center";

/** True only when the server lists the Help Center for this signed-in account. */
export function useHelpCenter(): boolean { return Boolean(useAssistantAccess().surfaces[HELP_SURFACE]); }

export function HelpTabButton({ active, onOpen }: { active: boolean; onOpen: () => void }) {
  return useHelpCenter() ? <button aria-current={active ? "page" : undefined} onClick={onOpen}>Help center</button> : null;
}

export default function HelpCenter() {
  const access = useAssistantAccess();
  // `api` is only set once the server has answered, so this never shows while the status is still loading.
  const withdrawn = access.api !== null && !access.surfaces[HELP_SURFACE];
  return <section className="business-stack" aria-labelledby="help-center-title">
    <div>
      <p className="business-eyebrow">General questions</p><h2 id="help-center-title">Help center</h2>
      <p>Plain-English answers about reporting a loss, evidence, deductibles, rentals, billing and privacy, quoted from published help articles. The Help Center cannot see your policy or claims.</p>
    </div>
    <AssistantPanel surface={HELP_SURFACE} />
    {withdrawn && <p role="status" className="business-callout">The Help Center is not available for this account right now. Your policies and claims are unaffected.</p>}
    {access.surfaces.concierge && <p className="business-callout">Have a question about your own policy or claim? Open it from Policies or My claims and ask the Claims Concierge there.</p>}
  </section>;
}
