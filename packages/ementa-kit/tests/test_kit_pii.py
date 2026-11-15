"""PII guardrail. Every CPF, CNPJ, name, phone, e-mail and address here is fictitious."""

import logging
import re
import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from presidio_analyzer import Pattern, PatternRecognizer

from ementa_kit.guardrails.pii import (
    PiiGuard,
    build_pii_guard,
    is_valid_cnpj,
    is_valid_cpf,
    rehydrate,
)


@pytest.fixture(scope="module")
def guard() -> PiiGuard:
    return build_pii_guard()


@pytest.fixture(scope="module")
def spans() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def found(guard: PiiGuard, text: str) -> list[tuple[str, str]]:
    return [(d.entity, text[d.start : d.end]) for d in guard.detect(text)]


def cpf_with_check_digits(base: str) -> str:
    """Textbook CPF check digits, written apart from the code under test."""
    digits = [int(c) for c in base]
    for size in (9, 10):
        total = sum(d * (size + 1 - i) for i, d in enumerate(digits[:size]))
        digits.append(total * 10 % 11 % 10)
    return "".join(map(str, digits))


def cnpj_with_check_digits(base: str) -> str:
    """Same for the CNPJ, alphanumeric included: weights 2 to 9 from the right, ASCII minus 48."""
    values = [ord(c) - 48 for c in base]
    for size in (12, 13):
        weights = [(size - 1 - i) % 8 + 2 for i in range(size)]
        values.append(sum(v * w for v, w in zip(values, weights, strict=True)) * 10 % 11 % 10)
    return base + "".join(str(v) for v in values[12:])


# Check digits ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("123.456.789-09", True),
        ("12345678909", True),
        ("111.444.777-35", True),
        ("123.456.789-00", False),
        ("12345678900", False),
        ("111.111.111-11", False),  # right arithmetic, but equal digits are never issued
        ("1234567890", False),
        ("123.456.789-0X", False),
    ],
)
def test_cpf_check_digits(value: str, valid: bool) -> None:
    assert is_valid_cpf(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("11.222.333/0001-81", True),
        ("11222333000181", True),
        ("12.ABC.345/01DE-35", True),  # alphanumeric, example of the Receita Federal
        ("12ABC34501DE35", True),
        ("11.222.333/0001-80", False),
        ("12.ABC.345/01DE-36", False),
        ("00.000.000/0000-00", False),
        ("12.ABC.345/01DE-3A", False),
    ],
)
def test_cnpj_check_digits(value: str, valid: bool) -> None:
    assert is_valid_cnpj(value) is valid


cpf_bases = st.text(string.digits, min_size=9, max_size=9).filter(lambda s: len(set(s)) > 1)
cnpj_bases = st.text(string.digits + string.ascii_uppercase, min_size=12, max_size=12).filter(
    lambda s: len(set(s)) > 1
)


@given(cpf_bases)
def test_any_cpf_with_its_check_digits_is_valid_and_a_wrong_last_digit_is_not(base: str) -> None:
    cpf = cpf_with_check_digits(base)

    assert is_valid_cpf(cpf)
    assert is_valid_cpf(f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}")
    assert not is_valid_cpf(cpf[:10] + str((int(cpf[10]) + 1) % 10))


@given(cnpj_bases)
def test_any_cnpj_with_its_check_digits_is_valid_and_a_wrong_last_digit_is_not(
    base: str,
) -> None:
    cnpj = cnpj_with_check_digits(base)

    assert is_valid_cnpj(cnpj)
    assert is_valid_cnpj(f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}")
    assert not is_valid_cnpj(cnpj[:13] + str((int(cnpj[13]) + 1) % 10))


def masked_as(guard: PiiGuard, entity: str, value: str) -> bool:
    """`value` sits inside one marker of `entity`. The marker may cover a word more: the NER
    model sometimes reads "CNPJ A0.000.00A" as a name, and overlaps merge into one span."""
    result = guard.pseudonymize(f"{entity} {value} confere")
    return value not in result.text and any(
        marker.startswith(f"[{entity}_") and value in original
        for marker, original in result.mapping.items()
    )


@settings(max_examples=25, deadline=None)
@given(cpf_bases)
def test_any_valid_cpf_is_masked_with_or_without_the_mask(guard: PiiGuard, base: str) -> None:
    cpf = cpf_with_check_digits(base)

    assert masked_as(guard, "CPF", cpf)
    assert masked_as(guard, "CPF", f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}")


@settings(max_examples=25, deadline=None)
@given(cnpj_bases)
def test_any_valid_cnpj_is_masked_with_or_without_the_mask(guard: PiiGuard, base: str) -> None:
    cnpj = cnpj_with_check_digits(base)

    assert masked_as(guard, "CNPJ", cnpj)
    assert masked_as(guard, "CNPJ", f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}")


