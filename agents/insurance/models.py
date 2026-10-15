from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InsuranceTriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str | None = Field(pattern=r"^CLM-\d+$")
    policy_id: str | None = Field(pattern=r"^POL-\d+$")
    policyholder_id: str | None = Field(pattern=r"^PH-\d+$")
    intent: Literal["FNOL", "CLAIM_STATUS", "POLICY_QUESTION", "EVIDENCE_SUBMISSION",
                    "ADJUSTER_REQUEST", "SETTLEMENT_QUERY", "COMPLAINT", "OTHER"]
    sentiment: Literal["calm", "frustrated", "angry"]
    urgency: Literal["P1", "P2", "P3", "P4"]
    problem_summary: str = Field(min_length=1, max_length=2000)
