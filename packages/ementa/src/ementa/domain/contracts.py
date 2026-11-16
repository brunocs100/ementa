"""Consumer contracts: clauses, the firm's playbook and the deterministic part of its review.

A contract splits into clauses at its headings: "Cláusula 5ª - Do foro", "CLÁUSULA QUINTA",
"## 5. Do foro" or "5. DO FORO". What comes before the first one is the preamble (title and
parties); the place, date and signatures after the last one are left out.

A playbook rule matches a clause by patterns on its title and text, compared without accents
and case: every pattern in `all_of`, at least one in `any_of` and none in `none_of`. A rule may
also ask for a number past a limit next to an anchor word in one sentence: a multa of 10% next
to "multa", a carência of 180 days next to "urgência". The sentences where a rule matched are
the ones its redline rewrites. Criteria are the playbook items that need a model's judgment;
here they are only data.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from ementa.domain.citations import split_sentences

_ITEM_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SOURCE_ID = re.compile(r"^[a-z]+(?::[A-Za-z0-9-]+)+$")


class PlaybookError(ValueError):
    """A playbook outside the format: the message names the item and the field."""


class Severity(StrEnum):
    HIGH = "alta"
    MEDIUM = "media"
    LOW = "baixa"

    @property
    def rank(self) -> int:
        """0 for the most serious, so a sort puts it first."""
        return list(Severity).index(self)

    @property
    def label(self) -> str:
        return "média" if self is Severity.MEDIUM else self.value


def fold(text: str) -> str:
    """Lower case without accents, one character for each one: "Cláusula 5ª" gives
    "clausula 5a". Offsets stay the same, so a match in the folded text is a match at the same
    place in the original."""
    out: list[str] = []
    for ch in text:
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        plain = base if len(base) == 1 else ch
        lower = plain.lower()
        out.append(lower if len(lower) == 1 else plain)
    return "".join(out)


# Clauses ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clause:
    number: str  # "5" from the heading; the position when the contract numbers none
    title: str  # "Do foro"; empty when the heading has none
    text: str  # the body under the heading, as written

    @property
    def label(self) -> str:
        if not self.title:
            return f"Cláusula {self.number}"
        return f"Cláusula {self.number} \u2013 {self.title}"

    def sentences(self) -> list[str]:
        """The body's sentences, line by line, cut as the citation verifier cuts them."""
        return [s for line in self.text.split("\n") for s in split_sentences(line)]


@dataclass(frozen=True)
class Contract:
    title: str
    preamble: str
    clauses: tuple[Clause, ...]


