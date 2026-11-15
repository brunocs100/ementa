"""Golden tests of the Planalto parser over the CPC snapshot (Lei 13.105/2015, compilada)."""

import pytest

from ementa.ingest.corpus import load_statute
from ementa.ingest.index import statute_chunks
from ementa.ingest.planalto import Article

DEADLINES = [
    "Parte Geral",
    "Livro IV DOS ATOS PROCESSUAIS",
    "Título I DA FORMA, DO TEMPO E DO LUGAR DOS ATOS PROCESSUAIS",
    "Capítulo III DOS PRAZOS",
    "Seção I Disposições Gerais",
]


@pytest.fixture(scope="module")
def cpc() -> dict[str, Article]:
    return {a.id: a for a in load_statute("cpc").articles}


def provision(cpc: dict[str, Article], pid: str) -> str:
    art_id = ":".join(pid.split(":")[:2])
    [match] = [p for p in cpc[art_id].provisions if p.id == pid]
    return match.text


def test_all_articles_are_found(cpc: dict[str, Article]) -> None:
    assert len(cpc) == 1074  # arts. 1 to 1.072 plus 699-A and 1.035-A
    assert next(iter(cpc)) == "cpc:art1"
    assert list(cpc)[-1] == "cpc:art1072"
    assert {"cpc:art699-A", "cpc:art1035-A"} <= cpc.keys()


def test_ids_are_unique(cpc: dict[str, Article]) -> None:
    ids = [p.id for a in cpc.values() for p in a.provisions]
    assert len(ids) == len(set(ids))


def test_deadline_articles(cpc: dict[str, Article]) -> None:
    assert provision(cpc, "cpc:art219").endswith("computar-se-ão somente os dias úteis.")
    assert provision(cpc, "cpc:art219:pu") == (
        "O disposto neste artigo aplica-se somente aos prazos processuais."
    )
    assert "entre 20 de dezembro e 20 de janeiro, inclusive" in provision(cpc, "cpc:art220")
    assert "excluindo o dia do começo e incluindo o dia do vencimento" in provision(
        cpc, "cpc:art224"
    )
    assert provision(cpc, "cpc:art231:I").startswith("a data de juntada aos autos do aviso")
    for art in ("cpc:art212", "cpc:art219", "cpc:art224", "cpc:art232"):
        assert cpc[art].path[:3] == DEADLINES[:3]
    assert cpc["cpc:art219"].path == DEADLINES


def test_thousands_dot_leaves_the_id_and_stays_in_the_text(cpc: dict[str, Article]) -> None:
    assert cpc["cpc:art1003"].number == "1.003"
    assert cpc["cpc:art1003"].render().startswith("Art. 1.003. O prazo para interposição")
    assert "15 (quinze) dias" in provision(cpc, "cpc:art1003:p5")
    assert cpc["cpc:art1035-A"].render().startswith("Art. 1.035-A. O Superior Tribunal")


def test_part_book_and_single_title_headings(cpc: dict[str, Article]) -> None:
    assert cpc["cpc:art1"].path[:3] == [
        "Parte Geral",
        "Livro I DAS NORMAS PROCESSUAIS CIVIS",
        "Título Único DAS NORMAS FUNDAMENTAIS E DA APLICAÇÃO DAS NORMAS PROCESSUAIS",
    ]
    assert cpc["cpc:art335"].path[:2] == [
        "Parte Especial",
        "Livro I DO PROCESSO DE CONHECIMENTO E DO CUMPRIMENTO DE SENTENÇA",
    ]
    assert cpc["cpc:art1072"].path == [
        "Parte Especial",
        "Livro Complementar DISPOSIÇÕES FINAIS E TRANSITÓRIAS",
    ]


def test_lettered_paragraphs_and_paragraph_labels(cpc: dict[str, Article]) -> None:
    assert provision(cpc, "cpc:art246:p1-A").startswith("A ausência de confirmação")
    assert provision(cpc, "cpc:art246:p1-A:I")
    assert provision(cpc, "cpc:art784:XI-A").startswith("o contrato de contragarantia")
    lines = cpc["cpc:art85"].render().splitlines()
    assert any(line.startswith("§ 6º-A Quando o valor da condenação") for line in lines)
    assert any(line.startswith("§ 10. Nos casos de perda do objeto") for line in lines)


def test_revoked_and_vetoed_articles_stay_out_of_the_index(cpc: dict[str, Article]) -> None:
    assert cpc["cpc:art945"].revoked
    assert cpc["cpc:art945"].render() == "Art. 945. (Revogado pela Lei nº 13.256, de 2016)"
    assert cpc["cpc:art35"].vetoed
    chunks = statute_chunks(load_statute("cpc"))
    assert len(chunks) == 1074 - 3 - 1  # three vetoed and one revoked
    assert {"cpc:art945", "cpc:art35"}.isdisjoint(c.id for c in chunks)


def test_quoted_text_of_another_law_stays_in_the_amending_article(
    cpc: dict[str, Article],
) -> None:
    assert [p.id for p in cpc["cpc:art1071"].provisions] == ["cpc:art1071"]
    assert "Art. 216-A." in provision(cpc, "cpc:art1071")
    assert provision(cpc, "cpc:art1072").startswith("Revogam-se")