# Detection matrix ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # CPF: a bare digit run counts only with right check digits; the official mask always
        ("CPF 123.456.789-09", [("CPF", "123.456.789-09")]),
        ("CPF 12345678909", [("CPF", "12345678909")]),
        ("CPF 123456789-09", [("CPF", "123456789-09")]),
        ("CPF 123.456.789-00", [("CPF", "123.456.789-00")]),
        ("CPF 12345678900", []),
        ("CPF 11111111111", []),
        # CNPJ, numeric and alphanumeric, same rule
        ("CNPJ 11.222.333/0001-81", [("CNPJ", "11.222.333/0001-81")]),
        ("CNPJ 11222333000181", [("CNPJ", "11222333000181")]),
        ("CNPJ 12.ABC.345/01DE-35", [("CNPJ", "12.ABC.345/01DE-35")]),
        ("CNPJ 12ABC34501DE35", [("CNPJ", "12ABC34501DE35")]),
        ("CNPJ 11.222.333/0001-80", [("CNPJ", "11.222.333/0001-80")]),
        ("CNPJ 11222333000180", []),
        # RG has no national check digit: only after a keyword
        ("RG 12.345.678-9", [("RG", "12.345.678-9")]),
        ("R.G. nº 1234567890", [("RG", "1234567890")]),
        ("carteira de identidade 12.345.678-X", [("RG", "12.345.678-X")]),
        ("número 12.345.678-9", []),
        # Phone with area code, checked against the numbering plan; without it, after a keyword
        ("(51) 99999-8888", [("TELEFONE", "(51) 99999-8888")]),
        ("+55 51 99999-8888", [("TELEFONE", "+55 51 99999-8888")]),
        ("51 3333-4444", [("TELEFONE", "51 3333-4444")]),
        ("(11) 98765-4321", [("TELEFONE", "(11) 98765-4321")]),
        ("celular 99999-8888", [("TELEFONE", "99999-8888")]),
        ("fone: 3333-4444", [("TELEFONE", "3333-4444")]),
        ("protocolo 99999-8888", []),
        # CEP with its dash, or eight digits right after the keyword
        ("CEP 90010-150", [("CEP", "90010-150")]),
        ("CEP 90.010-150", [("CEP", "90.010-150")]),
        ("CEP 90010150", [("CEP", "90010150")]),
        ("código 90010150", []),
        ("maria.souza@exemplo.com.br", [("EMAIL", "maria.souza@exemplo.com.br")]),
    ],
)
def test_detection_matrix(guard: PiiGuard, text: str, expected: list[tuple[str, str]]) -> None:
    assert found(guard, text) == expected


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("O cliente João da Silva comprou um celular pela internet.", ["João da Silva"]),
        (
            "Lucas Martins e Beatriz Costa firmaram contrato de locação em 2024.",
            ["Lucas Martins", "Beatriz Costa"],
        ),
        # Capitals, as in a petition: the NER pass sees them in title case
        (
            "JOÃO PEREIRA SANTOS, brasileiro, solteiro, vem propor a presente ação.",
            ["JOÃO PEREIRA SANTOS"],
        ),
        # The model also tags "Dra" alone, which is dropped
        ("Segundo a Dra. Camila Rocha, o prazo vence na sexta-feira.", ["Camila Rocha"]),
    ],
)
def test_person_names(guard: PiiGuard, text: str, names: list[str]) -> None:
    assert found(guard, text) == [("PESSOA", n) for n in names]


def test_party_qualification_in_capitals(guard: PiiGuard) -> None:
    text = (
        "ANTÔNIO CARLOS FERREIRA, brasileiro, divorciado, aposentado, inscrito no CPF sob o "
        "nº 111.444.777-35."
    )
    assert found(guard, text) == [("PESSOA", "ANTÔNIO CARLOS FERREIRA"), ("CPF", "111.444.777-35")]


@pytest.mark.parametrize(
    "text",
    [
        "Vence em 15/11/2026.",
        "Vence em 15.11.2026.",
        "Vence em 2026-11-15.",
        "Assinado em 15 de novembro de 2026.",
        "Pagou R$ 1.500,00.",
        "A causa vale R$ 1.234.567,89.",
        "Custou US$ 20.",
        "Pediu R$ 10 mil de dano moral.",
        "Devolveu 1.500 reais.",
        "Pagou R$ 12345678909 ontem.",  # the digits of a valid CPF, written as an amount
    ],
)
def test_dates_and_amounts_are_not_pii(guard: PiiGuard, text: str) -> None:
    assert guard.detect(text) == []


# Pseudonymize and rehydrate -----------------------------------------------------------


