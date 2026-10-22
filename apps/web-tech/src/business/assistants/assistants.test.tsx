import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BusinessApi, BusinessError } from "../api";
import { ClaimDetail } from "../Claims";
import HelpCenter, { HelpTabButton } from "../HelpCenter";
import Policies, { PolicyView } from "../Policies";
import ReportLoss from "../ReportLoss";
import { claimExample, configExample, identityExample, policyExample } from "../testData";
import { AssistantAccess, AssistantContext, noAssistants } from "./AssistantContext";
import AssistantDisclosure from "./AssistantDisclosure";
import AssistantPanel, { Turn, contextSuggestions, variantFor } from "./AssistantPanel";
import { AssistantApi, MAX_STREAM_BYTES, readEvents } from "./assistantApi";
import { AssistantEvent, SurfaceStatus, parseEvent, parseStatus } from "./assistantModels";
import { Conversation, Exchange, emptyConversation, reduce } from "./assistantState";
import { wordingAnchor } from "./wording";

const SESSION = "asst-concierge-123e4567-e89b-12d3-a456-426614174000";
const HELP_SESSION = "asst-help_center-123e4567-e89b-12d3-a456-426614174000";
const session = { kind: "session", session_id: SESSION, surface: "concierge", scope: { records: "own", claim_id: "CLM-2001", policy_id: "POL-1001" } };
const helpSession = { kind: "session", session_id: HELP_SESSION, surface: "help_center", scope: { records: "none", claim_id: null, policy_id: null } };
const citation = { policy_ref: "WORD-TEST#collision", title: "Test auto wording", version: "1", state: "WA", effective_from: "2026-01-01", effective_to: "2026-12-31", section_id: "collision", source_uri: "synthetic://WORD-TEST", text: "Synthetic wording for review." };
const helpSource = { id: "HELP-CLAIMS-001", title: "How to report a loss", version: "1", topic: "claims", effective_from: "2026-01-01", source_uri: "urn:northwind:synthetic:help-center:HELP-CLAIMS-001" };
const done = { kind: "done", notice: "Not a coverage decision.", provenance: { model: "gpt-test", prompt_sha: "abcdef123456", tool_calls: 1, human_review: false } };
const helpDone = { kind: "done", notice: "Not a coverage decision.", provenance: { model: "gpt-test", prompt_sha: "abcdef123456", tool_calls: 0, human_review: false } };
const surface: SurfaceStatus = { surface: "concierge", label: "Claims Concierge", available: true, reason: null, suggestions: ["Where do my claims stand?"], accepts: ["claim", "policy"] };
const helpSurface: SurfaceStatus = { surface: "help_center", label: "Help Center", available: true, reason: null, suggestions: ["How do I report a loss?"], accepts: [] };
const api = new BusinessApi(configExample, async () => "test-access-token");
const noop = () => {};
const encoder = new TextEncoder();
const ndjson = (...parts: string[]) => new ReadableStream<Uint8Array>({ start(controller) { for (const part of parts) controller.enqueue(encoder.encode(part)); controller.close(); } });
const lines = (...events: unknown[]) => events.map(event => JSON.stringify(event) + "\n");
async function collect(stream: ReadableStream<Uint8Array>) { const events: AssistantEvent[] = []; for await (const event of readEvents(stream)) events.push(event); return events; }
const NOTICE = "Assistants never decide coverage or claims and cannot change anything.";
const access = (...surfaces: SurfaceStatus[]): AssistantAccess => ({ api: new AssistantApi(api), notice: NOTICE, surfaces: Object.fromEntries(surfaces.map(s => [s.surface, s])) });
const withAssistants = (value: AssistantAccess, child: ReturnType<typeof createElement>) => renderToStaticMarkup(createElement(AssistantContext.Provider, { value }, child));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("assistant contracts", () => {
  it("parses server status and rejects malformed or oversized shapes", () => {
    const status = parseStatus({ enabled: true, notice: "n", surfaces: [{ ...surface }] });
    expect(status.surfaces[0]).toEqual(surface);
    expect(parseStatus({ enabled: false, notice: "off", surfaces: [] }).surfaces).toEqual([]);
    for (const bad of [null, { enabled: "yes", notice: "n", surfaces: [] }, { enabled: true, notice: "n", surfaces: [{ ...surface, accepts: ["admin"] }] },
      { enabled: true, notice: "n", surfaces: [{ ...surface, label: "x".repeat(300) }] }, { enabled: true, notice: "n", surfaces: Array(9).fill(surface) }]) {
      expect(() => parseStatus(bad)).toThrow(/Invalid/);
    }
  });
  it("parses every event kind strictly", () => {
    expect(parseEvent(session)).toEqual({ kind: "session", session_id: SESSION, surface: "concierge", scope: { records: "own", claim_id: "CLM-2001", policy_id: "POL-1001" } });
    expect(parseEvent({ kind: "tool", name: "get_claim", status: "ok", label: "Read claim CLM-2001" })).toMatchObject({ status: "ok" });
    expect(parseEvent({ kind: "assistant_delta", text: "hi", agent: "a" })).toMatchObject({ text: "hi" });
    expect(parseEvent({ kind: "citations", citations: [citation] })).toMatchObject({ citations: [citation] });
    expect(parseEvent({ kind: "error", code: "x", message: "m" })).toMatchObject({ code: "x" });
    expect(parseEvent(done)).toMatchObject({ provenance: { tool_calls: 1 } });
    expect(parseEvent({ ...done, provenance: null })).toMatchObject({ provenance: null });
    expect(parseEvent(helpSession)).toEqual({ kind: "session", session_id: HELP_SESSION, surface: "help_center", scope: { records: "none", claim_id: null, policy_id: null } });
    expect(parseEvent({ kind: "sources", sources: [helpSource] })).toEqual({ kind: "sources", sources: [helpSource] });
    expect(parseEvent({ kind: "sources", sources: [] })).toEqual({ kind: "sources", sources: [] });
    for (const bad of [{ kind: "mystery" }, { ...session, session_id: "other-session" }, { ...session, scope: { records: "all", claim_id: null, policy_id: null } },
      { ...helpSession, scope: { records: "none", claim_id: "CLM-2001", policy_id: null } }, { ...helpSession, scope: { records: "none", claim_id: null, policy_id: "POL-1001" } },
      { kind: "sources", sources: Array(9).fill(helpSource) }, { kind: "sources", sources: [{ ...helpSource, id: "bad id!" }] },
      { kind: "sources", sources: [{ ...helpSource, title: "x".repeat(121) }] }, { kind: "sources", sources: [{ ...helpSource, version: undefined }] },
      { kind: "sources", sources: "none" }, { kind: "sources" },
      { ...session, scope: { records: "own", claim_id: "<script>", policy_id: null } }, { kind: "tool", name: "t", status: "great", label: "l" },
      { kind: "assistant_delta", text: "x".repeat(7000), agent: "a" }, { kind: "citations", citations: Array(13).fill(citation) },
      { ...done, provenance: { ...done.provenance, tool_calls: -1 } }, { kind: "assistant_delta", agent: "a" }, "text", null]) {
      expect(() => parseEvent(bad)).toThrow(/Invalid/);
    }
  });
});

