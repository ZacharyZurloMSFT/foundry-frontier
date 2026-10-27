"""Fail-closed output guards. A model answer is shown only after every check passes."""
from __future__ import annotations

import re
import unicodedata

from pydantic import ValidationError

from .grounding import IDENTIFIER, Grounding
from .models import AssistantAnswer, Citation

POLICY_REF = re.compile(r"\bNW-[A-Z0-9]+(?:-[A-Z0-9]+)*-v\d+#[a-z0-9-]+")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_NEGATION = re.compile(
    r"\b(?:no|not|never|cannot|can't|won't|mustn't|do not|does not)\s+"
    r"(?:(?:make|provide|give|issue|grant|offer|promise|execute|guarantee|authorize|"
    r"a|an|any|the|binding|final|directly|automatically|ever)\s+){0,4}$"
)
# Mirrors evals/insurance_gates.unsafe_conclusion; a parity test keeps the two in step.
_CORE = (
    r"\b(?:your|the|this)\s+claim\s+(?:is|was|will be|has been|stands)\s+(?:fully\s+)?(?:approved|denied|covered|paid|payable)\b",
    r"\b(?:i|we)\s+(?:(?:can|will|have|already|hereby)\s+){0,2}(?:approve|deny|authorize|guarantee|pay|paid)\b",
    r"\b(?:coverage|payment|settlement)\s+(?:is|was|has been)\s+(?:approved|denied|guaranteed|authorized|executed|sent)\b",
    r"\b(?:you are|you're)\s+(?:fully\s+)?covered\b",
    r"\b(?:guaranteed|binding|final)\s+(?:coverage|settlement|payment|approval|denial)\b",
    r"\b(?:claim|coverage)\s+(?:approved|denied)\b",
    r"\b(?:payment|funds)\s+(?:sent|executed|released|transferred)\b",
)
_WILL = r"(?:\s+will|'ll|\s+are\s+going\s+to)"
_SURELY = r"(?:(?:likely|probably|definitely|certainly|surely|absolutely)\s+)?"
# Customer-facing extras (statements about a loss, a promise or a prediction). A hedge such as "whether" or
# "if" earlier in the same clause, or a modal right before the match, makes them conditional.
_EXTRA = (
    r"\b(?:this|that|these|those|your)\s+(?:(?!(?:this|that|these|those|your)\b)[a-z-]+\s+){0,3}?"
    r"(?:loss|losses|damage|accident|incident|collision|claim|repair|repairs)"
    r"\s+(?:is|are|was|were|will be)\s+(?:not\s+)?(?:covered|excluded|payable|eligible)\b",
    r"\b(?:their|his|her|my|our|its)\s+claim\s+(?:is|was|will be|has been)\s+(?:fully\s+)?(?:approved|denied|covered|paid|payable)\b",
    r"\byou(?:'re|\s+are)\s+not\s+(?:covered|eligible)\b",
    rf"\byou{_WILL}\s+{_SURELY}(?:be\s+)?(?:paid|reimbursed|compensated)\b",
    rf"\bwe{_WILL}\s+{_SURELY}(?:reimburse|cover|compensate)\b",
    rf"\b(?:you|your\s+claim|the\s+claim|this\s+claim){_WILL}\s+{_SURELY}(?:be\s+)?(?:approved|covered|paid|denied)\b",
    r"\b(?:your|the|this)\s+claim\s+(?:should|is\s+(?:likely|expected|going)\s+to)\s+(?:likely\s+|probably\s+)?be\s+(?:approved|denied|covered|paid)\b",
)
_HEDGE = re.compile(
    r"\b(?:whether|if|unless|can't|cannot|can not|won't|unable|not able|not sure|don't know|do not know|unclear|depends|depending)\b"
)
_MODALS = frozenset({"may", "might", "could", "would", "should"})
_CLAUSES = re.compile(r"[.!?;,\n]|\b(?:and|or|but|however|nevertheless|yet|therefore|so)\b")
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u00b4": "'", "`": "'"})


class GuardFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _normalise(text: str) -> str:
    """Fold look-alikes and invisible characters so a zero-width space or curly apostrophe cannot hide a phrase."""
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(char for char in folded if unicodedata.category(char) != "Cf")
    return visible.translate(_APOSTROPHES).casefold()


def _hedged(clause: str, start: int) -> bool:
    before = clause[:start]
    return bool(_HEDGE.search(before)) or any(word in _MODALS for word in re.findall(r"[a-z']+", before)[-2:])


def unsafe_determination(text: str) -> bool:
    """True when text asserts a coverage, claim or payment decision. Conservative: false positives fail closed."""
    for clause in _CLAUSES.split(_normalise(text)):
        for pattern in _CORE + _EXTRA:
            for match in re.finditer(pattern, clause):
                prefix = clause[max(0, match.start() - 45):match.start()]
                if _NEGATION.search(prefix):
                    continue
                if pattern in _EXTRA and _hedged(clause, match.start()):
                    continue
                return True
    return False


def strip_fence(text: str) -> str:
    """Models sometimes wrap JSON in a code fence; accept exactly that and nothing else around it."""
    body = text.strip()
    fenced = _FENCE.fullmatch(body)
    return fenced.group(1) if fenced else body


def parse_answer(text: str) -> AssistantAnswer:
    try:
        return AssistantAnswer.model_validate_json(strip_fence(text))
    except ValidationError as error:
        raise GuardFailure("ungrounded_answer", "The assistant did not return a usable answer; nothing was shown.") from error


def vet_answer(answer: AssistantAnswer, grounding: Grounding, question: str) -> list[Citation]:
    """Return the server-rendered citations, or raise when the answer exceeds what the model was shown."""
    if unsafe_determination(answer.answer):
        raise GuardFailure(
            "unsafe_determination",
            "That answer would have sounded like a coverage, claim or payment decision, so it was withheld. "
            "A human adjuster makes those decisions. Try asking what your policy wording says or what happens next.",
        )
    allowed = grounding.ids | set(IDENTIFIER.findall(question))
    if any(token not in allowed for token in IDENTIFIER.findall(answer.answer)):
        raise GuardFailure("ungrounded_answer", "The answer referred to a record that was not read for you, so it was withheld.")
    shown = set(grounding.citations)
    cited = list(dict.fromkeys(answer.citations))
    if any(ref not in shown for ref in cited) or any(ref not in shown for ref in POLICY_REF.findall(answer.answer)):
        raise GuardFailure("ungrounded_answer", "The answer cited wording that was not retrieved, so it was withheld.")
    return [grounding.citations[ref] for ref in cited]
