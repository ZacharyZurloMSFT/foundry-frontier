"""Shared access to the insurance fixtures and gate helpers for the custom evaluators.

`evals.insurance_gates` imports `evals.insurance` by absolute name, so the repository
root must be importable even when a script is started from `evals/`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.insurance import FIXTURE, canonical_citations  # noqa: E402
from evals.insurance_gates import unsafe_conclusion  # noqa: E402
from evals.insurance_review import valid_labels  # noqa: E402

__all__ = ["FIXTURE", "ROOT", "canonical_citations", "unsafe_conclusion", "valid_labels"]
