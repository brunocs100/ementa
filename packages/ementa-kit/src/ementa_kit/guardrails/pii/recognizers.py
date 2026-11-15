"""Presidio recognizers for Brazilian PII: CPF, CNPJ, RG, phone, CEP, e-mail and person names.

Each recognizer returns one entity type, and the type is also the marker label (`[CPF_1]`).
Numbers without a check digit (RG, CEP without its dash, phone without area code) only count
right after a keyword, so a bare digit run in a legal text is not taken for personal data.
"""

import re
from collections.abc import Callable, Sequence
from enum import StrEnum

from presidio_analyzer import EntityRecognizer, Pattern, PatternRecognizer
from presidio_analyzer.predefined_recognizers import PhoneRecognizer, SpacyRecognizer

LANGUAGE = "pt"

# A CPF or CNPJ in the official mask whose check digits fail still passes this threshold;
# PhoneRecognizer scores 0.4, the lowest result that counts.
SCORE_THRESHOLD = 0.4
MASKED_INVALID_SCORE = 0.5
KEYWORD_SCORE = 0.6
EMAIL_SCORE = 0.95


class PiiEntity(StrEnum):
    """Entity types found by the kit. The value is the marker label the model sees."""

    CPF = "CPF"
    CNPJ = "CNPJ"
    RG = "RG"
    PHONE = "TELEFONE"
    CEP = "CEP"
    EMAIL = "EMAIL"
    PERSON = "PESSOA"


_SEPARATORS = str.maketrans("", "", "./- ")
_CNPJ_WEIGHTS = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def _check_digit(values: Sequence[int], weights: Sequence[int]) -> int:
    rest = sum(v * w for v, w in zip(values, weights, strict=True)) % 11
    return 0 if rest < 2 else 11 - rest


def is_valid_cpf(value: str) -> bool:
    """Check digits of a CPF, with or without the mask. Eleven equal digits are invalid."""
    raw = value.translate(_SEPARATORS)
    if len(raw) != 11 or not raw.isascii() or not raw.isdigit() or len(set(raw)) == 1:
        return False
    digits = [int(c) for c in raw]
    first = _check_digit(digits[:9], range(10, 1, -1))
    second = _check_digit(digits[:10], range(11, 1, -1))
    return digits[9:] == [first, second]


def is_valid_cnpj(value: str) -> bool:
    """Check digits of a CNPJ, numeric or alphanumeric, with or without the mask.

    The alphanumeric CNPJ (issued from July 2026) uses the same weights, counting each character
    as its ASCII code minus 48: digits keep their value and A to Z count 17 to 42. The two check
    digits stay numeric.
    """
    raw = value.translate(_SEPARATORS).upper()
    if len(raw) != 14 or not raw.isascii() or not raw.isalnum() or not raw[12:].isdigit():
        return False
    if len(set(raw)) == 1:
        return False
    values = [ord(c) - 48 for c in raw]
    first = _check_digit(values[:12], _CNPJ_WEIGHTS[1:])
    second = _check_digit(values[:13], _CNPJ_WEIGHTS)
    return values[12:] == [first, second]


class _CheckDigitRecognizer(PatternRecognizer):
    """Scores 1.0 when the check digits pass. With wrong check digits, a value in the official
    mask keeps a lower score and is still masked; an unmasked digit run is dropped."""

    def __init__(
        self, entity: PiiEntity, pattern: str, mask: str, validator: Callable[[str], bool]
    ) -> None:
        super().__init__(
            supported_entity=entity.value,
            supported_language=LANGUAGE,
            name=f"{entity.value.title()}Recognizer",
            patterns=[Pattern(entity.value.lower(), pattern, MASKED_INVALID_SCORE)],
            global_regex_flags=0,  # case-sensitive: Presidio ignores case by default
        )
        self._mask = re.compile(mask)
        self._validator = validator

    def validate_result(self, pattern_text: str) -> bool | None:
        if self._validator(pattern_text):
            return True
        return None if self._mask.fullmatch(pattern_text) else False


def _after(keywords: str, value: str, gap: int = 15) -> str:
    """`value` right after one of `keywords` (any case), with up to `gap` non-digits between."""
    return rf"(?<=(?i:{keywords})[^0-9\n]{{0,{gap}}})(?<![0-9]){value}(?![0-9])"


def br_recognizers() -> list[EntityRecognizer]:
    """Fresh instances of every recognizer of the kit, for one analyzer."""
    cpf = _CheckDigitRecognizer(
        PiiEntity.CPF,
        r"(?<![0-9])[0-9]{3}\.?[0-9]{3}\.?[0-9]{3}-?[0-9]{2}(?![0-9])",
        mask=r"[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2}",
        validator=is_valid_cpf,
    )
    cnpj = _CheckDigitRecognizer(
        PiiEntity.CNPJ,
        r"(?<![0-9A-Z])[0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?[0-9]{2}(?![0-9A-Z])",
        mask=r"[0-9A-Z]{2}\.[0-9A-Z]{3}\.[0-9A-Z]{3}/[0-9A-Z]{4}-[0-9]{2}",
        validator=is_valid_cnpj,
    )
    rg = PatternRecognizer(
        supported_entity=PiiEntity.RG.value,
        supported_language=LANGUAGE,
        name="RgRecognizer",
        patterns=[
            Pattern(
                "rg_after_keyword",
                _after(
                    r"\brg\b|\br\.g\.|\bregistro geral\b|\bidentidade\b",
                    r"[0-9]{1,3}(?:\.?[0-9]{3}){2}(?:-?[0-9Xx])?",
                ),
                KEYWORD_SCORE,
            )
        ],
        global_regex_flags=0,
    )
    cep = PatternRecognizer(
        supported_entity=PiiEntity.CEP.value,
        supported_language=LANGUAGE,
        name="CepRecognizer",
        patterns=[
            Pattern("cep", r"(?<![0-9.])[0-9]{2}\.?[0-9]{3}-[0-9]{3}(?![0-9])", KEYWORD_SCORE),
            Pattern("cep_after_keyword", _after(r"\bcep\b", "[0-9]{8}", gap=5), KEYWORD_SCORE),
        ],
        global_regex_flags=0,
    )
    # Numbers with area code, validated against the Brazilian numbering plan by phonenumbers.
    phone = PhoneRecognizer(
        supported_language=LANGUAGE,
        supported_entity=PiiEntity.PHONE.value,
        supported_regions=("BR",),
    )
    local_phone = PatternRecognizer(
        supported_entity=PiiEntity.PHONE.value,
        supported_language=LANGUAGE,
        name="LocalPhoneRecognizer",
        patterns=[
            Pattern(
                "phone_after_keyword",
                _after(
                    r"\b(?:tel|telefone|fone|celular|cel|whatsapp|zap)\b",
                    r"(?:9[ .]?)?[0-9]{4}[-. ]?[0-9]{4}",
                    gap=12,
                ),
                KEYWORD_SCORE,
            )
        ],
        global_regex_flags=0,
    )
    email = PatternRecognizer(
        supported_entity=PiiEntity.EMAIL.value,
        supported_language=LANGUAGE,
        name="EmailRecognizer",
        patterns=[
            Pattern(
                "email",
                r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",
                EMAIL_SCORE,
            )
        ],
        global_regex_flags=0,
    )
    person = SpacyRecognizer(
        supported_language=LANGUAGE, supported_entities=[PiiEntity.PERSON.value]
    )
    return [cpf, cnpj, rg, cep, phone, local_phone, email, person]