describe("assistant stream", () => {
  it("reads ordered events across arbitrary chunk boundaries", async () => {
    const text = lines(session, { kind: "tool", name: "get_claim", status: "ok", label: "Read claim" }, { kind: "assistant_delta", text: "Answer", agent: "a" }, { kind: "citations", citations: [citation] }, done).join("");
    const events = await collect(ndjson(text.slice(0, 40), text.slice(40, 41), text.slice(41, 200), text.slice(200)));
    expect(events.map(event => event.kind)).toEqual(["session", "tool", "assistant_delta", "citations", "done"]);
  });
  it("fails closed for disorder, truncation, invalid data, oversize and excess events", async () => {
    await expect(collect(ndjson(...lines({ kind: "assistant_delta", text: "x", agent: "a" }, done)))).rejects.toThrow(/start with a session/);
    await expect(collect(ndjson(...lines(session, session, done)))).rejects.toThrow(/second assistant session/);
    await expect(collect(ndjson(...lines(session, done, { kind: "assistant_delta", text: "late", agent: "a" })))).rejects.toThrow(/after completion/);
    await expect(collect(ndjson(...lines(session, { kind: "assistant_delta", text: "partial", agent: "a" })))).rejects.toThrow(/interrupted/);
    await expect(collect(ndjson(JSON.stringify(session) + "\n", "{not json}\n"))).rejects.toThrow(/Invalid assistant event/);
    await expect(collect(ndjson(...lines(session), "x".repeat(MAX_STREAM_BYTES + 1)))).rejects.toThrow(/size limit/);
    await expect(collect(ndjson(...lines(session, ...Array(70).fill({ kind: "tool", name: "t", status: "ok", label: "l" }), done)))).rejects.toThrow(/event limit/);
    await expect(collect(ndjson(JSON.stringify(session) + "\n" + JSON.stringify(done)))).resolves.toHaveLength(2);
  });
  it("reads a help-center turn that ends in a sources event", async () => {
    const text = lines(helpSession, { kind: "assistant_delta", text: "Here is what the Help Center says:", agent: "a" }, { kind: "sources", sources: [helpSource] }, helpDone).join("");
    const events = await collect(ndjson(text.slice(0, 60), text.slice(60)));
    expect(events.map(event => event.kind)).toEqual(["session", "assistant_delta", "sources", "done"]);
    await expect(collect(ndjson(...lines(helpSession, { kind: "sources", sources: Array(9).fill(helpSource) }, helpDone)))).rejects.toThrow(/Invalid/);
  });
  it("posts only the question, session and record hints with the portal's token and run", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(ndjson(...lines(session, done)), { headers: { "Content-Type": "application/x-ndjson" } }));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    const events = [];
    for await (const event of new AssistantApi(api).chat("concierge", { message: "Where is it?", session_id: SESSION, context: { claim_id: "CLM-2001" } }, controller.signal)) events.push(event);
    expect(events).toHaveLength(2);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("http://localhost:8000/api/v1/assistants/concierge/chat");
    expect(init.method).toBe("POST"); expect(init.signal).toBe(controller.signal); expect(init.credentials).toBe("omit");
    expect(JSON.parse(init.body)).toEqual({ message: "Where is it?", session_id: SESSION, context: { claim_id: "CLM-2001" } });
    const headers = new Headers(init.headers);
    expect(headers.get("X-Demo-Run")).toBe("test-run"); expect(headers.get("Accept")).toBe("application/x-ndjson");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("surfaces typed server errors, wrong formats and lost connections without retrying", async () => {
    const envelope = { error: { code: "assistant_forbidden", message: "Not available for your account.", correlation_id: "ref-1" } };
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(envelope), { status: 403 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockRejectedValueOnce(new TypeError("offline"));
    vi.stubGlobal("fetch", fetch);
    const open = () => new AssistantApi(api).chat("concierge", { message: "hi" }).next();
    await expect(open()).rejects.toMatchObject({ status: 403, code: "assistant_forbidden", uncertain: false });
    await expect(open()).rejects.toMatchObject({ code: "invalid_response" });
    const lost = await open().catch(error => error as BusinessError);
    expect(lost).toMatchObject({ code: "network_error", uncertain: false, message: "Unable to reach the insurance service." });
    expect(fetch).toHaveBeenCalledTimes(3);
  });
  it("reads assistant availability from the protected status route", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ enabled: true, notice: "n", surfaces: [surface] })));
    vi.stubGlobal("fetch", fetch);
    expect((await new AssistantApi(api).status()).surfaces[0].surface).toBe("concierge");
    expect(fetch.mock.calls[0][0]).toBe("http://localhost:8000/api/v1/assistants/status");
  });
});

