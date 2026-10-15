import { useEffect, useRef } from "react";
import { BusinessError, errorMessage } from "./api";

export function ErrorNotice({ error }: { error: unknown }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (error) ref.current?.focus(); }, [error]);
  if (!error) return null;
  return <div className="business-error" role="alert" tabIndex={-1} ref={ref}>
    <strong>{error instanceof BusinessError && error.status === 409 ? "Conflict — nothing was retried." : "Unable to complete this request."}</strong>
    <p>{errorMessage(error)}</p>
    {error instanceof BusinessError && <>
      <p>HTTP {error.status || "unavailable"} · Code: <code>{error.code}</code></p>
      {error.correlationId && <p>Support reference: <code>{error.correlationId}</code></p>}
      {error.status === 401 && <p>Your access was rejected. Continue with Microsoft before explicitly trying again.</p>}
      {error.status === 403 && <p>The server does not allow this account to perform this action. Your role cannot be changed here.</p>}
      {error.status === 409 && <p>Read the current case, review changes, then choose a new action using the displayed version.</p>}
      {error.status === 503 && <p>The connected service is unavailable. No fixture or offline data is substituted.</p>}
      {error.uncertain && <p><strong>The write may have been saved.</strong> Refresh the case or check your claims. No automatic retry will occur.</p>}
    </>}
  </div>;
}
export function Loading({ children = "Loading connected records…" }: { children?: string }) {
  return <p role="status" className="business-muted">{children}</p>;
}
export function DateText({ value }: { value: string }) {
  const date = new Date(value);
  return <time dateTime={value}>{Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, value.length <= 10
    ? { dateStyle: "medium", timeZone: "UTC" } : { dateStyle: "medium", timeStyle: "short" })}</time>;
}
export function Pager({ offset, next, history, busy, onPage }: {
  offset: number; next: number | null; history: number[]; busy: boolean; onPage: (offset: number, backwards: boolean) => void;
}) {
  const validNext = next !== null && next > offset && next <= 1_000_000;
  return <nav aria-label="Results pages" className="business-actions">
    <button type="button" disabled={busy || history.length === 0} onClick={() => onPage(history[history.length - 1], true)}>Previous page</button>
    <span>Showing page starting at record {offset + 1}</span>
    <button type="button" disabled={busy || !validNext} onClick={() => { if (validNext && next !== null) onPage(next, false); }}>Next page</button>
    {next !== null && !validNext && <span role="alert">The service returned an invalid next page. Contact support.</span>}
  </nav>;
}
