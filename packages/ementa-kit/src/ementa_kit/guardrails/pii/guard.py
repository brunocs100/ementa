"""Pseudonymization with markers that stay the same for a value across a conversation.

`PiiGuard.pseudonymize` swaps each value for a marker such as `[CPF_1]` or `[PESSOA_2]` and
returns the mapping from marker to value; `rehydrate` puts the values back into the model's
answer. The caller keeps the mapping (in memory per request in F2, Redis with a TTL from F3).
Spans carry counts per entity type, never values or markers.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache

import spacy
from presidio_analyzer import AnalyzerEngine, EntityRecognizer, RecognizerRegistry
from presidio_analyzer.nlp_engine import NerModelConfiguration, SpacyNlpEngine

from ementa_kit.guardrails.pii.recognizers import (
    LANGUAGE,
    SCORE_THRESHOLD,
    PiiEntity,
    br_recognizers,
)
from ementa_kit.telemetry import get_tracer

# 42 MB wheel. NER F1 on WikiNER: sm 88.1, md 89.6, lg 90.3 (568 MB). On legal sentences the sm
# model missed uncommon first names that md finds; lg found one name more at 13 times the size.
DEFAULT_MODEL = "pt_core_news_md"

# NER has its own tok2vec in the pt_core_news pipelines, so nothing else needs to load.
_NOT_NER = ["tok2vec", "morphologizer", "parser", "lemmatizer", "senter", "attribute_ruler"]

_MARKER = re.compile(r"\[([A-Z]+(?:_[A-Z]+)*)_([0-9]+)\]")

# Spans that are never PII in Brazilian text. A detection entirely inside one is dropped.
_ALLOW = (
    re.compile(r"\b[0-9]{1,2}[/.-][0-9]{1,2}[/.-][0-9]{2,4}\b"),  # 15/11/2026, 15.11.2026
    re.compile(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b"),  # 2026-11-15
    re.compile(
        r"\b[0-9]{1,2}º?\s+de\s+(?:janeiro|fevereiro|março|abril|maio|junho|julho|agosto"
        r"|setembro|outubro|novembro|dezembro)(?:\s+de\s+[0-9]{4})?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:R\$|US\$|€)\s?[0-9][0-9.]*(?:,[0-9]{1,2})?(?:\s?(?:mil|milh[õo]es|bilh[õo]es)\b)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b[0-9][0-9.]*(?:,[0-9]{1,2})?\s?(?:reais|centavos|d[óo]lares|euros)\b", re.IGNORECASE
    ),
    _MARKER,  # markers already in the text, so a second pass leaves them as they are
    # Words the NER model takes for names
    re.compile(r"\b(?:CPF|CNPJ|RG|CEP|CNH|CTPS|PIS|NIS)\b"),
    re.compile(r"\b(?:Dr|Dra|Sr|Sra|Srta)\b\.?"),
)

# Two or more words in capitals, as party names come in petitions ("MARIA DA SILVA, brasileira").
_CAPS_RUN = re.compile(r"\b[A-ZÀ-ÖØ-Þ]{2,}(?:[ \t]+(?:[A-ZÀ-ÖØ-Þ]{2,}|E))+\b")

_WARM_UP = "Ana Lima, telefone (51) 99999-8888."  # fictitious

# A value found elsewhere is masked wherever it appears again, with the lowest priority in an
# overlap; values shorter than this are not repeated, as they would match plain words.
_ECHO_SCORE = 0.1
_MIN_ECHO = 3

tracer = get_tracer(__name__)


@dataclass(frozen=True)
class Detection:
    entity: str
    start: int
    end: int
    score: float


@dataclass(frozen=True)
class Pseudonymized:
    """`text` with markers; `mapping` from marker to value, including the ones passed in;
    `counts` of values replaced in this call, per entity type."""

    text: str
    mapping: dict[str, str]
    counts: dict[str, int]


class _NerOnlyEngine(SpacyNlpEngine):
    """The installed spaCy package with only its NER component. It never downloads a model."""

    def __init__(self, model: str) -> None:
        super().__init__(
            models=[{"lang_code": LANGUAGE, "model_name": model}],
            ner_model_configuration=NerModelConfiguration(
                model_to_presidio_entity_mapping={"PER": PiiEntity.PERSON.value},
                labels_to_ignore=["LOC", "ORG", "MISC"],
            ),
        )
        self.model_name = model

    def load(self) -> None:
        if not spacy.util.is_package(self.model_name):
            raise RuntimeError(f"spaCy model {self.model_name} is not installed; run `uv sync`")
        # Presidio types `nlp` as None until its own load() fills it
        self.nlp = {LANGUAGE: spacy.load(self.model_name, exclude=_NOT_NER)}  # type: ignore[assignment]


@cache
def _nlp_engine(model: str) -> _NerOnlyEngine:
    engine = _NerOnlyEngine(model)
    engine.load()
    return engine


def _title_case_capitals(text: str) -> str:
    """Title-case runs of words in capitals, keeping every offset, for the NER pass only."""

    def title(match: re.Match[str]) -> str:
        run = match.group(0)
        titled = run.title()
        return titled if len(titled) == len(run) else run

    return _CAPS_RUN.sub(title, text)


def _merge(detections: Iterable[Detection]) -> list[Detection]:
    """Overlapping detections become one span over all of them, typed by the highest score, so
    no part of a value is left in clear text."""
    spans: list[Detection] = []
    for current in sorted(detections, key=lambda d: (d.start, -d.end, -d.score)):
        if spans and current.start < spans[-1].end:
            last = spans[-1]
            strongest = current if current.score > last.score else last
            current = Detection(
                strongest.entity, last.start, max(last.end, current.end), strongest.score
            )
            spans[-1] = current
        else:
            spans.append(current)
    return spans


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _key(entity: str, value: str) -> str:
    """What makes two values the same: 123.456.789-09 and 12345678909 get one marker."""
    if entity == PiiEntity.EMAIL:
        return value.casefold()
    if entity == PiiEntity.PERSON:
        return " ".join(_fold(value).casefold().split())
    key = re.sub(r"[^0-9A-Z]", "", _fold(value).upper())
    if entity == PiiEntity.PHONE:
        if len(key) in (12, 13) and key.startswith("55"):  # +55 country code
            key = key[2:]
        elif len(key) in (11, 12) and key.startswith("0"):  # 0 or 0xx before the area code
            key = key[1:]
    return key or value


def _parse_marker(marker: str) -> tuple[str, int]:
    match = _MARKER.fullmatch(marker)
    if not match:  # the key is not echoed: a swapped mapping would put a value in the message
        raise ValueError("mapping keys must be markers such as [CPF_1]")
    return match.group(1), int(match.group(2))


class PiiGuard:
    """Finds Brazilian PII and swaps it for markers that stay the same across a conversation."""

    def __init__(self, analyzer: AnalyzerEngine, allow: Iterable[re.Pattern[str]] = ()) -> None:
        self._analyzer = analyzer
        self._allow = (*_ALLOW, *allow)

    def detect(self, text: str) -> list[Detection]:
        """PII spans in `text`, in order and without overlaps."""
        return self._detect(text, {})

    def _detect(self, text: str, known: Mapping[str, str]) -> list[Detection]:
        """`known` maps values already masked in the conversation to their entity. A known
        value, or one found once in `text`, counts wherever it appears: the NER model does not
        fire on every occurrence of a name."""
        artifacts = self._analyzer.nlp_engine.process_text(_title_case_capitals(text), LANGUAGE)
        results = self._analyzer.analyze(
            text, LANGUAGE, score_threshold=SCORE_THRESHOLD, nlp_artifacts=artifacts
        )
        found = [Detection(r.entity_type, r.start, r.end, r.score) for r in results]
        values = {**known, **{text[d.start : d.end]: d.entity for d in found}}
        for value, entity in values.items():
            if len(value) >= _MIN_ECHO:
                found += [
                    Detection(entity, m.start(), m.end(), _ECHO_SCORE)
                    for m in re.finditer(rf"(?<!\w){re.escape(value)}(?!\w)", text)
                ]
        allowed = [m.span() for pattern in self._allow for m in pattern.finditer(text)]
        kept = [d for d in found if not any(a <= d.start and d.end <= b for a, b in allowed)]
        return _merge(kept)

    def pseudonymize(
        self, text: str, mapping: Mapping[str, str] | None = None, *, stage: str = "input"
    ) -> Pseudonymized:
        """Replace each value with its marker. Pass the conversation's previous mapping to keep
        its markers; the returned mapping has the old entries plus the new ones.

        `stage` goes to the span: "input" before a model call, "output" for what leaves to an
        external MCP client. An error propagates, so the raw text is never returned by mistake.
        """
        with tracer.start_as_current_span(
            "guardrail pii",
            attributes={
                "gen_ai.operation.name": "guardrail",
                "ementa.guardrail.name": "pii",
                "ementa.guardrail.stage": stage,
            },
        ) as span:
            merged = dict(mapping or {})
            known: dict[tuple[str, str], str] = {}
            entities: dict[str, str] = {}
            last: Counter[str] = Counter()
            for old, value in merged.items():
                entity, n = _parse_marker(old)
                known[(entity, _key(entity, value))] = old
                entities[value] = entity
                last[entity] = max(last[entity], n)

            counts: Counter[str] = Counter()
            parts: list[str] = []
            cursor = 0
            for d in self._detect(text, entities):
                value = text[d.start : d.end]
                key = (d.entity, _key(d.entity, value))
                marker = known.get(key)
                if marker is None:
                    last[d.entity] += 1
                    marker = known[key] = f"[{d.entity}_{last[d.entity]}]"
                    merged[marker] = value
                parts += [text[cursor : d.start], marker]
                cursor = d.end
                counts[d.entity] += 1
            parts.append(text[cursor:])

            for entity, n in counts.items():
                span.set_attribute(f"ementa.pii.count.{entity.lower()}", n)
            span.set_attribute("ementa.pii.total", counts.total())
            span.set_attribute("ementa.guardrail.decision", "mask" if counts else "allow")
            return Pseudonymized("".join(parts), merged, dict(counts))


def rehydrate(text: str, mapping: Mapping[str, str]) -> str:
    """Put each value back where the model echoed its marker.

    Models rewrite markers now and then, so these all count as `[CPF_1]`: `[ CPF_1 ]`,
    `[cpf_1]`, `[CPF 1]`, `[CPF_01]`, the Markdown-escaped `\\[CPF\\_1\\]` and a bare `CPF_1`.
    A marker missing from the mapping stays as it is.
    """
    values: dict[tuple[str, int], str] = {}
    for marker, value in mapping.items():
        values[_parse_marker(marker)] = value
    if not values:
        return text
    labels = "|".join(
        re.escape(label).replace("_", r"\\?_")
        for label in sorted({label for label, _ in values}, key=len, reverse=True)
    )
    echo = re.compile(
        rf"\\?\[\s*(?P<label>{labels})(?:\\?_|\s)(?P<n>[0-9]+)\s*\\?\]"
        rf"|(?<!\w)(?P<bare>{labels})\\?_(?P<bare_n>[0-9]+)(?!\w)",
        re.IGNORECASE,
    )

    def restore(match: re.Match[str]) -> str:
        label = (match.group("label") or match.group("bare")).replace("\\", "").upper()
        n = int(match.group("n") or match.group("bare_n"))
        return values.get((label, n), match.group(0))

    return echo.sub(restore, text)


def build_pii_guard(
    *,
    extra_recognizers: Sequence[EntityRecognizer] = (),
    allow: Iterable[re.Pattern[str]] = (),
    model: str = DEFAULT_MODEL,
) -> PiiGuard:
    """A guard with the kit's recognizers plus `extra_recognizers` (a domain plugs its own in,
    in Portuguese), and `allow` patterns on top of the kit's dates, amounts and markers.

    The spaCy model loads once per process (a few seconds); build the guard once and reuse it.
    """
    for recognizer in extra_recognizers:
        if recognizer.supported_language != LANGUAGE:
            raise ValueError(
                f"recognizer for {recognizer.supported_entities} must support {LANGUAGE!r}"
            )
        for entity in recognizer.supported_entities:
            if not _MARKER.fullmatch(f"[{entity}_1]"):
                raise ValueError(f"entity {entity!r} must be upper-case letters and underscores")
    registry = RecognizerRegistry(
        recognizers=[*br_recognizers(), *extra_recognizers], supported_languages=[LANGUAGE]
    )
    analyzer = AnalyzerEngine(
        registry=registry, nlp_engine=_nlp_engine(model), supported_languages=[LANGUAGE]
    )
    guard = PiiGuard(analyzer, allow)
    guard.detect(_WARM_UP)  # compiles the patterns and loads phone metadata before a request
    return guard
