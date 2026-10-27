"""What the model has actually been shown this turn; the only basis for ids and citations in an answer."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from ..business.models import ClaimRecord, PolicySnapshot
from .models import Citation

IDENTIFIER = re.compile(r"\b(?:CLM|POL|PH)-\d+\b")


@dataclass
class Grounding:
    citations: dict[str, Citation] = field(default_factory=dict)
    ids: set[str] = field(default_factory=set)

    def add_policy(self, snapshot: PolicySnapshot) -> None:
        self.ids.add(snapshot.policy["policy_id"])
        for entry in snapshot.citations:
            try:
                citation = Citation(
                    policy_ref=entry["policy_ref"], title=entry["title"], version=entry["version"],
                    state=entry["state"], effective_from=entry["effective_from"],
                    effective_to=entry["effective_to"], section_id=entry["section_id"],
                    source_uri=entry["source_uri"], text=entry["text"],
                )
            except (KeyError, ValidationError):
                continue
            self.citations[citation.policy_ref] = citation

    def add_claim(self, claim: ClaimRecord) -> None:
        self.ids.update((claim.claim_id, claim.policy_id))

    def add_ids(self, text: str) -> None:
        self.ids.update(IDENTIFIER.findall(text))
