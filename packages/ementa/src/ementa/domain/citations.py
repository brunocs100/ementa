"""Citation verifier: an answer may only cite sources that were in the model's context.

The prompt asks for every legal reference to carry its source id in brackets, as in
"[cdc:art49]". A sentence is removed when it cites an id outside the context, or when it mentions
an article or a súmula in prose without the matching id next to it: "art. 1.003" needs an id of
`art1003`, "Súmula 385" one ending in `:sumula:385`, "Súmula Vinculante 13" one ending in `:sv:13`.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

_ID = re.compile(r"\[([a-z]+(?::[A-Za-z0-9-]+)+)\]")
_PROSE_ARTICLE = re.compile(
    r"\bart(?:igo)?s?\.?\s*(\d{1,3}(?:\.\d{3})+|\d+)((?:-[A-Z])?)", re.IGNORECASE
)
_PROSE_SUMULA = re.compile(r"\bs[úu]mula\s+(vinculante\s+)?(?:n[º°o.]*\s*)?(\d+)", re.IGNORECASE)
_ABBREVIATIONS = {
    "art",
    "arts",
    "inc",
    "n",
    "nº",
    "al",
    "p",
    "pp",
    "fls",
    "rel",
    "min",
    "dr",
    "dra",
}
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ\"“(\[])")


@dataclass(frozen=True)
class RemovedCitation:
    reference: str
    sentence: str
    reason: str


@dataclass
class VerificationResult:
    text: str
    valid: list[str] = field(default_factory=list)
    removed: list[RemovedCitation] = field(default_factory=list)

    @property
    def has_citations(self) -> bool:
        return bool(self.valid)


def extract_ids(text: str) -> list[str]:
    return _ID.findall(text)


def split_sentences(line: str) -> list[str]:
    """Split a line into sentences without breaking "art. 49" or "nº 8.078"."""
    sentences: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(line):
        before = line[start : match.start()].rstrip(".!?").split()
        if before and before[-1].lower().rstrip(".") in _ABBREVIATIONS:
            continue
        sentences.append(line[start : match.start()].strip())
        start = match.end()
    tail = line[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _check_sentence(
    sentence: str, allowed: frozenset[str], check_prose: bool = True
) -> tuple[list[str], list[RemovedCitation]]:
    ids = extract_ids(sentence)
    valid = [i for i in ids if i in allowed]
    problems = [
        RemovedCitation(i, sentence, "fonte fora do contexto recuperado")
        for i in ids
        if i not in allowed
    ]
    if not check_prose:
        return valid, problems
    # A prose mention counts as cited when an id in the same sentence points to it. When that id
    # is outside the context, the problem is already reported above.
    prose = _ID.sub(" ", sentence)
    for m in _PROSE_ARTICLE.finditer(prose):
        number = m.group(1).replace(".", "") + m.group(2).upper()
        if not any(re.search(rf":art{re.escape(number)}(?::|$)", v) for v in ids):
            problems.append(RemovedCitation(f"art. {m.group(1)}{m.group(2)}", sentence, _UNBACKED))
    for m in _PROSE_SUMULA.finditer(prose):
        kind, label = (":sv:", "Súmula Vinculante") if m.group(1) else (":sumula:", "Súmula")
        if not any(v.endswith(f"{kind}{m.group(2)}") for v in ids):
            problems.append(RemovedCitation(f"{label} {m.group(2)}", sentence, _UNBACKED))
    return valid, problems


_UNBACKED = "menção sem id de fonte verificado"


def verify_citations(
    answer: str, allowed: Iterable[str], *, check_prose: bool = True
) -> VerificationResult:
    """Keep the sentences whose citations are all in `allowed`; report the rest.

    With `check_prose=False` only the bracketed ids count: a text built from tool results, such
    as a deadline's premises ("CPC, art. 219"), may name an article without citing it.
    """
    allowed_ids = frozenset(allowed)
    result = VerificationResult(text="")
    kept_lines: list[str] = []
    for line in answer.split("\n"):
        kept: list[str] = []
        for sentence in split_sentences(line):
            valid, problems = _check_sentence(sentence, allowed_ids, check_prose)
            if problems:
                result.removed.extend(problems)
            else:
                kept.append(sentence)
                result.valid.extend(v for v in valid if v not in result.valid)
        kept_lines.append(" ".join(kept))
    result.text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    return result