describe("conversation state", () => {
  const run = (...actions: Parameters<typeof reduce>[1][]) => actions.reduce(reduce, emptyConversation);
  const event = (value: unknown) => ({ type: "event" as const, event: parseEvent(value) });
  const answered = (state: Conversation) => state.exchanges[state.exchanges.length - 1];
  it("shows an answer only after the server completes the turn", () => {
    const mid = run({ type: "ask", question: "Where is it?" }, event(session), event({ kind: "tool", name: "get_claim", status: "ok", label: "Read claim" }), event({ kind: "assistant_delta", text: "Awaiting evidence.", agent: "a" }));
    expect(answered(mid)).toMatchObject({ status: "pending", answer: "Awaiting evidence." }); expect(mid.busy).toBe(true);
    const end = reduce(reduce(mid, event({ kind: "citations", citations: [citation] })), event(done));
    expect(answered(end)).toMatchObject({ status: "answered", citations: [citation], provenance: { model: "gpt-test" } });
    expect(end.busy).toBe(false); expect(end.sessionId).toBe(SESSION);
  });
  it("never shows an answer from a failed turn and drops a lost session", () => {
    const failed = run({ type: "ask", question: "q" }, event(session), event({ kind: "assistant_delta", text: "partial", agent: "a" }),
      event({ kind: "error", code: "session_not_found", message: "Gone." }), event(done));
    expect(answered(failed)).toMatchObject({ status: "failed", answer: null, citations: [], error: { code: "session_not_found" } });
    expect(failed.busy).toBe(false); expect(failed.sessionId).toBeNull();
    const silent = run({ type: "ask", question: "q" }, event(session), event(done));
    expect(answered(silent)).toMatchObject({ status: "failed", error: { code: "no_answer" } });
    expect(run({ type: "ask", question: "q" }, { type: "ended" }).exchanges[0]).toMatchObject({ status: "failed", error: { code: "interrupted" } });
    const refused = run({ type: "ask", question: "q" }, event(session), { type: "failed", code: "assistant_busy", message: "Busy." });
    expect(refused.sessionId).toBe(SESSION); expect(answered(refused).error?.message).toBe("Busy.");
    expect(reduce(run({ type: "ask", question: "q" }), { type: "reset" })).toEqual(emptyConversation);
  });
  it("keeps earlier answers untouched while a later turn changes", () => {
    const first = run({ type: "ask", question: "one" }, event(session), event({ kind: "assistant_delta", text: "First", agent: "a" }), event(done));
    const second = reduce(first, { type: "ask", question: "two" });
    const after = reduce(second, event({ kind: "assistant_delta", text: "Second", agent: "a" }));
    expect(after.exchanges.map(e => [e.question, e.answer, e.status])).toEqual([["one", "First", "answered"], ["two", "Second", "pending"]]);
  });
  it("keeps help-article sources only on a turn the server completed", () => {
    const answeredTurn = run({ type: "ask", question: "How do I report a loss?" }, event(helpSession), event({ kind: "assistant_delta", text: "Here it is.", agent: "a" }),
      event({ kind: "sources", sources: [helpSource] }), event(helpDone));
    expect(answered(answeredTurn)).toMatchObject({ status: "answered", sources: [helpSource], citations: [] });
    expect(answeredTurn.sessionId).toBe(HELP_SESSION);
    const withdrawn = run({ type: "ask", question: "q" }, event(helpSession), event({ kind: "assistant_delta", text: "Partial", agent: "a" }),
      event({ kind: "sources", sources: [helpSource] }), event({ kind: "error", code: "ungrounded_answer", message: "Nothing was shown." }), event(helpDone));
    expect(answered(withdrawn)).toMatchObject({ status: "failed", answer: null, sources: [], error: { code: "ungrounded_answer" } });
    expect(answered(run({ type: "ask", question: "q" }, event(helpSession), event({ kind: "sources", sources: [helpSource] }), event(helpDone)))).toMatchObject({ status: "failed", sources: [], error: { code: "no_answer" } });
    expect(answered(run({ type: "ask", question: "q" }, event({ kind: "sources", sources: [helpSource] }), { type: "ended" }))).toMatchObject({ sources: [], error: { code: "interrupted" } });
    expect(answered(run({ type: "ask", question: "q" }, event({ kind: "sources", sources: [helpSource] }), { type: "failed", code: "assistant_busy", message: "Busy." }))).toMatchObject({ sources: [] });
    expect(run({ type: "ask", question: "q" }).exchanges[0].sources).toEqual([]);
  });
});