def test_values_become_markers_and_the_mapping_brings_them_back(guard: PiiGuard) -> None:
    text = "Sou Maria Souza, CPF 123.456.789-09, e-mail maria.souza@exemplo.com.br."

    result = guard.pseudonymize(text)

    assert result.text == "Sou [PESSOA_1], CPF [CPF_1], e-mail [EMAIL_1]."
    assert result.mapping == {
        "[PESSOA_1]": "Maria Souza",
        "[CPF_1]": "123.456.789-09",
        "[EMAIL_1]": "maria.souza@exemplo.com.br",
    }
    assert result.counts == {"PESSOA": 1, "CPF": 1, "EMAIL": 1}
    assert rehydrate(result.text, result.mapping) == text


def test_same_value_gets_the_same_marker_whatever_its_punctuation(guard: PiiGuard) -> None:
    text = (
        "CPF 123.456.789-09 e CPF 111.444.777-35; repito, 12345678909. "
        "Fone (51) 99999-8888 ou +55 51 99999-8888."
    )

    result = guard.pseudonymize(text)

    assert result.text == (
        "CPF [CPF_1] e CPF [CPF_2]; repito, [CPF_1]. Fone [TELEFONE_1] ou [TELEFONE_1]."
    )
    assert result.counts == {"CPF": 3, "TELEFONE": 2}
    # The model's answer gets the first spelling back
    assert rehydrate("[CPF_1]", result.mapping) == "123.456.789-09"


def test_markers_stay_stable_across_a_conversation(guard: PiiGuard) -> None:
    first = guard.pseudonymize("Meu CPF é 123.456.789-09.")
    second = guard.pseudonymize(
        "Minha mãe tem CPF 111.444.777-35 e eu, 123.456.789-09.", first.mapping
    )

    assert second.text == "Minha mãe tem CPF [CPF_2] e eu, [CPF_1]."
    assert second.mapping == {"[CPF_1]": "123.456.789-09", "[CPF_2]": "111.444.777-35"}
    assert second.counts == {"CPF": 2}
    assert first.mapping == {"[CPF_1]": "123.456.789-09"}  # the caller's mapping is not changed


def test_a_name_differing_only_in_accents_keeps_its_marker(guard: PiiGuard) -> None:
    first = guard.pseudonymize("José da Silva assinou o contrato.")
    later = guard.pseudonymize("Jose da Silva assinou o contrato.", first.mapping)

    assert later.text == "[PESSOA_1] assinou o contrato."


def test_a_known_value_is_masked_where_the_ner_model_misses_it(guard: PiiGuard) -> None:
    text = "Ligue para a Patrícia amanhã para confirmar a audiência."
    assert guard.detect(text) == []  # the model alone misses this name here

    result = guard.pseudonymize(text, {"[PESSOA_1]": "Patrícia"})

    assert result.text == "Ligue para a [PESSOA_1] amanhã para confirmar a audiência."


def test_a_second_pass_leaves_markers_alone(guard: PiiGuard) -> None:
    first = guard.pseudonymize("Maria Souza, CPF 123.456.789-09.")
    again = guard.pseudonymize(first.text, first.mapping)

    assert again.text == first.text
    assert again.mapping == first.mapping
    assert again.counts == {}


MAPPING = {"[PESSOA_1]": "Maria Souza", "[CPF_1]": "123.456.789-09", "[CPF_10]": "111.444.777-35"}


@pytest.mark.parametrize(
    ("echo", "expected"),
    [
        ("O CPF de [PESSOA_1] é [CPF_1].", "O CPF de Maria Souza é 123.456.789-09."),
        ("**[PESSOA_1]**, CPF `[CPF_1]`", "**Maria Souza**, CPF `123.456.789-09`"),
        ("[ PESSOA_1 ] e [pessoa_1]", "Maria Souza e Maria Souza"),
        (r"\[PESSOA\_1\] e [PESSOA 1]", "Maria Souza e Maria Souza"),
        ("PESSOA_1 assinou; CPF_01.", "Maria Souza assinou; 123.456.789-09."),
        ("[CPF_10] não é [CPF_1].", "111.444.777-35 não é 123.456.789-09."),
        ("[CPF_2] e [EMAIL_1] não existem.", "[CPF_2] e [EMAIL_1] não existem."),
    ],
)
def test_rehydrate_survives_how_models_echo_markers(echo: str, expected: str) -> None:
    assert rehydrate(echo, MAPPING) == expected


def test_mapping_keys_must_be_markers() -> None:
    with pytest.raises(ValueError, match="markers"):
        rehydrate("texto", {"123.456.789-09": "[CPF_1]"})


