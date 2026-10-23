import { isObject, LossReport } from "./models";

export interface ReportCommand { key: string; hash: string }
export interface RetryStorage {
  getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem(key: string): void;
}
export class ReportStorageError extends Error {
  constructor() { super("Session storage is unavailable. This portal cannot safely preserve a report command across refresh. No new report request was sent."); }
}
export const reportStorageKey = (runId: string, policyholderId: string) =>
  `northwind.loss-command.${encodeURIComponent(runId)}.${encodeURIComponent(policyholderId)}`;
export function canonicalReport(report: LossReport): string {
  return JSON.stringify({
    policy_id: report.policy_id, vehicle_id: report.vehicle_id, incident_type: report.incident_type,
    loss_date: report.loss_date, state: report.state, description: report.description, synthetic_acknowledged: true,
  });
}
export async function reportHash(report: LossReport): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalReport(report));
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), b => b.toString(16).padStart(2, "0")).join("");
}
export function readCommand(storage: RetryStorage, slot: string): ReportCommand | null {
  let raw: string | null;
  try { raw = storage.getItem(slot); } catch { throw new ReportStorageError(); }
  if (raw === null) return null;
  let value: unknown;
  try { value = JSON.parse(raw); } catch {
    throw new Error("The saved report command is unreadable. Check your claims before explicitly starting a new report.");
  }
  if (!isObject(value) || typeof value.key !== "string" || !/^[0-9a-f-]{36}$/i.test(value.key)
    || typeof value.hash !== "string" || !/^[0-9a-f]{64}$/.test(value.hash)
    || Object.keys(value).some(key => !["key", "hash"].includes(key))) {
    throw new Error("The saved report command is unreadable. Check your claims before explicitly starting a new report.");
  }
  return { key: value.key, hash: value.hash };
}
export async function prepareReportCommand(report: LossReport, storage: RetryStorage, slot: string): Promise<ReportCommand> {
  const hash = await reportHash(report);
  return prepareHashedCommand(hash, storage, slot);
}
export function prepareHashedCommand(hash: string, storage: RetryStorage, slot: string, noun = "report"): ReportCommand {
  if (!/^[a-f0-9]{64}$/.test(hash)) throw new Error("A valid command fingerprint is required.");
  const previous = readCommand(storage, slot);
  if (previous) {
    if (previous.hash !== hash) throw new Error(`This ${noun} differs from the saved command. Restore the exact original fields to retry, or check the case and explicitly start a new ${noun}.`);
    return previous;
  }
  const command = { key: crypto.randomUUID(), hash };
  // Fail closed if persistence is blocked: an uncertain refresh must not generate a duplicate command.
  try { storage.setItem(slot, JSON.stringify(command)); } catch { throw new ReportStorageError(); }
  return command;
}
export function clearConfirmedCommand(storage: RetryStorage, slot: string, noun = "claim"): string | null {
  try { storage.removeItem(slot); return null; }
  catch {
    return `The ${noun} is confirmed, but its saved retry key could not be cleared. Check the case before starting another submission.`;
  }
}
