"""Golden tests of the súmula parsers over the STJ (SCON) and STF snapshots."""

import shutil
from pathlib import Path

import pytest

from ementa.ingest.corpus import DEFAULT_CORPUS_DIR, SnapshotError, load_statute
from ementa.ingest.index import statute_chunks
from ementa.ingest.planalto import Article
from ementa.ingest.sumulas import SumulaParseError, parse_stj_sumulas

CONSUMER = "DIREITO DO CONSUMIDOR"
BANKING = "DIREITO BANCÁRIO"


@pytest.fixture(scope="module")
def stj() -> dict[str, Article]:
    return {s.id: s for s in load_statute("stj").articles}


@pytest.fixture(scope="module")
def stf() -> dict[str, Article]:
    return {s.id: s for s in load_statute("stf").articles}


def text(units: dict[str, Article], sid: str) -> str:
    [provision] = units[sid].provisions
    assert provision.id == sid and provision.kind == "sumula"
    return provision.text


def test_stj_consumer_and_banking_sumulas_in_force(stj: dict[str, Article]) -> None:
    assert len(stj) == 72  # 73 on the SCON page; 357 says SÚMULA REVOGADA
    assert next(iter(stj)) == "stj:sumula:16"
    assert list(stj)[-1] == "stj:sumula:675"
    assert "stj:sumula:357" not in stj
    assert "stj:sumula:469" not in stj  # cancelled: SCON's filter leaves it out
    assert {s.path[0] for s in stj.values()} == {CONSUMER, BANKING}


def test_stj_text_branch_topic_and_reference(stj: dict[str, Article]) -> None:
    assert text(stj, "stj:sumula:385") == (
        "Da anotação irregular em cadastro de proteção ao crédito, não cabe indenização por dano "
        "moral, quando preexistente legítima inscrição, ressalvado o direito ao cancelamento."
    )
    sumula = stj["stj:sumula:385"]
    assert sumula.path == [CONSUMER, "INSCRIÇÃO EM CADASTRO DE INADIMPLENTES"]
    assert sumula.provisions[0].notes == [
        "(SÚMULA 385, SEGUNDA SEÇÃO, julgado em 27/05/2009, DJe 08/06/2009)"
    ]
    assert sumula.url.endswith("toc.jsp?livre=%27385%27.num.&O=JT")
    assert sumula.render().startswith("Súmula 385 do STJ: Da anotação irregular")


def test_stj_banking_sumula(stj: dict[str, Article]) -> None:
    assert text(stj, "stj:sumula:297") == (
        "O Código de Defesa do Consumidor é aplicável às instituições financeiras."
    )
    assert stj["stj:sumula:297"].path == [BANKING, "CONTRATO BANCÁRIO"]


def test_stj_entry_without_the_verbete_span(stj: dict[str, Article]) -> None:
    assert text(stj, "stj:sumula:675").startswith("É legítima a atuação dos órgãos de defesa")
    assert text(stj, "stj:sumula:675").endswith("quando a atividade é regulada.")
    assert stj["stj:sumula:675"].provisions[0].notes == [
        "(PRIMEIRA SEÇÃO, julgado em 13/11/2024, DJe de 25/11/2024)"
    ]


def test_stj_block_without_reference_is_refused() -> None:
    html = (
        '<div class="gridSumula"><span class="numeroSumula">1</span>'
        '<div class="blocoVerbete"><span class="ramoSumula">DIREITO CIVIL - X</span>'
        "Texto sem referência.</div></div>"
    )
    with pytest.raises(SumulaParseError, match="reference"):
        parse_stj_sumulas(html)


def test_stf_binding_sumulas_without_cancelled_or_unpublished(stf: dict[str, Article]) -> None:
    assert len(stf) == 61  # 63 pages; 9 is cancelled and 30 is pending publication
    assert list(stf)[:2] == ["stf:sv:1", "stf:sv:2"]
    assert list(stf)[-1] == "stf:sv:63"
    assert {"stf:sv:9", "stf:sv:30"}.isdisjoint(stf)


def test_stf_text_publication_note_and_url(stf: dict[str, Article]) -> None:
    assert text(stf, "stf:sv:27") == (
        "Compete à Justiça Estadual julgar causas entre consumidor e concessionária de serviço "
        "público de telefonia, quando a ANATEL não seja litisconsorte passiva necessária, "
        "assistente, nem opoente."
    )
    sumula = stf["stf:sv:27"]
    assert sumula.provisions[0].notes == ["Data de publicação do enunciado: DJE de 11-12-2009"]
    assert sumula.url.endswith("sumariosumulas.asp?base=26&sumula=1274")
    assert sumula.title == "Súmula Vinculante 27 do STF"
    assert text(stf, "stf:sv:13").endswith("viola a Constituição Federal.")


def test_sumula_chunks_carry_their_own_title_and_url(stj: dict[str, Article]) -> None:
    chunks = {c.id: c for c in statute_chunks(load_statute("stj"))}
    chunk = chunks["stj:sumula:479"]
    assert chunk.title == "Súmula 479 do STJ"
    assert chunk.provision_ids == ["stj:sumula:479"]
    assert chunk.url == stj["stj:sumula:479"].url
    assert len(chunk.text.splitlines()) == 1


def test_tampered_page_of_a_multi_file_source_is_refused(tmp_path: Path) -> None:
    shutil.copytree(DEFAULT_CORPUS_DIR / "raw" / "stf", tmp_path / "raw" / "stf")
    page = tmp_path / "raw" / "stf" / "sv13.htm"
    page.write_bytes(page.read_bytes().replace(b"Federal", b"Estadual", 1))

    with pytest.raises(SnapshotError, match=r"stf/sv13\.htm: sha256"):
        load_statute("stf", corpus_dir=tmp_path)
