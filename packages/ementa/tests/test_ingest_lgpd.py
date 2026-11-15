"""Golden tests of the Planalto parser over the LGPD snapshot (Lei 13.709/2018, compilada)."""

import pytest

from ementa.ingest.corpus import load_statute
from ementa.ingest.index import statute_chunks
from ementa.ingest.planalto import Article


@pytest.fixture(scope="module")
def lgpd() -> dict[str, Article]:
    return {a.id: a for a in load_statute("lgpd").articles}


def provision(lgpd: dict[str, Article], pid: str) -> str:
    art_id = ":".join(pid.split(":")[:2])
    [match] = [p for p in lgpd[art_id].provisions if p.id == pid]
    return match.text


def test_all_articles_are_found(lgpd: dict[str, Article]) -> None:
    assert len(lgpd) == 80  # arts. 1 to 65 plus 55-A to 55-M, 58-A and 58-B
    assert list(lgpd)[:2] == ["lgpd:art1", "lgpd:art2"]
    assert list(lgpd)[-1] == "lgpd:art65"


def test_ids_are_unique(lgpd: dict[str, Article]) -> None:
    ids = [p.id for a in lgpd.values() for p in a.provisions]
    assert len(ids) == len(set(ids))


def test_art7_legal_bases(lgpd: dict[str, Article]) -> None:
    assert provision(lgpd, "lgpd:art7").startswith(
        "O tratamento de dados pessoais somente poderá ser realizado nas seguintes hipóteses"
    )
    assert (
        provision(lgpd, "lgpd:art7:I") == "mediante o fornecimento de consentimento pelo titular;"
    )
    assert (
        lgpd["lgpd:art7"].path[-1] == "Seção I Dos Requisitos para o Tratamento de Dados Pessoais"
    )


def test_art18_rights_of_the_data_subject(lgpd: dict[str, Article]) -> None:
    assert provision(lgpd, "lgpd:art18:II") == "acesso aos dados;"
    assert provision(lgpd, "lgpd:art18:p4:I").startswith("comunicar que não é agente de tratamento")
    assert lgpd["lgpd:art18"].path == ["Capítulo III DOS DIREITOS DO TITULAR"]


def test_revoked_paragraphs_keep_the_note_in_the_render(lgpd: dict[str, Article]) -> None:
    assert provision(lgpd, "lgpd:art7:p1") == ""
    assert "§ 1º (Revogado)" in lgpd["lgpd:art7"].render().splitlines()


def test_fully_revoked_article_stays_out_of_the_index(lgpd: dict[str, Article]) -> None:
    assert lgpd["lgpd:art55-B"].revoked
    assert lgpd["lgpd:art55-B"].render() == "Art. 55-B. (Revogado pela Lei nº 14.460, de 2022)"
    chunk_ids = {c.id for c in statute_chunks(load_statute("lgpd"))}
    assert "lgpd:art55-B" not in chunk_ids
    assert "lgpd:art57" not in chunk_ids  # vetoed
    assert "lgpd:art55-A" in chunk_ids  # caput in force, three paragraphs revoked


def test_number_split_by_a_space_is_one_article(lgpd: dict[str, Article]) -> None:
    # The page prints "Art. 5 7. (VETADO)."; it must not become a second art. 5.
    assert lgpd["lgpd:art57"].vetoed
    assert provision(lgpd, "lgpd:art5").startswith("Para os fins desta Lei, considera-se")


def test_lettered_incisos(lgpd: dict[str, Article]) -> None:
    assert provision(lgpd, "lgpd:art65:I-A").startswith("dia 1º de agosto de 2021")
    assert provision(lgpd, "lgpd:art55-C:V-A").startswith("Procuradoria")
    assert "I-A - dia 1º de agosto de 2021" in lgpd["lgpd:art65"].render()


def test_quoted_marco_civil_text_stays_in_art60(lgpd: dict[str, Article]) -> None:
    assert [p.id for p in lgpd["lgpd:art60"].provisions] == ["lgpd:art60"]
    assert "exclusão definitiva dos dados pessoais" in provision(lgpd, "lgpd:art60")
    assert provision(lgpd, "lgpd:art61").startswith("A empresa estrangeira será notificada")


def test_signatures_after_the_last_article_are_left_out(lgpd: dict[str, Article]) -> None:
    assert provision(lgpd, "lgpd:art65:II").endswith("quanto aos demais artigos.")
