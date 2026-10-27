import { object, text } from "../models";

export const MAX_MESSAGE = 4000;
const MAX_TEXT = 6000, MAX_LABEL = 240, MAX_CITATIONS = 12, MAX_SOURCES = 8, MAX_SUGGESTIONS = 8;
const SESSION = /^asst-[a-z][a-z_]{0,31}-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/;

export type ToolStatus = "ok" | "not_found" | "invalid" | "error";
export interface SurfaceStatus {
  surface: string; label: string; available: boolean; reason: string | null;
  suggestions: string[]; accepts: ("claim" | "policy")[];
}
export interface AssistantStatus { enabled: boolean; notice: string; surfaces: SurfaceStatus[] }
// `none` marks a general-help surface that reads no personal records at all.
export interface Scope { records: "own" | "none"; claim_id: string | null; policy_id: string | null }
export interface Citation {
  policy_ref: string; title: string; version: string; state: string; effective_from: string;
  effective_to: string; section_id: string; source_uri: string; text: string;
}
export interface HelpSource { id: string; title: string; version: string; topic: string; effective_from: string; source_uri: string }
export interface ToolStep { name: string; status: ToolStatus; label: string }
export interface Provenance { model: string; prompt_sha: string; tool_calls: number; human_review: boolean }
export type AssistantEvent =
  | { kind: "session"; session_id: string; surface: string; scope: Scope }
  | ({ kind: "tool" } & ToolStep)
  | { kind: "assistant_delta"; text: string; agent: string }
  | { kind: "citations"; citations: Citation[] }
  | { kind: "sources"; sources: HelpSource[] }
  | { kind: "error"; code: string; message: string }
  | { kind: "done"; notice: string; provenance: Provenance | null };

function reject(field: string): never { throw new Error(`Invalid assistant response: ${field}.`); }
function bounded(value: unknown, field: string, max = MAX_LABEL): string {
  const result = text(value, field);
  return result.length <= max ? result : reject(field);
}
function nullableId(value: unknown, field: string, pattern: RegExp): string | null {
  if (value === null || value === undefined) return null;
  const result = bounded(value, field, 32);
  return pattern.test(result) ? result : reject(field);
}
function list<T>(value: unknown, parse: (entry: unknown) => T, field: string, max: number): T[] {
  return Array.isArray(value) && value.length <= max ? value.map(parse) : reject(field);
}
function bool(value: unknown, field: string): boolean { return typeof value === "boolean" ? value : reject(field); }

export function parseStatus(value: unknown): AssistantStatus {
  const v = object(value, "assistant status");
  return {
    enabled: bool(v.enabled, "enabled"), notice: bounded(v.notice, "notice", MAX_TEXT),
    surfaces: list(v.surfaces ?? [], entry => {
      const s = object(entry, "surface");
      return {
        surface: bounded(s.surface, "surface", 32), label: bounded(s.label, "label"), available: bool(s.available, "available"),
        reason: s.reason === null || s.reason === undefined ? null : bounded(s.reason, "reason", MAX_TEXT),
        suggestions: list(s.suggestions ?? [], item => bounded(item, "suggestion"), "suggestions", MAX_SUGGESTIONS),
        accepts: list(s.accepts ?? [], item => item === "claim" || item === "policy" ? item : reject("accepts"), "accepts", 2),
      };
    }, "surfaces", 8),
  };
}
function parseCitation(value: unknown): Citation {
  const v = object(value, "citation");
  return {
    policy_ref: bounded(v.policy_ref, "policy_ref"), title: bounded(v.title, "title"), version: bounded(v.version, "version", 64),
    state: bounded(v.state, "state", 8), effective_from: bounded(v.effective_from, "effective_from", 32),
    effective_to: bounded(v.effective_to, "effective_to", 32), section_id: bounded(v.section_id, "section_id"),
    source_uri: bounded(v.source_uri, "source_uri"), text: bounded(v.text, "text", MAX_TEXT),
  };
}
function parseSource(value: unknown): HelpSource {
  const v = object(value, "source");
  const id = bounded(v.id, "id", 64);
  if (!SOURCE_ID.test(id)) reject("id");
  return {
    id, title: bounded(v.title, "title", 120), version: bounded(v.version, "version", 32), topic: bounded(v.topic, "topic", 40),
    effective_from: bounded(v.effective_from, "effective_from", 32), source_uri: bounded(v.source_uri, "source_uri"),
  };
}
export function parseEvent(value: unknown): AssistantEvent {
  const v = object(value, "assistant event");
  switch (v.kind) {
    case "session": {
      const scope = object(v.scope, "scope");
      const sessionId = bounded(v.session_id, "session_id", 80);
      const records = scope.records;
      if (!SESSION.test(sessionId) || (records !== "own" && records !== "none")) reject("session");
      const claim_id = nullableId(scope.claim_id, "claim_id", /^CLM-\d+$/), policy_id = nullableId(scope.policy_id, "policy_id", /^POL-\d+$/);
      if (records === "none" && (claim_id !== null || policy_id !== null)) reject("session");
      return { kind: "session", session_id: sessionId, surface: bounded(v.surface, "surface", 32), scope: { records: records as Scope["records"], claim_id, policy_id } };
    }
    case "tool": {
      const status = v.status;
      if (status !== "ok" && status !== "not_found" && status !== "invalid" && status !== "error") reject("tool status");
      return { kind: "tool", name: bounded(v.name, "name", 64), status: status as ToolStatus, label: bounded(v.label, "label") };
    }
    case "assistant_delta":
      return { kind: "assistant_delta", text: bounded(v.text, "text", MAX_TEXT), agent: bounded(v.agent, "agent", 80) };
    case "citations":
      return { kind: "citations", citations: list(v.citations, parseCitation, "citations", MAX_CITATIONS) };
    case "sources":
      return { kind: "sources", sources: list(v.sources, parseSource, "sources", MAX_SOURCES) };
    case "error":
      return { kind: "error", code: bounded(v.code, "code", 64), message: bounded(v.message, "message", 1000) };
    case "done": {
      let provenance: Provenance | null = null;
      if (v.provenance !== null && v.provenance !== undefined) {
        const p = object(v.provenance, "provenance");
        const calls = p.tool_calls;
        if (typeof calls !== "number" || !Number.isInteger(calls) || calls < 0 || calls > 64) reject("tool_calls");
        provenance = {
          model: bounded(p.model, "model", 80), prompt_sha: bounded(p.prompt_sha, "prompt_sha", 64),
          tool_calls: calls as number, human_review: bool(p.human_review, "human_review"),
        };
      }
      return { kind: "done", notice: bounded(v.notice, "notice", MAX_TEXT), provenance };
    }
    default:
      return reject("event kind");
  }
}