describe("embedded assistant panels", () => {
  const claimProps = { api, identity: identityExample, claimId: claimExample.claim_id, initialClaim: claimExample, onError: noop };
  it("render nothing unless the server lists the surface for this account", () => {
    expect(renderToStaticMarkup(createElement(AssistantPanel, { surface: "concierge" }))).toBe("");
    expect(withAssistants(noAssistants, createElement(AssistantPanel, { surface: "concierge" }))).toBe("");
    expect(withAssistants(access(), createElement(AssistantPanel, { surface: "concierge" }))).toBe("");
    expect(withAssistants(access(surface), createElement(AssistantPanel, { surface: "unknown" }))).toBe("");
    expect(withAssistants({ ...access(surface), api: null }, createElement(AssistantPanel, { surface: "concierge" }))).toBe("");
  });
  it("say honestly when an enabled assistant is unavailable, without exposing configuration", () => {
    const html = withAssistants(access({ ...surface, available: false, reason: "A Foundry project endpoint is required." }), createElement(AssistantPanel, { surface: "concierge" }));
    expect(html).toContain("not available right now"); expect(html).not.toContain("Foundry"); expect(html).not.toContain("<textarea");
  });
  it("show scope, questions and the standing notice for a policyholder, pinned to the record", () => {
    const html = withAssistants(access(surface), createElement(AssistantPanel, { surface: "concierge", context: { claim_id: "CLM-2001" } }));
    for (const text of ["Ask about this claim", "Only your own records · CLM-2001", "What happens next on this claim?", "Your question", "not a coverage decision", NOTICE]) expect(html).toContain(text);
    expect(html).toContain('role="log"'); expect(html).toContain("maxLength=\"4000\"");
    expect(withAssistants(access(surface), createElement(AssistantPanel, { surface: "concierge", context: { policy_id: "POL-1001" } }))).toContain("Ask about this policy");
    const general = withAssistants(access(surface), createElement(AssistantPanel, { surface: "concierge" }));
    expect(general).toContain("Ask about your policies and claims"); expect(general).toContain("Where do my claims stand?");
    expect(contextSuggestions({ policy_id: "POL-1" }, surface)).toHaveLength(3); expect(contextSuggestions(undefined, surface)).toEqual(surface.suggestions);
  });
  it("sit inside claim detail, the claims list and the policies page only when available", () => {
    expect(renderToStaticMarkup(createElement(ClaimDetail, claimProps))).not.toContain("Claims Concierge");
    const detail = withAssistants(access(surface), createElement(ClaimDetail, claimProps));
    expect(detail).toContain("Ask about this claim"); expect(detail).toContain("CLM-TEST"); expect(detail).not.toContain("AI assistance and voice");
    expect(detail.indexOf("Ask about this claim")).toBeLessThan(detail.indexOf("Documents and evidence review"));
    const policies = (value: AssistantAccess) => withAssistants(value, createElement(Policies, { api, canReport: true, onReport: noop, onError: noop }));
    expect(policies(access(surface))).toContain("Ask about your policies and claims"); expect(policies(noAssistants)).not.toContain("Claims Concierge");
    expect(policies(access())).not.toContain("<textarea");
  });
  it("link citations to stable ids on the policy wording they quote", () => {
    expect(wordingAnchor("NW-AUTO-WA-2026-v1#collision")).toBe("wording-NW-AUTO-WA-2026-v1-collision");
    expect(wordingAnchor("a b#c/d")).toBe("wording-a-b-c-d");
    expect(renderToStaticMarkup(createElement(PolicyView, { snapshot: policyExample }))).toContain('id="wording-WORD-TEST-collision"');
  });
});

