"""Legal side of the PII guardrail. Every name, CPF and OAB number here is fictitious."""

import pytest

from ementa.pii import pii_guard
from ementa_kit.guardrails.pii import PiiGuard, rehydrate


@pytest.fixture(scope="module")
def guard() -> PiiGuard:
    return pii_guard()


def found(guard: PiiGuard, text: str) -> list[tuple[str, str]]:
    return [(d.entity, text[d.start : d.end]) for d in guard.detect(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("OAB/RS 123.456", "OAB/RS 123.456"),
        ("OAB/SP nº 98.765", "OAB/SP nº 98.765"),
        ("OAB-RJ 123456", "OAB-RJ 123456"),
        ("OAB nº 12.345/RS", "OAB nº 12.345/RS"),
        ("OAB/RJ 1.234-E", "OAB/RJ 1.234-E"),
        ("inscrito na OAB/RS sob o nº 54.321", "OAB/RS sob o nº 54.321"),
    ],
)
def test_oab_registration_is_one_entity(guard: PiiGuard, text: str, expected: str) -> None:
    assert found(guard, text) == [("OAB", expected)]


@pytest.mark.parametrize(
    "text", ["a OAB recomenda cautela", "OAB/RS", "OAB se manifestou sobre 12.345 casos"]
)
def test_oab_without_a_registration_number_is_not_pii(guard: PiiGuard, text: str) -> None:
    assert guard.detect(text) == []


@pytest.mark.parametrize(
    "text",
    [
        # CNJ case numbers, with and without punctuation
        "Processo 0001234-56.2024.8.21.0001",
        "processo 00012345620248210001",
        "5001234-12.2025.4.04.7100",
        # Articles and paragraphs
        "art. 49",
        "Art. 6º, inciso III",
        "artigo 42, parágrafo único",
        "art. 1.022 do CPC",
        "§ 2º do art. 26",
        # Statutes
        "Lei 8.078/1990",
        "Lei nº 13.709/2018",
        "Lei n. 8.078, de 11 de setembro de 1990",
        "Decreto 7.962/2013",
        "Lei Complementar 123/2006",
        "Decreto-Lei 4.657/1942",
        "Resolução CNJ 615/2025",
        "Lei Maria da Penha",
        "LEI MARIA DA PENHA",
        # Súmulas and source ids
        "Súmula 385 do STJ",
        "Súmula Vinculante 13",
        "cdc:art49",
        "[cdc:art42:pu]",
        "stj:sumula:385",
        "cpc:art219",
        # Court panels and procedural roles that the NER model takes for names
        "A Terceira Turma do STJ decidiu.",
        "Autor e Réu chegaram a um acordo na audiência.",
        "O Relator votou pelo provimento.",
    ],
)
def test_legal_references_are_not_pii(guard: PiiGuard, text: str) -> None:
    assert guard.detect(text) == []


def test_only_the_personal_data_of_a_legal_question_is_masked(guard: PiiGuard) -> None:
    text = (
        "O cliente João da Silva, CPF 123.456.789-09, processo 0001234-56.2024.8.21.0001, pede "
        "com base no art. 49 da Lei 8.078/1990 [cdc:art49] e na Súmula 385 do STJ "
        "[stj:sumula:385] a devolução de R$ 1.500,00 pagos em 10/11/2026."
    )

    result = guard.pseudonymize(text)

    assert result.text == (
        "O cliente [PESSOA_1], CPF [CPF_1], processo 0001234-56.2024.8.21.0001, pede "
        "com base no art. 49 da Lei 8.078/1990 [cdc:art49] e na Súmula 385 do STJ "
        "[stj:sumula:385] a devolução de R$ 1.500,00 pagos em 10/11/2026."
    )
    assert rehydrate(result.text, result.mapping) == text


def test_the_lawyer_becomes_markers_too(guard: PiiGuard) -> None:
    result = guard.pseudonymize("A advogada Fernanda Lima, OAB/RS 123.456, representa a autora.")

    assert result.text == "A advogada [PESSOA_1], [OAB_1], representa a autora."
    assert result.mapping["[OAB_1]"] == "OAB/RS 123.456"


@pytest.mark.parametrize(
    ("text", "cpf"),
    [
        ("Lei 8.078/1990, CPF 123.456.789-09", "123.456.789-09"),
        ("art. 12345678909", "12345678909"),
        ("cpf:12345678909", "12345678909"),
    ],
)
def test_a_cpf_next_to_a_legal_reference_is_still_masked(
    guard: PiiGuard, text: str, cpf: str
) -> None:
    assert found(guard, text) == [("CPF", cpf)]


def test_the_guard_is_built_once() -> None:
    assert pii_guard() is pii_guard()
