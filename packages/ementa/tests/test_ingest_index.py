from ementa.ingest.corpus import load_statute
from ementa.ingest.index import statute_chunks


def test_one_chunk_per_article_without_the_vetoed() -> None:
    statute = load_statute("cdc")
    chunks = statute_chunks(statute)

    vetoed = {a.id for a in statute.articles if a.vetoed}
    assert len(chunks) == len(statute.articles) - len(vetoed) == 118
    assert vetoed.isdisjoint(c.id for c in chunks)


def test_chunk_lines_match_provision_ids() -> None:
    chunk = next(c for c in statute_chunks(load_statute("cdc")) if c.id == "cdc:art26")

    assert len(chunk.text.splitlines()) == len(chunk.provision_ids)
    assert "cdc:art26:p2:III" in chunk.provision_ids
    assert chunk.title == "Código de Defesa do Consumidor, art. 26"
    assert chunk.url.startswith("https://www.planalto.gov.br/")
