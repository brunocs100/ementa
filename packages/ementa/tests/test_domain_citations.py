from functools import cache

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ementa.domain.citations import (
    citation_label,
    extract_ids,
    label_citations,
    split_sentences,
    verify_citations,
)
from ementa.ingest.corpus import SOURCE_IDS, load_statute
from ementa.ingest.index import statute_chunks

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


MORE = frozenset({"cpc:art1003", "cpc:art1003:p5", "lgpd:art65:I-A", "stj:sumula:385", "stf:sv:25"})


def test_prose_article_with_thousands_dot_backed_by_its_id_stays() -> None:
    answer = "O art. 1.003, § 5º, do CPC fixa 15 dias para recorrer [cpc:art1003:p5]."
    result = verify_citations(answer, MORE)

    assert result.removed == []
    assert result.valid == ["cpc:art1003:p5"]


def test_lettered_inciso_id_is_extracted() -> None:
    assert extract_ids("Vigência em 2021 [lgpd:art65:I-A].") == ["lgpd:art65:I-A"]


def test_prose_sumulas_need_the_id_of_their_own_kind() -> None:
    kept = "A Súmula 385 do STJ afasta o dano moral [stj:sumula:385]. "
    kept += "A Súmula Vinculante 25 veda a prisão civil do depositário infiel [stf:sv:25]."
    assert verify_citations(kept, MORE).removed == []

    wrong = "A Súmula Vinculante 385 afasta o dano moral [stj:sumula:385]."
    result = verify_citations(wrong, MORE)
    assert result.text == ""
    assert result.removed[0].reference == "Súmula Vinculante 385"


def test_without_the_prose_check_only_bracketed_ids_count() -> None:
    answer = (
        "O prazo vence em 26/01/2027. Só dias úteis contam (CPC, art. 219). "
        "A Súmula 999 do STJ dobra o prazo [stj:sumula:999]."
    )

    result = verify_citations(answer, ALLOWED, check_prose=False)

    assert result.text == "O prazo vence em 26/01/2027. Só dias úteis contam (CPC, art. 219)."
    assert [r.reference for r in result.removed] == ["stj:sumula:999"]


@cache
def corpus_ids() -> list[str]:
    return [
        i
        for source in SOURCE_IDS
        for chunk in statute_chunks(load_statute(source))
        for i in (chunk.id, *chunk.provision_ids)
    ]


@pytest.mark.parametrize(
    ("source_id", "label"),
    [
        ("cdc:art49", "art. 49 do CDC"),
        ("cdc:art6:VIII", "art. 6º, VIII, do CDC"),
        ("cdc:art42:pu", "art. 42, parágrafo único, do CDC"),
        ("cdc:art12:p1:I", "art. 12, § 1º, I, do CDC"),
        ("cdc:art54-A:p1", "art. 54-A, § 1º, do CDC"),
        ("cpc:art22:I:a", "art. 22, I, a, do CPC"),
        ("cpc:art85:p6-A", "art. 85, § 6º-A, do CPC"),
        ("cpc:art1035-A:p1", "art. 1.035-A, § 1º, do CPC"),
        ("cpc:art784:XI-A", "art. 784, XI-A, do CPC"),
        ("lgpd:art7", "art. 7º da LGPD"),
        ("stj:sumula:385", "Súmula 385 do STJ"),
        ("stf:sv:13", "Súmula Vinculante 13 do STF"),
        ("cc:art186", "cc:art186"),  # a law outside the corpus keeps its id
        ("cdc:art49:x1", "cdc:art49:x1"),
    ],
)
def test_a_source_id_is_written_as_a_brief_cites_it(source_id: str, label: str) -> None:
    assert citation_label(source_id) == label


def test_every_id_of_the_corpus_has_a_label() -> None:
    assert [i for i in corpus_ids() if citation_label(i) == i] == []


def test_ids_cited_side_by_side_share_one_parenthesis() -> None:
    text = "Cabe a inversão [cdc:art6:VIII] [cdc:art14], [cdc:art14]. Não [stj:sumula:385]."

    assert label_citations(text) == (
        "Cabe a inversão (art. 6º, VIII, do CDC; art. 14 do CDC). Não (Súmula 385 do STJ)."
    )


@given(st.lists(st.sampled_from(corpus_ids()), min_size=1, max_size=4), st.sampled_from(" ,;"))
def test_no_id_is_left_in_brackets(ids: list[str], separator: str) -> None:
    text = "Frase " + separator.join(f"[{i}]" for i in ids) + "."

    labelled = label_citations(text)

    assert extract_ids(labelled) == []
    assert labelled.count("(") == 1