FRAGMENTS = [
    "Maria Souza",
    "CPF 123.456.789-09",
    "CPF 111.444.777-35",
    "maria@exemplo.com",
    "(51) 99999-8888",
    "CEP 90010-150",
    "RG 12.345.678-9",
    "CNPJ 11.222.333/0001-81",
    "pediu a devolução",
    "em 10/11/2026",
    "de R$ 1.500,00",
    "pelo art. 49",
    "e",
    ",",
]


@settings(max_examples=40, deadline=None)
@given(st.lists(st.sampled_from(FRAGMENTS), min_size=1, max_size=8))
def test_rehydrate_undoes_pseudonymize(guard: PiiGuard, parts: list[str]) -> None:
    text = " ".join(parts)

    result = guard.pseudonymize(text)

    assert rehydrate(result.text, result.mapping) == text
    assert not any(value in result.text for value in result.mapping.values())


# Plugging in, overlaps and validation -------------------------------------------------


def test_a_plugged_recognizer_and_allow_pattern_take_part() -> None:
    protocol = PatternRecognizer(
        supported_entity="PROTOCOLO",
        supported_language="pt",
        patterns=[Pattern("protocolo", r"PROT-[0-9]{6}", 0.9)],
    )
    custom = build_pii_guard(
        extra_recognizers=[protocol], allow=[re.compile(r"Pedido nº [0-9.\-]+")]
    )

    result = custom.pseudonymize("PROT-123456: Pedido nº 123.456.789-09 do CPF 111.444.777-35.")

    assert result.text == "[PROTOCOLO_1]: Pedido nº 123.456.789-09 do CPF [CPF_1]."


def test_overlapping_detections_become_one_marker_over_both() -> None:
    suffix = PatternRecognizer(
        supported_entity="CODIGO",
        supported_language="pt",
        patterns=[Pattern("codigo", r"[0-9]{2}/[0-9]{2}", 0.7)],
    )
    custom = build_pii_guard(extra_recognizers=[suffix])

    result = custom.pseudonymize("Ver 123.456.789-09/77 no sistema.")

    # The CPF scores higher and names the marker; no digit of either is left in clear text
    assert result.text == "Ver [CPF_1] no sistema."
    assert result.mapping == {"[CPF_1]": "123.456.789-09/77"}


def test_a_plugged_recognizer_must_be_portuguese_with_an_upper_case_entity() -> None:
    english = PatternRecognizer(supported_entity="OAB", patterns=[Pattern("oab", "OAB", 0.9)])
    lower = PatternRecognizer(
        supported_entity="oab", supported_language="pt", patterns=[Pattern("oab", "OAB", 0.9)]
    )

    with pytest.raises(ValueError, match="'pt'"):
        build_pii_guard(extra_recognizers=[english])
    with pytest.raises(ValueError, match="upper-case"):
        build_pii_guard(extra_recognizers=[lower])


# Telemetry and logs -------------------------------------------------------------------


def test_span_counts_entities_and_never_carries_values(
    guard: PiiGuard, spans: InMemorySpanExporter
) -> None:
    spans.clear()

    result = guard.pseudonymize(
        "Maria Souza, CPF 123.456.789-09 e 111.444.777-35, fone (51) 99999-8888."
    )

    [span] = [s for s in spans.get_finished_spans() if s.name == "guardrail pii"]
    attributes = dict(span.attributes or {})
    assert attributes["gen_ai.operation.name"] == "guardrail"
    assert attributes["ementa.guardrail.stage"] == "input"
    assert attributes["ementa.guardrail.decision"] == "mask"
    assert attributes["ementa.pii.count.cpf"] == 2
    assert attributes["ementa.pii.count.pessoa"] == 1
    assert attributes["ementa.pii.count.telefone"] == 1
    assert attributes["ementa.pii.total"] == 4
    recorded = repr(attributes) + repr(span.events)
    assert not any(secret in recorded for secret in [*result.mapping.values(), *result.mapping])


def test_span_allows_text_without_pii(guard: PiiGuard, spans: InMemorySpanExporter) -> None:
    spans.clear()

    guard.pseudonymize("Pelo art. 49 do CDC, o prazo é de 7 dias.", stage="output")

    [span] = [s for s in spans.get_finished_spans() if s.name == "guardrail pii"]
    assert span.attributes is not None
    assert span.attributes["ementa.guardrail.decision"] == "allow"
    assert span.attributes["ementa.guardrail.stage"] == "output"
    assert span.attributes["ementa.pii.total"] == 0


def test_nothing_with_a_value_reaches_the_logs(
    guard: PiiGuard, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    values = ["Maria Souza", "123.456.789-09", "maria.souza@exemplo.com.br", "(51) 99999-8888"]

    guard.pseudonymize(f"{values[0]}, CPF {values[1]}, {values[2]}, telefone {values[3]}.")

    out, err = capsys.readouterr()
    logged = caplog.text + out + err
    assert not any(value in logged for value in values)
