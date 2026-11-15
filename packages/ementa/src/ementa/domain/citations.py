"""Citation verifier: an answer may only cite sources that were in the model's context.

The prompt asks for every legal reference to carry its source id in brackets, as in
"[cdc:art49]". A sentence is removed when it cites an id outside the context, or when it mentions
an article or a súmula in prose without the matching id next to it: "art. 1.003" needs an id of
`art1003`, "Súmula 385" one ending in `:sumula:385`, "Súmula Vinculante 13" one ending in `:sv:13`.

Once verified, a brief writes its ids as references a court reads: `label_citations` turns
"[cdc:art42:pu]" into "(art. 42, parágrafo único, do CDC)".
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


_LAW_OF = {"cdc": "do CDC", "cpc": "do CPC", "lgpd": "da LGPD"}
_COURTS = {"stj": "STJ", "stf": "STF"}
_ARTICLE_ID = re.compile(
    r"(?P<law>[a-z]+):art(?P<number>\d+)(?P<suffix>-[A-Z])?(?P<parts>(?::[A-Za-z0-9-]+)*)"
)
_SUMULA_ID = re.compile(r"(?P<court>[a-z]+):(?P<kind>sumula|sv):(?P<number>\d+)")
_PARAGRAPH = re.compile(r"p(\d+)(-[A-Z])?")
_ITEM = re.compile(r"[IVXLCDM]+(?:-[A-Z])?|[a-z]")  # inciso (XI-A) or alínea, as the law writes
_ONE_ID = r"\[[a-z]+(?::[A-Za-z0-9-]+)+\]"
_ID_RUN = re.compile(rf"{_ONE_ID}(?:\s*[,;]?\s*{_ONE_ID})*")


def _ordinal(number: str) -> str:
    """As the law writes numbers: 1 to 9 take the ordinal sign (art. 6º, § 1º), 10 on do not
    (art. 10), and thousands take a dot (art. 1.035)."""
    value = int(number)
    return f"{value}º" if value < 10 else f"{value:,}".replace(",", ".")


def citation_label(source_id: str) -> str:
    """The reference a brief writes for a source id.

    `cdc:art49` is "art. 49 do CDC", `cdc:art42:pu` "art. 42, parágrafo único, do CDC",
    `cdc:art12:p1:I` "art. 12, § 1º, I, do CDC", `stj:sumula:385` "Súmula 385 do STJ" and
    `stf:sv:13` "Súmula Vinculante 13 do STF". An id of any other shape comes back as it is.
    """
    if sumula := _SUMULA_ID.fullmatch(source_id):
        court = _COURTS.get(sumula["court"])
        kind = "Súmula Vinculante" if sumula["kind"] == "sv" else "Súmula"
        return f"{kind} {int(sumula['number'])} do {court}" if court else source_id
    article = _ARTICLE_ID.fullmatch(source_id)
    if article is None or article["law"] not in _LAW_OF:
        return source_id
    parts = [f"art. {_ordinal(article['number'])}{article['suffix'] or ''}"]
    for part in article["parts"].split(":")[1:]:
        if part == "pu":
            parts.append("parágrafo único")
        elif paragraph := _PARAGRAPH.fullmatch(part):
            parts.append(f"§ {_ordinal(paragraph[1])}{paragraph[2] or ''}")
        elif _ITEM.fullmatch(part):
            parts.append(part)
        else:
            return source_id
    law = _LAW_OF[article["law"]]
    return f"{', '.join(parts)}, {law}" if len(parts) > 1 else f"{parts[0]} {law}"


def label_citations(text: str) -> str:
    """Write each bracketed id as the reference a brief uses, in parentheses. Ids cited side by
    side share one: "[cdc:art6:VIII] [cdc:art14]" becomes
    "(art. 6º, VIII, do CDC; art. 14 do CDC)"."""

    def label(match: re.Match[str]) -> str:
        ids = dict.fromkeys(_ID.findall(match.group(0)))
        return f"({'; '.join(citation_label(i) for i in ids)})"

    return _ID_RUN.sub(label, text)
