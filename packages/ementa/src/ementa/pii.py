"""Legal side of the PII guardrail: the OAB number, and what looks like personal data in a legal
text but is not (CNJ case numbers, statutes, articles, súmulas, source ids, court panels and
procedural roles).

The API and the MCP server use `pii_guard()`; the kit's guard alone knows none of these.
"""

import re
from functools import cache

from presidio_analyzer import Pattern, PatternRecognizer

from ementa_kit.guardrails.pii import LANGUAGE, PiiGuard, build_pii_guard

OAB = "OAB"

_UF = r"(?:AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)"
_OAB_NUMBER = r"[0-9]{1,3}(?:\.?[0-9]{3})?(?:-?[A-Z])?"

# A detection entirely inside one of these spans is dropped. Numbers are bounded (a statute has
# at most five digits), so a CPF written after "Lei" or "art." is still caught.
LEGAL_ALLOW: tuple[re.Pattern[str], ...] = (
    # CNJ case number NNNNNNN-DD.AAAA.J.TR.OOOO, with or without punctuation
    re.compile(r"(?<![0-9])[0-9]{7}-?[0-9]{2}\.?[0-9]{4}\.?[0-9]\.?[0-9]{2}\.?[0-9]{4}(?![0-9])"),
    # Lei 8.078/1990, Lei nº 13.709/2018, LC 123/2006, Decreto-Lei 4.657/1942, Resolução CNJ 615
    re.compile(
        r"\b(?i:lei(?:\s+complementar)?|lc|decreto(?:-lei)?|medida\s+provis[óo]ria|mp"
        r"|emenda\s+constitucional|ec|resolu[çc][ãa]o|provimento|portaria)\b"
        r"(?:\s+[A-Z]{2,6})?\s*(?:(?i:n)[º°o.]*\s*)?"
        r"(?:[0-9]{1,2}\.[0-9]{3}|[0-9]{1,5})(?![0-9])(?:/[0-9]{2,4})?"
    ),
    # Laws named after a person
    re.compile(
        r"\b(?i:lei)\s+(?:Maria da Penha|Carolina Dieckmann|Rouanet|Pel[ée]|Aldir Blanc"
        r"|Paulo Gustavo|Henry Borel|Mariana Ferrer|Lucas|Berenice Piana|Romeo Mion)\b"
    ),
    # art. 49, Art. 6º, artigo 42, art. 1.022, § 2º
    re.compile(r"\b(?i:arts?\.?|artigos?)\s*[0-9]{1,4}(?:\.[0-9]{3})?(?:[º°o]|-[A-Z])?"),
    re.compile(r"§{1,2}\s*[0-9]{1,3}[º°o]?"),
    # Súmula 385 do STJ, Súmula Vinculante 13
    re.compile(
        r"\b(?i:s[úu]mula)(?:\s+(?i:vinculante))?\s*(?:(?i:n)[º°o.]*\s*)?[0-9]{1,4}"
        r"(?:\s+do\s+(?:STJ|STF|TST|TSE))?"
    ),
    # Source ids: cdc:art49, cdc:art42:pu, cpc:art219, stj:sumula:385
    re.compile(r"\b[a-z]+:(?:art[0-9]+[A-Za-z0-9-]*|sumula:[0-9]+)(?::[A-Za-z0-9-]+)*"),
    # Codes and courts by acronym, court panels and procedural roles: the NER model takes some
    # of them for names ("CPC", "Terceira Turma", "Réu")
    re.compile(
        r"\b(?:CPC|CDC|CC|CF|CLT|CTN|CP|CPP|ECA|CTB|LGPD|LINDB|ADCT|STF|STJ|TST|TSE|STM"
        r"|TJ[A-Z]{2}|TRF[1-6]?|TRT[0-9]{0,2}|TRE-?[A-Z]{2})\b"
    ),
    re.compile(
        r"\b(?:primeira|segunda|terceira|quarta|quinta|sexta|s[ée]tima|oitava|nona|d[ée]cima)"
        r"\s+(?:turma|se[çc][ãa]o|c[âa]mara)\b|\b(?:corte|[óo]rg[ãa]o)\s+especial\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:autora?|autores|r[ée]us?|r[ée]|relatora?|revisora?|ju[ií]za?|desembargadora?"
        r"|ministr[oa]|promotora?|procuradora?|defensora?|requerente|requerid[oa]|reclamante"
        r"|reclamad[oa]|exequente|executad[oa]|apelante|apelad[oa]|agravante|agravad[oa]"
        r"|recorrente|recorrid[oa]|impetrante|impetrad[oa]|embargante|embargad[oa])\b",
        re.IGNORECASE,
    ),
)


def oab_recognizer() -> PatternRecognizer:
    """OAB registration: "OAB/RS 123.456", "OAB-SP nº 98.765", "OAB 12.345/RS", "OAB/RJ 1.234-E".

    The whole reference becomes one marker, so `[OAB_1]` stands for the lawyer.
    """
    return PatternRecognizer(
        supported_entity=OAB,
        supported_language=LANGUAGE,
        name="OabRecognizer",
        patterns=[
            Pattern(
                "oab_uf_number", rf"\bOAB\s?[/-]?\s?{_UF}\b[^0-9\n]{{0,10}}{_OAB_NUMBER}\b", 0.9
            ),
            Pattern("oab_number_uf", rf"\bOAB\b[^0-9\n]{{0,10}}{_OAB_NUMBER}\s?/\s?{_UF}\b", 0.9),
        ],
        global_regex_flags=0,  # capitals only: with Presidio's default, "se" or "to" pass as a UF
    )


@cache
def pii_guard() -> PiiGuard:
    """The guard for the API and the MCP server: the kit's recognizers plus the legal ones.

    Built once per process; the first call loads the spaCy model (a few seconds).
    """
    return build_pii_guard(extra_recognizers=[oab_recognizer()], allow=LEGAL_ALLOW)