describe("embedded help center", () => {
  const answeredExchange = (overrides: Partial<Exchange> = {}): Exchange => ({
    id: 1, question: "How do I report a loss?", status: "answered", answer: "Here is what the Help Center says:\n\n\u201cReport the loss as soon as you can.\u201d\n\u2014 How to report a loss",
    citations: [], sources: [helpSource], steps: [], provenance: { model: "gpt-test", prompt_sha: "abcdef123456", tool_calls: 0, human_review: false }, error: null, notice: "n", ...overrides,
  });
  const turn = (surfaceName: string, exchange: Exchange, label: string) =>
    renderToStaticMarkup(createElement(Turn, { exchange, latest: true, busy: false, label, variant: variantFor(surfaceName), onAgain: noop, onReveal: noop }));
  it("states plainly that it reads no personal records and offers general questions only", () => {
    const html = withAssistants(access(surface, helpSurface), createElement(AssistantPanel, { surface: "help_center" }));
    for (const text of ["Help Center · AI assistant", "Ask the Help Center", "Help articles only · no personal records", "How do I report a loss?", "cannot see your policy or claims", NOTICE, "not a coverage decision"]) expect(html).toContain(text);
    expect(html).not.toContain("Only your own records"); expect(html).not.toContain("Ask about this claim");
    expect(withAssistants(access(surface), createElement(AssistantPanel, { surface: "help_center" }))).toBe("");
    expect(variantFor("help_center").articles).toBe(true); expect(variantFor("concierge").articles).toBe(false); expect(variantFor("anything-else").articles).toBe(false);
  });
  it("never shows or sends a record hint, even if one is passed", () => {
    const html = withAssistants(access(helpSurface), createElement(AssistantPanel, { surface: "help_center", context: { claim_id: "CLM-2001" } }));
    expect(html).toContain("Help articles only · no personal records"); expect(html).toContain("How do I report a loss?");
    for (const text of ["CLM-2001", "What happens next on this claim?", "Ask about this claim"]) expect(html).not.toContain(text);
  });
  it("shows the quoted articles, no policy wording and no record counts for an answered help turn", () => {
    const html = turn("help_center", answeredExchange(), "Help Center");
    for (const text of ["Help Center", "Report the loss as soon as you can.", "Help articles quoted", "How to report a loss", "version 1", "effective 2026-01-01", "HELP-CLAIMS-001", "Personal records read", "None", "verbatim excerpt"]) expect(html).toContain(text);
    for (const text of ["Policy wording cited", "Records checked", "Human review recommended", "Show in policy wording"]) expect(html).not.toContain(text);
  });
  it("is honest when no article matched and no model was used", () => {
    const html = turn("help_center", answeredExchange({ sources: [], answer: "I couldn't find that in the Help Center articles, so I won't guess.", provenance: { model: "none", prompt_sha: "abcdef123456", tool_calls: 0, human_review: false } }), "Help Center");
    expect(html).toContain("Not used (no matching article)"); expect(html).not.toContain("Help articles quoted"); expect(html).not.toContain("<code>none</code>");
  });
  it("keeps the concierge turn showing records, wording and human review", () => {
    const html = turn("concierge", answeredExchange({ sources: [], citations: [{ ...citation }], provenance: { model: "gpt-test", prompt_sha: "abcdef123456", tool_calls: 2, human_review: true } }), "Claims Concierge");
    for (const text of ["Claims Concierge", "Policy wording cited (1)", "Show in policy wording", "Records checked", "Human review recommended", "A person needs to review this"]) expect(html).toContain(text);
    expect(html).not.toContain("Help articles quoted"); expect(html).not.toContain("Personal records read");
  });
  it("shows a failed help turn as no answer, never as partial text", () => {
    const html = turn("help_center", answeredExchange({ status: "failed", answer: null, sources: [], provenance: null, error: { code: "ungrounded_answer", message: "Nothing was shown." } }), "Help Center");
    expect(html).toContain("No answer was shown"); expect(html).toContain("ungrounded_answer"); expect(html).not.toContain("Help articles quoted"); expect(html).not.toContain("Here is what");
  });
  it("adds a collapsed disclosure beside the report form only when the server lists the surface", () => {
    const disclosure = (value: AssistantAccess) => withAssistants(value, createElement(AssistantDisclosure, { surface: "help_center", summary: "Ask the Help Center", note: "General questions only." }));
    expect(disclosure(noAssistants)).toBe(""); expect(disclosure(access(surface))).toBe(""); expect(disclosure({ ...access(helpSurface), api: null })).toBe("");
    const html = disclosure(access(helpSurface));
    expect(html).toContain("<details"); expect(html).not.toContain("<details open"); expect(html).toContain("Ask the Help Center"); expect(html).toContain("General questions only."); expect(html).toContain("<textarea");
    const report = (value: AssistantAccess) => withAssistants(value, createElement(ReportLoss, { api, identity: identityExample, initialPolicy: null, onCreated: noop, onClaims: noop, onError: noop }));
    const withHelp = report(access(helpSurface));
    expect(withHelp).toContain("Questions about reporting a loss? Ask the Help Center"); expect(withHelp).toContain("cannot see this form");
    expect(withHelp.indexOf("</form>")).toBeLessThan(withHelp.indexOf("Questions about reporting a loss?"));
    expect(withHelp.match(/<form/g)).toHaveLength(2);
    const without = report(noAssistants);
    expect(without).not.toContain("Ask the Help Center"); expect(without).toContain("not part of this form"); expect(without.match(/<form/g)).toHaveLength(1);
  });
  it("adds a Help center tab and page only for an account the server lists it for", () => {
    const tab = (value: AssistantAccess, active = false) => withAssistants(value, createElement(HelpTabButton, { active, onOpen: noop }));
    expect(tab(noAssistants)).toBe(""); expect(tab(access(surface))).toBe("");
    expect(tab(access(helpSurface))).toContain("Help center"); expect(tab(access(helpSurface), true)).toContain('aria-current="page"'); expect(tab(access(helpSurface))).not.toContain("aria-current");
    const page = (value: AssistantAccess) => withAssistants(value, createElement(HelpCenter));
    const both = page(access(surface, helpSurface));
    for (const text of ["Help center", "General questions", "Ask the Help Center", "cannot see your policy or claims", "ask the Claims Concierge there"]) expect(both).toContain(text);
    const onlyHelp = page(access(helpSurface));
    expect(onlyHelp).toContain("Ask the Help Center"); expect(onlyHelp).not.toContain("Claims Concierge there");
  });
});
