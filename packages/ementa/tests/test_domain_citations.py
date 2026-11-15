from ementa.domain.citations import extract_ids, split_sentences, verify_citations

ALLOWED = frozenset({"cdc:art49", "cdc:art49:pu", "cdc:art26", "cdc:art26:II"})


def test_extracts_bracketed_ids() -> None:
    assert extract_ids("Veja [cdc:art49] e [cdc:art26:II].") == ["cdc:art49", "cdc:art26:II"]


def test_sentence_split_keeps_article_abbreviations() -> None:
    text = "O art. 49 do CDC fixa 7 dias [cdc:art49]. O prazo conta do recebimento."
    assert split_sentences(text) == [
        "O art. 49 do CDC fixa 7 dias [cdc:art49].",
        "O prazo conta do recebimento.",
    ]


def test_valid_citations_stay() -> None:
    answer = "Você tem 7 dias para desistir, pelo art. 49 do CDC [cdc:art49]."
    result = verify_citations(answer, ALLOWED)

    assert result.text == answer
    assert result.valid == ["cdc:art49"]
    assert result.removed == []


def test_citation_outside_the_context_is_removed_and_reported() -> None:
    answer = (
        "Você tem 7 dias para desistir [cdc:art49]. "
        "A Súmula 999 do STJ garante o dobro [stj:sumula:999]."
    )
    result = verify_citations(answer, ALLOWED)

    assert result.text == "Você tem 7 dias para desistir [cdc:art49]."
    assert [r.reference for r in result.removed] == ["stj:sumula:999"]
    assert result.removed[0].reason == "fonte fora do contexto recuperado"


def test_prose_reference_without_an_id_is_removed() -> None:
    answer = "Você tem 7 dias [cdc:art49]. A Súmula 385 do STJ também se aplica."
    result = verify_citations(answer, ALLOWED)

    assert result.text == "Você tem 7 dias [cdc:art49]."
    assert result.removed[0].reference == "Súmula 385"
    assert result.removed[0].reason == "menção sem id de fonte verificado"


def test_prose_article_backed_by_its_id_stays() -> None:
    answer = "O art. 26, inciso II, dá noventa dias para bens duráveis [cdc:art26:II]."
    assert verify_citations(answer, ALLOWED).removed == []


def test_prose_article_backed_by_another_article_is_removed() -> None:
    answer = "O art. 50 complementa a garantia [cdc:art49]."
    result = verify_citations(answer, ALLOWED)

    assert result.text == ""
    assert result.removed[0].reference == "art. 50"


def test_answer_without_citations_is_flagged() -> None:
    result = verify_citations("Procure um advogado.", ALLOWED)

    assert result.valid == []
    assert not result.has_citations


def test_line_breaks_are_preserved() -> None:
    answer = "Resposta curta [cdc:art49].\n\nDetalhe [cdc:art49:pu]."
    assert verify_citations(answer, ALLOWED).text == answer
