import { describe, expect, it } from "vitest";
import { canonicalReport, clearConfirmedCommand, prepareReportCommand, readCommand, reportStorageKey, RetryStorage } from "./reportRetry";
import { reportExample } from "./testData";
import { validateLossReport } from "./ReportLoss";

function memoryStorage(): RetryStorage {
  const values = new Map<string, string>();
  return { getItem: k => values.get(k) ?? null, setItem: (k, v) => { values.set(k, v); }, removeItem: k => { values.delete(k); } };
}
describe("loss command recovery", () => {
  it("persists only key/hash, recovers the exact command after refresh, and rejects edited reports", async () => {
    const storage = memoryStorage(), slot = reportStorageKey("test-run", "PH-TEST");
    const first = await prepareReportCommand(reportExample, storage, slot);
    expect(readCommand(storage, slot)).toEqual(first);
    expect(await prepareReportCommand({ ...reportExample }, storage, slot)).toEqual(first);
    const stored = storage.getItem(slot)!;
    expect(Object.keys(JSON.parse(stored))).toEqual(["key", "hash"]);
    expect(stored).not.toContain(reportExample.description);
    expect(stored).not.toContain("accessToken");
    await expect(prepareReportCommand({ ...reportExample, description: "An edited synthetic report" }, storage, slot)).rejects.toThrow("differs from the saved command");
    expect(readCommand(storage, slot)).toEqual(first);
  });
  it("requires explicit discard before a separate report and isolates run/customer keys", async () => {
    const storage = memoryStorage(), slot = reportStorageKey("test-run", "PH-TEST");
    const first = await prepareReportCommand(reportExample, storage, slot);
    storage.removeItem(slot);
    expect((await prepareReportCommand(reportExample, storage, slot)).key).not.toBe(first.key);
    expect(reportStorageKey("other-run", "PH-TEST")).not.toBe(slot);
    expect(reportStorageKey("test-run", "other-person")).not.toBe(slot);
  });
  it("fails safely for blocked or corrupted storage without sending requests", async () => {
    const blocked = { getItem: () => null, setItem: () => { throw new Error("blocked"); }, removeItem: () => {} };
    await expect(prepareReportCommand(reportExample, blocked, "slot")).rejects.toThrow("No new report request was sent");
    const storage = memoryStorage(); storage.setItem("slot", JSON.stringify({ key: "bad", hash: "bad", description: "must not be stored" }));
    expect(() => readCommand(storage, "slot")).toThrow("unreadable");
  });
  it("hashes a fixed property order, rather than incidental object ordering", () => {
    expect(canonicalReport({ synthetic_acknowledged: true, description: reportExample.description, state: "WA", loss_date: "2026-09-18", incident_type: "auto_collision", vehicle_id: "TEST-VIN", policy_id: "POL-TEST" })).toBe(canonicalReport(reportExample));
  });
  it("surfaces a cleanup warning without losing the confirmed outcome or its retry key", async () => {
    const storage = memoryStorage(), slot = "confirmed-report";
    const command = await prepareReportCommand(reportExample, storage, slot);
    const blocked = { ...storage, removeItem: () => { throw new Error("blocked"); } };
    expect(clearConfirmedCommand(blocked, slot)).toContain("claim is confirmed");
    expect(readCommand(storage, slot)).toEqual(command);
    expect(clearConfirmedCommand(storage, slot)).toBeNull();
    expect(readCommand(storage, slot)).toBeNull();
  });
});
describe("loss form validation", () => {
  it("validates dates, state, incident, vehicle, and bounded synthetic descriptions", () => {
    expect(validateLossReport(reportExample, "2026-10-15")).toBeNull();
    for (const extra of [
      { loss_date: "2027-01-01" }, { loss_date: "2026-02-30" }, { loss_date: "yesterday" },
      { state: "Washington" }, { state: "wa" }, { vehicle_id: "" }, { description: "short" }, { description: "x".repeat(2001) },
    ]) expect(validateLossReport({ ...reportExample, ...extra }, "2026-10-15")).not.toBeNull();
  });
});