_UNITS = {
    "primeira": 1,
    "segunda": 2,
    "terceira": 3,
    "quarta": 4,
    "quinta": 5,
    "sexta": 6,
    "setima": 7,
    "oitava": 8,
    "nona": 9,
}
_TENS = {"decima": 10, "vigesima": 20, "trigesima": 30, "quadragesima": 40}
_ORDINAL = rf"(?:(?:{'|'.join(_TENS)})(?:\s+(?:{'|'.join(_UNITS)}))?|{'|'.join(_UNITS)}|unica)"
# On folded text: "clausula 5a - do foro", "clausula decima primeira - da vigencia"; the dash
# may be a hyphen, an en dash or an em dash
_CLAUSE_HEADING = re.compile(
    rf"^clausula\s+(?:(?P<digits>\d{{1,3}})\s*(?:[ao]\b|°)?|(?P<ordinal>{_ORDINAL})\b)"
    r"\s*\.?\s*(?:[-\u2013\u2014:.]\s*)?(?P<title>.*?)\s*\.?$"
)
# On folded text, inside a Markdown heading: "5. do foro", "5 - do foro", "5) do foro"
_NUMBERED = re.compile(
    r"^(?P<digits>\d{1,3})(?![.,]\d)\s*[.)]?\s*(?:[-\u2013\u2014:]\s*)?(?P<title>.+?)\s*\.?$"
)
_SUB_ITEM = re.compile(r"^\d+[.,]\d")  # "### 5.1 ..." is a sub-item of clause 5, not a clause
# On the original text: a numbered heading in capitals, "5. DO FORO", in plain text
_CAPS_NUMBERED = re.compile(
    r"^(?P<digits>\d{1,2})\s*[.)]?\s*(?:[-\u2013\u2014]\s*)?"
    r"(?P<title>[A-ZÀ-Ý][A-ZÀ-Ý0-9 ,/\u2013-]{2,}?)\s*\.?$"
)
_MARKDOWN = re.compile(r"^\s{0,3}(?P<marks>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
_BOLD_LEAD = re.compile(r"^\s*(?:\*\*|__)(?P<head>.+?)(?:\*\*|__)\s*(?P<rest>.*)$")
_CLOSING = re.compile(
    r"^(?:_{3,}|(?:local e data|assinaturas?|testemunhas?)\b"
    r"|[^,]{2,60},\s*\d{1,2}\s+de\s+[a-z]+\s+de\s+\d{4}\s*\.?$)"
)


@dataclass(frozen=True)
class _Heading:
    number: str | None
    title: str
    rest: str = ""  # body text on the heading's own line ("**Cláusula 5.** Fica eleito...")
    document_title: bool = False


def _ordinal_value(words: str) -> int:
    parts = words.split()
    if parts[0] == "unica":
        return 1
    return _TENS.get(parts[0], 0) + sum(_UNITS.get(p, 0) for p in parts[-1:] if p in _UNITS)


def _parse_clause_heading(text: str) -> _Heading | None:
    """`text` as "Cláusula 5ª - Do foro" or "CLÁUSULA QUINTA - DO FORO"; None otherwise."""
    match = _CLAUSE_HEADING.match(fold(text.strip()))
    if match is None:
        return None
    stripped = text.strip()
    number = match.group("digits") or str(_ordinal_value(match.group("ordinal")))
    title = stripped[match.start("title") : match.end("title")].strip()
    return _Heading(str(int(number)), title)


def _heading(line: str) -> _Heading | None:
    markdown = _MARKDOWN.match(line)
    if markdown:
        text = markdown.group("text").strip().strip("*_").strip()
        if _SUB_ITEM.match(text):
            return None
        if len(markdown.group("marks")) == 1:
            return _parse_clause_heading(text) or _Heading(None, text, document_title=True)
        parsed = _parse_clause_heading(text)
        if parsed:
            return parsed
        numbered = _NUMBERED.match(fold(text))
        if numbered:
            title = text[numbered.start("title") : numbered.end("title")].strip()
            return _Heading(str(int(numbered.group("digits"))), title)
        return _Heading(None, text)
    bold = _BOLD_LEAD.match(line)
    if bold:
        parsed = _parse_clause_heading(bold.group("head"))
        if parsed:
            return _Heading(parsed.number, parsed.title, bold.group("rest").strip())
    parsed = _parse_clause_heading(line)
    if parsed:
        return parsed
    caps = _CAPS_NUMBERED.match(line.strip())
    if caps:
        return _Heading(str(int(caps.group("digits"))), caps.group("title").strip())
    return None


def _clean(lines: Sequence[str]) -> str:
    """Trailing spaces off, blank lines at the ends dropped and runs of them made one."""
    text = "\n".join(line.rstrip() for line in lines).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-") or "secao"


def split_contract(text: str) -> Contract:
    """The contract's title, preamble and clauses, in order.

    The first level-1 Markdown heading, or else the first line of a plain text, is the title.
    A heading without a number (an annex, say) is a clause named after its title when the
    contract numbers its clauses, and gets its position when none is numbered. A contract
    without any heading is split at its blank lines, each paragraph a clause.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title = ""
    preamble: list[str] = []
    sections: list[tuple[_Heading, list[str]]] = []
    closed = False
    for line in lines:
        heading = _heading(line)
        if heading is not None and heading.document_title:
            if not title and not sections:
                title = heading.title
                continue
            heading = _Heading(None, heading.title)  # a later level-1 heading opens a section
        if heading is not None:
            sections.append((heading, [heading.rest] if heading.rest else []))
            closed = False
            continue
        if not sections:
            if not title and line.strip():
                title = line.strip().strip("#*_ ").strip()
            else:
                preamble.append(line)
            continue
        if closed:
            continue
        if _CLOSING.match(fold(line.strip())):
            closed = True
            continue
        sections[-1][1].append(line)

    if not sections:  # no heading at all: one clause per paragraph
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", _clean(preamble)) if p.strip()]
        loose = tuple(Clause(str(n), "", p) for n, p in enumerate(paragraphs, start=1))
        return Contract(title, "", loose)

    numbered = any(h.number is not None for h, _ in sections)
    clauses: list[Clause] = []
    seen: dict[str, int] = {}
    for position, (heading, body) in enumerate(sections, start=1):
        content = _clean(body)
        if not content:
            continue
        number = heading.number or (_slug(heading.title) if numbered else str(position))
        seen[number] = seen.get(number, 0) + 1
        if seen[number] > 1:  # two clauses with the same number keep apart
            number = f"{number}-{seen[number]}"
        clauses.append(Clause(number, heading.title, content))
    return Contract(title, _clean(preamble), tuple(clauses))


# Playbook --------------------------------------------------------------------------------

_PERCENT = re.compile(r"(?<![\d,.])(?P<n>\d{1,3}(?:,\d{1,2})?)\s*%")
_DURATION = re.compile(
    r"(?<![\d,.])(?P<n>\d{1,4})\s*(?:\([^)]{0,40}\)\s*)?(?P<u>horas?|h|dias?|meses|mes|anos?)\b"
)
_HOURS = {"h": 1, "hora": 1, "dia": 24, "mes": 24 * 30, "meses": 24 * 30, "ano": 24 * 365}

Unit = Literal["percent", "hours"]


def _gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Characters between two spans; 0 when they touch or overlap."""
    return max(b[0] - a[1], a[0] - b[1], 0)


@dataclass(frozen=True)
class NumberLimit:
    """A number past `limit` next to an anchor, in one sentence: for each anchor, the nearest
    number of the unit counts. `context`, when given, must be in the same sentence."""

    anchor: re.Pattern[str]
    unit: Unit
    limit: float
    context: re.Pattern[str] | None = None

    def numbers(self, sentence: str) -> list[tuple[tuple[int, int], float]]:
        if self.unit == "percent":
            return [
                (m.span(), float(m.group("n").replace(",", ".")))
                for m in _PERCENT.finditer(sentence)
            ]
        found = []
        for m in _DURATION.finditer(sentence):
            unit = m.group("u")
            hours = _HOURS.get(unit, _HOURS.get(unit.rstrip("s"), 0))
            found.append((m.span(), float(int(m.group("n")) * hours)))
        return found

    def exceeded(self, folded_sentence: str) -> bool:
        if self.context is not None and not self.context.search(folded_sentence):
            return False
        numbers = self.numbers(folded_sentence)
        if not numbers:
            return False
        for anchor in self.anchor.finditer(folded_sentence):
            _, value = min(numbers, key=lambda n: _gap(anchor.span(), n[0]))
            if value > self.limit:
                return True
        return False


@dataclass(frozen=True)
class RuleHit:
    rule: str
    sentences: tuple[str, ...]  # where it matched; empty when only the title did


@dataclass(frozen=True)
class Rule:
    """A deterministic playbook rule, tied to the provisions in `basis` (source ids)."""

    id: str
    title: str
    severity: Severity
    basis: tuple[str, ...]
    why: str  # the reason, citing `basis` by id: "[cdc:art52:p1]"
    redline: str  # the firm's wording for the sentence it rewrites; empty removes the sentence
    all_of: tuple[re.Pattern[str], ...] = ()
    any_of: tuple[re.Pattern[str], ...] = ()
    none_of: tuple[re.Pattern[str], ...] = ()
    above: NumberLimit | None = None

    def check(self, clause: Clause) -> RuleHit | None:
        text = fold(f"{clause.title}\n{clause.text}")
        if not all(p.search(text) for p in self.all_of):
            return None
        if self.any_of and not any(p.search(text) for p in self.any_of):
            return None
        if any(p.search(text) for p in self.none_of):
            return None
        sentences = clause.sentences()
        folded = [fold(s) for s in sentences]
        if self.above is not None:
            over = tuple(
                s for s, f in zip(sentences, folded, strict=True) if self.above.exceeded(f)
            )
            return RuleHit(self.id, over) if over else None
        return RuleHit(self.id, self._locate(sentences, folded))

    def _locate(self, sentences: Sequence[str], folded: Sequence[str]) -> tuple[str, ...]:
        """The sentences to rewrite: those with an `any_of` pattern; without `any_of`, those
        with the most `all_of` patterns."""
        if self.any_of:
            return tuple(
                s
                for s, f in zip(sentences, folded, strict=True)
                if any(p.search(f) for p in self.any_of)
            )
        scores = [sum(bool(p.search(f)) for p in self.all_of) for f in folded]
        best = max(scores, default=0)
        return tuple(s for s, n in zip(sentences, scores, strict=True) if best and n == best)


@dataclass(frozen=True)
class Criterion:
    """A playbook item only a model can judge, tied to the provisions in `basis`."""

    id: str
    title: str
    severity: Severity
    basis: tuple[str, ...]
    why: str  # the law behind it, citing `basis` by id
    description: str  # what the model is told to look for
    redline: str  # the firm's wording when the model suggests none


@dataclass(frozen=True)
class Playbook:
    id: str
    version: int
    title: str
    scope: str
    rules: tuple[Rule, ...]
    criteria: tuple[Criterion, ...]

    @property
    def label(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def items(self) -> tuple[Rule | Criterion, ...]:
        return (*self.rules, *self.criteria)

    def item(self, item_id: str) -> Rule | Criterion:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def basis_ids(self) -> set[str]:
        return {i for item in self.items for i in item.basis}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Playbook":
        """Build and check a playbook from its YAML structure; PlaybookError says what is
        wrong and where."""
        if not isinstance(data, Mapping):
            raise PlaybookError("the playbook must be a mapping")
        playbook_id = _text(data, "id", "playbook")
        if not _ITEM_ID.match(playbook_id):
            raise PlaybookError(f"playbook: id {playbook_id!r} must be lower-case-with-hyphens")
        version = data.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise PlaybookError("playbook: version must be a positive integer")
        rules = tuple(_rule(raw) for raw in _items(data, "rules"))
        criteria = tuple(_criterion(raw) for raw in _items(data, "criteria", required=False))
        ids = [item.id for item in rules] + [item.id for item in criteria]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise PlaybookError(f"playbook: repeated ids {repeated}")
        return cls(
            playbook_id,
            version,
            _text(data, "title", "playbook"),
            _text(data, "scope", "playbook"),
            rules,
            criteria,
        )


def check_clause(clause: Clause, rules: Iterable[Rule]) -> list[RuleHit]:
    """The rules `clause` breaks, in playbook order."""
    return [hit for rule in rules if (hit := rule.check(clause)) is not None]


def _items(data: Mapping[str, Any], key: str, *, required: bool = True) -> list[Mapping[str, Any]]:
    raw = data.get(key)
    if raw is None and not required:
        return []
    if not isinstance(raw, list) or (required and not raw):
        raise PlaybookError(f"playbook: {key} must be a non-empty list")
    if not all(isinstance(item, Mapping) for item in raw):
        raise PlaybookError(f"playbook: every item in {key} must be a mapping")
    items: list[Mapping[str, Any]] = list(raw)
    return items


def _text(raw: Mapping[str, Any], key: str, where: str, *, empty: bool = False) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise PlaybookError(f"{where}: {key} must be {'a' if empty else 'a non-empty'} string")
    return " ".join(value.split()) if key != "redline" else value.strip()


def _common(raw: Mapping[str, Any], kind: str) -> tuple[str, str, Severity, tuple[str, ...]]:
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not _ITEM_ID.match(item_id):
        raise PlaybookError(f"{kind} {item_id!r}: id must be lower-case-with-hyphens")
    where = f"{kind} {item_id}"
    try:
        severity = Severity(str(raw.get("severity")))
    except ValueError:
        raise PlaybookError(
            f"{where}: severity must be one of {[s.value for s in Severity]}"
        ) from None
    basis = raw.get("basis")
    if not isinstance(basis, list) or not basis:
        raise PlaybookError(f"{where}: basis must list at least one source id")
    for source in basis:
        if not isinstance(source, str) or not _SOURCE_ID.match(source):
            raise PlaybookError(f"{where}: basis {source!r} is not a source id like cdc:art51:IV")
    return item_id, _text(raw, "title", where), severity, tuple(basis)


def _patterns(raw: Any, where: str) -> tuple[re.Pattern[str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        raise PlaybookError(f"{where} must be a list of regular expressions")
    return tuple(_compile(p, where) for p in raw)


def _compile(pattern: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(fold(pattern))
    except re.error as error:
        raise PlaybookError(f"{where}: bad pattern {pattern!r}: {error}") from None


def _limit(raw: Any, where: str) -> NumberLimit | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PlaybookError(f"{where}: above must be a mapping")
    unit = raw.get("unit")
    if unit not in ("percent", "hours"):
        raise PlaybookError(f"{where}: above.unit must be percent or hours")
    limit = raw.get("limit")
    if not isinstance(limit, int | float) or isinstance(limit, bool):
        raise PlaybookError(f"{where}: above.limit must be a number")
    anchor = raw.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        raise PlaybookError(f"{where}: above.anchor must be a regular expression")
    context = raw.get("context")
    if context is not None and (not isinstance(context, str) or not context):
        raise PlaybookError(f"{where}: above.context must be a regular expression")
    return NumberLimit(
        _compile(anchor, f"{where}: above.anchor"),
        unit,
        float(limit),
        _compile(context, f"{where}: above.context") if context else None,
    )


def _rule(raw: Mapping[str, Any]) -> Rule:
    item_id, title, severity, basis = _common(raw, "rule")
    where = f"rule {item_id}"
    match = raw.get("match")
    if not isinstance(match, Mapping):
        raise PlaybookError(f"{where}: match must be a mapping")
    unknown = set(match) - {"all", "any", "none", "above"}
    if unknown:
        raise PlaybookError(f"{where}: unknown match keys {sorted(unknown)}")
    rule = Rule(
        item_id,
        title,
        severity,
        basis,
        _text(raw, "why", where),
        _text(raw, "redline", where, empty=True),
        all_of=_patterns(match.get("all"), f"{where}: match.all"),
        any_of=_patterns(match.get("any"), f"{where}: match.any"),
        none_of=_patterns(match.get("none"), f"{where}: match.none"),
        above=_limit(match.get("above"), where),
    )
    if not (rule.all_of or rule.any_of or rule.above):
        raise PlaybookError(f"{where}: match needs all, any or above")
    return rule


def _criterion(raw: Mapping[str, Any]) -> Criterion:
    item_id, title, severity, basis = _common(raw, "criterion")
    where = f"criterion {item_id}"
    return Criterion(
        item_id,
        title,
        severity,
        basis,
        _text(raw, "why", where),
        _text(raw, "description", where),
        _text(raw, "redline", where),
    )


# Redlines --------------------------------------------------------------------------------

# A sub-item's number stays when its sentence is rewritten: "5.2", "a)", "§ 1º", "Parágrafo único."
_ENUMERATOR = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?|[a-z]\)|§\s*\d+\s*[º°o]?\.?|par[áa]grafo\s+[úu]nico\s*[.:]?)\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Edit:
    sentence: str | None  # a sentence of the clause; None adds `text` at the end
    text: str  # what takes its place; empty removes it


@dataclass(frozen=True)
class Change:
    before: str  # empty when the text is added
    after: str  # empty when the sentence is removed


@dataclass(frozen=True)
class Redraft:
    text: str
    changes: tuple[Change, ...]


def apply_edits(body: str, edits: Sequence[Edit]) -> Redraft:
    """The clause body with each targeted sentence rewritten once.

    Edits on the same sentence join their texts, each said once; when all of them are empty,
    the sentence goes. A sentence keeps its sub-item number ("5.2") unless the new text brings
    one. Edits without a sentence, or whose sentence is not in the body, add their texts as a
    last paragraph.
    """
    present = {s for line in body.split("\n") for s in split_sentences(line)}
    targeted: dict[str, list[str]] = {}
    added: list[str] = []
    for edit in edits:
        if edit.sentence is not None and edit.sentence in present:
            targeted.setdefault(edit.sentence, []).append(edit.text.strip())
        elif edit.text.strip():
            added.append(edit.text.strip())
    changes: list[Change] = []
    done: set[str] = set()
    lines: list[str] = []
    for line in body.split("\n"):
        if not line.strip():
            lines.append("")
            continue
        kept: list[str] = []
        carry = ""  # the number of a removed sub-item, for the sentence after it
        for sentence in split_sentences(line):
            if sentence not in targeted or sentence in done:
                numbered = carry and not _ENUMERATOR.match(sentence)
                kept.append(f"{carry}{sentence}" if numbered else sentence)
                carry = ""
                continue
            done.add(sentence)
            new = " ".join(dict.fromkeys(t for t in targeted[sentence] if t))
            lead = _ENUMERATOR.match(sentence)
            if new and lead and not _ENUMERATOR.match(new):
                new = f"{lead.group(0)}{new}"
            changes.append(Change(sentence, new))
            if new:
                kept.append(new)
            elif lead and not kept:
                carry = lead.group(0)
        if kept:
            lines.append(" ".join(kept))
    unique_added = list(dict.fromkeys(added))
    changes.extend(Change("", text) for text in unique_added)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if unique_added:
        text = f"{text}\n\n{' '.join(unique_added)}".strip()
    return Redraft(text, tuple(changes))
