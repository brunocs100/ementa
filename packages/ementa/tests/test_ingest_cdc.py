"""Golden tests of the CDC parser over the versioned snapshot."""

from pathlib import Path

import pytest

from ementa.ingest.corpus import SnapshotError, load_statute
from ementa.ingest.planalto import Article


@pytest.fixture(scope="module")
def cdc() -> dict[str, Article]:
    return {a.id: a for a in load_statute("cdc").articles}


def provision(cdc: dict[str, Article], pid: str) -> str:
    art_id = ":".join(pid.split(":")[:2])
    [match] = [p for p in cdc[art_id].provisions if p.id == pid]
    return match.text


def test_all_articles_are_found(cdc: dict[str, Article]) -> None:
    assert len(cdc) == 130  # arts. 1 to 119 plus 42-A, 54-A to 54-G and 104-A to 104-C
    assert list(cdc)[:3] == ["cdc:art1", "cdc:art2", "cdc:art3"]
    assert list(cdc)[-1] == "cdc:art119"


def test_ids_are_unique(cdc: dict[str, Article]) -> None:
    ids = [p.id for a in cdc.values() for p in a.provisions]
    assert len(ids) == len(set(ids))


def test_art49_right_of_withdrawal(cdc: dict[str, Article]) -> None:
    assert provision(cdc, "cdc:art49").startswith(
        "O consumidor pode desistir do contrato, no prazo de 7 dias"
    )
    assert "serão devolvidos, de imediato" in provision(cdc, "cdc:art49:pu")
    assert cdc["cdc:art49"].path[-1] == "Seção I Disposições Gerais"


def test_art42_sole_paragraph_double_refund(cdc: dict[str, Article]) -> None:
    assert "dobro do que pagou em excesso" in provision(cdc, "cdc:art42:pu")


def test_incisos_hang_from_the_right_parent(cdc: dict[str, Article]) -> None:
    assert provision(cdc, "cdc:art26:II").startswith("noventa dias")
    assert provision(cdc, "cdc:art26:p2:III").startswith("a instauração de inquérito civil")
    assert provision(cdc, "cdc:art4:II:a") == "por iniciativa direta;"


def test_lettered_articles_and_their_chapter(cdc: dict[str, Article]) -> None:
    assert "superendividamento" in provision(cdc, "cdc:art54-A")
    assert cdc["cdc:art54-A"].path[-1].startswith("Capítulo VI-A")


def test_amendment_notes_leave_the_text(cdc: dict[str, Article]) -> None:
    [inciso] = [p for p in cdc["cdc:art6"].provisions if p.id == "cdc:art6:III"]
    assert inciso.text.endswith("bem como sobre os riscos que apresentem;")
    assert inciso.notes == ["(Redação dada pela Lei nº 12.741, de 2012)"]


def test_vetoed_provisions_are_flagged(cdc: dict[str, Article]) -> None:
    assert cdc["cdc:art11"].vetoed
    assert not cdc["cdc:art49"].vetoed


def test_quoted_text_of_another_law_stays_in_the_amending_article(
    cdc: dict[str, Article],
) -> None:
    assert [p.id for p in cdc["cdc:art113"].provisions] == ["cdc:art113"]
    assert "litisconsórcio facultativo" in provision(cdc, "cdc:art113")


def test_render_uses_the_statute_style(cdc: dict[str, Article]) -> None:
    lines = cdc["cdc:art49"].render().splitlines()
    assert lines[0].startswith("Art. 49. O consumidor")
    assert lines[1].startswith("Parágrafo único. Se o consumidor")
    assert cdc["cdc:art6"].render().startswith("Art. 6º São direitos")


def test_hash_mismatch_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "raw" / "cdc"
    folder.mkdir(parents=True)
    real = load_statute("cdc").source
    (folder / "source.json").write_text(
        f'{{"id": "cdc", "title": "", "statute": "", "url": "", "file": "x.htm", '
        f'"encoding": "cp1252", "retrieved_at": "", "sha256": "{real.sha256}"}}',
        encoding="utf-8",
    )
    (folder / "x.htm").write_bytes(b"<p>Art. 1 tampered</p>")

    with pytest.raises(SnapshotError, match="sha256"):
        load_statute("cdc", corpus_dir=tmp_path)
