"""Clauses, playbook rules and redline edits, without any model. The contracts are fictitious."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ementa.domain.contracts import (
    Clause,
    Edit,
    Playbook,
    PlaybookError,
    Severity,
    apply_edits,
    check_clause,
    fold,
    split_contract,
)

MARKDOWN = """# Contrato de teste

> Contrato fictício.

**CONTRATADA:** Empresa Exemplo Ltda.

## Cláusula 1ª - Do objeto

1.1 Serviço de teste.

## CLÁUSULA 2 - DO PREÇO

2.1 O serviço custa R$ 100,00.

### 2.2 Do reajuste

O valor sobe uma vez por ano.

## 3. Da vigência

3.1 Doze meses.

**Cláusula 4ª - Do foro.** Fica eleito o foro do domicílio do CONTRATANTE.

Porto Alegre, 15 de novembro de 2026.

______________________
CONTRATANTE
"""

PLAIN = """CONTRATO DE TESTE

CONTRATADA: Empresa Exemplo Ltda.

CLÁUSULA PRIMEIRA - DO OBJETO
1.1. Serviço de teste.

CLÁUSULA DÉCIMA PRIMEIRA: DA VIGÊNCIA
11.1. Doze meses.

12. DO FORO
12.1. Foro do domicílio do CONTRATANTE.
"""


def test_a_markdown_contract_splits_at_its_clause_headings() -> None:
    contract = split_contract(MARKDOWN)

    assert contract.title == "Contrato de teste"
    assert contract.preamble == "> Contrato fictício.\n\n**CONTRATADA:** Empresa Exemplo Ltda."
    assert [(c.number, c.title) for c in contract.clauses] == [
        ("1", "Do objeto"),
        ("2", "DO PREÇO"),
        ("3", "Da vigência"),
        ("4", "Do foro"),
    ]
    # a numbered sub-heading stays inside its clause; place, date and signatures are left out
    assert contract.clauses[1].text.splitlines()[2] == "### 2.2 Do reajuste"
    assert contract.clauses[3].text == "Fica eleito o foro do domicílio do CONTRATANTE."
    assert contract.clauses[0].label == "Cláusula 1 \u2013 Do objeto"


def test_a_plain_text_contract_splits_at_ordinals_and_numbered_capitals() -> None:
    contract = split_contract(PLAIN)

    assert contract.title == "CONTRATO DE TESTE"
    assert [(c.number, c.title) for c in contract.clauses] == [
        ("1", "DO OBJETO"),
        ("11", "DA VIGÊNCIA"),
        ("12", "DO FORO"),
    ]
    assert contract.clauses[2].text == "12.1. Foro do domicílio do CONTRATANTE."


def test_a_contract_without_headings_splits_at_its_paragraphs() -> None:
    contract = split_contract("Termos de uso\n\nPrimeiro parágrafo.\n\nSegundo parágrafo.\n")

    assert contract.title == "Termos de uso"
    assert [(c.number, c.text) for c in contract.clauses] == [
        ("1", "Primeiro parágrafo."),
        ("2", "Segundo parágrafo."),
    ]


def test_an_unnumbered_section_of_a_numbered_contract_goes_by_its_title() -> None:
    text = "# T\n\n## Cláusula 1 - Do objeto\n\nTexto.\n\n## Anexo I - Tabela\n\nPreços.\n"

    assert [c.number for c in split_contract(text).clauses] == ["1", "anexo-i-tabela"]


@given(st.text())
def test_folding_keeps_one_character_for_each(text: str) -> None:
    folded = fold(text)

    assert len(folded) == len(text)
    assert fold(folded) == folded


def test_folding_drops_accents_and_case() -> None:
    assert fold("Cláusula 5ª: AÇÃO, Carência") == "clausula 5a: acao, carencia"


def rule(**match: Any) -> dict[str, Any]:
    return {
        "id": "regra",
        "title": "Regra",
        "severity": "alta",
        "basis": ["cdc:art51:IV"],
        "why": "Motivo [cdc:art51:IV].",
        "redline": "Texto novo.",
        "match": match,
    }


def playbook(*rules: dict[str, Any], criteria: list[dict[str, Any]] | None = None) -> Playbook:
    data: dict[str, Any] = {
        "id": "teste",
        "version": 1,
        "title": "Teste",
        "scope": "Contratos de teste",
        "rules": list(rules),
    }
    if criteria is not None:
        data["criteria"] = criteria
    return Playbook.from_dict(data)


def clause(text: str, title: str = "") -> Clause:
    return Clause("1", title, text)


def test_a_rule_needs_all_of_one_of_and_none_of_its_patterns() -> None:
    [check] = playbook(rule(all=["foro"], any=["comarca de"], none=["domicilio do"])).rules

    assert check.check(clause("Fica eleito o foro da Comarca de Curitiba."))
    assert not check.check(clause("Fica eleito o foro do domicílio do CLIENTE."))
    assert not check.check(clause("Fica eleita a Comarca de Curitiba."))  # no "foro"
    assert check.check(clause("Comarca de Curitiba.", title="Do foro"))  # the title counts


def test_a_rule_points_at_the_sentences_where_it_matched() -> None:
    [check] = playbook(rule(any=["nao se responsabiliza"])).rules
    body = "6.1 O acesso é livre.\n6.2 A ACADEMIA não se responsabiliza por objetos."

    hit = check.check(clause(body))

    assert hit is not None
    assert hit.sentences == ("6.2 A ACADEMIA não se responsabiliza por objetos.",)


@pytest.mark.parametrize(
    ("sentence", "over"),
    [
        ("Em caso de atraso, multa de 10% (dez por cento).", True),
        ("Em caso de atraso, multa de 2% e juros de 5% ao mês.", False),  # 2% is next to multa
        ("Em caso de atraso, juros de 1% ao mês e multa de 10%.", True),
        ("Em caso de cancelamento, multa de 30%.", False),  # no atraso in the sentence
        ("Multa de 2,5% sobre a parcela vencida.", True),
    ],
)
def test_a_percentage_counts_next_to_its_anchor_in_one_sentence(sentence: str, over: bool) -> None:
    above = {"anchor": "multa", "unit": "percent", "limit": 2, "context": "atras|vencid"}
    [check] = playbook(rule(above=above)).rules

    assert bool(check.check(clause(sentence))) is over


@pytest.mark.parametrize(
    ("text", "over"),
    [
        ("Carência de 24 (vinte e quatro) horas para urgência e emergência.", False),
        ("Carência de 180 (cento e oitenta) dias para consultas, urgência e emergência.", True),
        ("Carência de 24 horas para urgência e de 180 dias para internações.", False),
        ("Carência de 2 meses para emergências.", True),
        ("Carência de 48h para urgência.", True),
    ],
)
def test_a_duration_counts_in_hours_next_to_its_anchor(text: str, over: bool) -> None:
    above = {"anchor": "urgencia|emergencia", "unit": "hours", "limit": 24}
    [check] = playbook(rule(all=["carencia"], above=above)).rules

    assert bool(check.check(clause(text))) is over


def test_check_clause_returns_the_hits_in_playbook_order() -> None:
    first = rule(any=["multa"]) | {"id": "primeira"}
    second = rule(any=["atraso"]) | {"id": "segunda"}
    rules = playbook(second, first).rules

    hits = check_clause(clause("Multa por atraso."), rules)

    assert [h.rule for h in hits] == ["segunda", "primeira"]


def test_an_edit_rewrites_a_sentence_and_keeps_its_number() -> None:
    body = "5.1 Primeira frase.\n5.2 Em caso de atraso, multa de 10%."

    redraft = apply_edits(body, [Edit("5.2 Em caso de atraso, multa de 10%.", "Multa de 2%.")])

    assert redraft.text == "5.1 Primeira frase.\n5.2 Multa de 2%."
    assert [(c.before, c.after) for c in redraft.changes] == [
        ("5.2 Em caso de atraso, multa de 10%.", "5.2 Multa de 2%.")
    ]


def test_an_empty_edit_removes_the_sentence_and_its_number_moves_on() -> None:
    body = "3.1 Frase que sai. Frase que fica.\n3.2 Outra."

    redraft = apply_edits(body, [Edit("3.1 Frase que sai.", "")])

    assert redraft.text == "3.1 Frase que fica.\n3.2 Outra."
    assert redraft.changes[0].after == ""


def test_edits_on_one_sentence_join_and_edits_elsewhere_are_added() -> None:
    body = "1.1 Texto antigo."
    edits = [
        Edit("1.1 Texto antigo.", "Primeira."),
        Edit("1.1 Texto antigo.", "Segunda."),
        Edit("1.1 Texto antigo.", "Primeira."),
        Edit(None, "Acréscimo."),
        Edit("frase que não existe", "Outro acréscimo."),
    ]

    redraft = apply_edits(body, edits)

    assert redraft.text == "1.1 Primeira. Segunda.\n\nAcréscimo. Outro acréscimo."
    assert [c.before for c in redraft.changes] == ["1.1 Texto antigo.", "", ""]


def test_severity_sorts_the_most_serious_first() -> None:
    assert sorted(Severity, key=lambda s: s.rank) == [Severity.HIGH, Severity.MEDIUM, Severity.LOW]
    assert Severity.MEDIUM.label == "média"


def test_a_playbook_has_rules_and_criteria() -> None:
    criterion = {
        "id": "criterio",
        "title": "Critério",
        "severity": "media",
        "basis": ["cdc:art46"],
        "why": "Motivo [cdc:art46].",
        "description": "O que o modelo procura.",
        "redline": "Redação do escritório.",
    }
    book = playbook(rule(any=["x"]), criteria=[criterion])

    assert book.label == "teste@1"
    assert book.item("criterio").severity is Severity.MEDIUM
    assert book.basis_ids() == {"cdc:art51:IV", "cdc:art46"}
    with pytest.raises(KeyError):
        book.item("outra")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"severity": "grave"}, "severity must be one of"),
        ({"basis": []}, "basis must list"),
        ({"basis": ["CDC art. 51"]}, "is not a source id"),
        ({"id": "Regra Um"}, "id must be lower-case"),
        ({"match": {"any": ["("]}}, "bad pattern"),
        ({"match": {"none": ["x"]}}, "needs all, any or above"),
        ({"match": {"some": ["x"]}}, "unknown match keys"),
        ({"match": {"above": {"anchor": "multa", "unit": "reais", "limit": 2}}}, "above.unit"),
        ({"why": ""}, "why must be a non-empty string"),
    ],
)
def test_a_malformed_rule_says_what_is_wrong(change: dict[str, Any], message: str) -> None:
    with pytest.raises(PlaybookError, match=message):
        playbook(rule(any=["x"]) | change)


def test_repeated_ids_are_refused() -> None:
    with pytest.raises(PlaybookError, match="repeated ids"):
        playbook(rule(any=["x"]), rule(any=["y"]))
