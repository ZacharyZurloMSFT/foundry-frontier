import { ClaimRecord, Identity, LossReport, PolicySnapshot } from "./models";

// Synthetic contract examples used by tests only; never imported by the portal.
export const policyExample: PolicySnapshot = {
  source_version: "source-v1",
  policy: {
    policy_id: "POL-TEST", policyholder_id: "PH-TEST", status: "active", effective_from: "2026-01-01", effective_to: "2026-12-31",
    state: "WA", insured_asset: { type: "auto", description: "Synthetic sedan", vin: "TEST-VIN" },
    coverages: [{ coverage_type: "collision", limit_usd: 30000, deductible_usd: 500 }],
    endorsements: [{ endorsement_id: "END-TEST", title: "Synthetic endorsement", version: "1", state: "WA", effective_from: "2026-01-01", effective_to: "2026-12-31" }],
    wording_id: "WORD-TEST",
  },
  policyholder: { policyholder_id: "PH-TEST", name: "Synthetic Customer", email: "synthetic@example.test", phone: "+1-555-0100" },
  policy_wording: {
    wording_id: "WORD-TEST", title: "Test auto wording", version: "1", state: "WA", effective_from: "2026-01-01", effective_to: "2026-12-31",
    sections: [{ section_id: "collision", policy_ref: "WORD-TEST#collision", text: "Synthetic wording for review.", source_uri: "synthetic://WORD-TEST" }],
  },
  citations: [{ section_id: "collision", policy_ref: "WORD-TEST#collision", text: "Synthetic citation.", source_uri: "synthetic://WORD-TEST", title: "Test auto wording", version: "1", state: "WA", effective_from: "2026-01-01", effective_to: "2026-12-31" }],
};
export const identityExample: Identity = {
  run_id: "test-run", role: "policyholder", policyholder_id: "PH-TEST", source_version: "source-v1", synthetic: true, adjusters: [],
};
export const claimExample: ClaimRecord = {
  claim_id: "CLM-TEST", run_id: "test-run", policy_id: "POL-TEST", policyholder_id: "PH-TEST",
  incident_type: "auto_collision", loss_date: "2026-09-18", state: "WA", description: "Synthetic collision report.",
  status: "reported", version: 1, created_at: "2026-10-15T15:00:00Z", updated_at: "2026-10-15T15:00:00Z",
  policy_snapshot: policyExample, review_flags: ["human_review_required"], outstanding_evidence: ["Repair estimate"], synthetic: true,
  timeline: [{ event_id: "event-1", command_id: "command-1", actor_id: "test-user", actor_role: "policyholder", occurred_at: "2026-10-15T15:00:00Z", summary: "Report received", before_version: 0, after_version: 1, internal_note: null, visibility: "customer" }],
  tasks: [], notes: [], assigned_adjuster_id: null,
};
export const reportExample: LossReport = {
  policy_id: "POL-TEST", vehicle_id: "TEST-VIN", incident_type: "auto_collision", loss_date: "2026-09-18",
  state: "WA", description: "Synthetic collision report.", synthetic_acknowledged: true,
};
export const configExample = {
  tenantId: "11111111-1111-1111-1111-111111111111", clientId: "22222222-2222-2222-2222-222222222222",
  scope: "api://33333333-3333-3333-3333-333333333333/access_as_user", runId: "test-run", apiBase: "http://localhost:8000",
};
