from collections.abc import Sequence

from ementa.ingest.corpus import load_statute
from ementa.ingest.index import EMBED_BATCH, embed_passages, passage, statute_chunks


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


def test_passage_has_title_path_and_text() -> None:
    article = next(c for c in statute_chunks(load_statute("cdc")) if c.id == "cdc:art49")
    sumula = next(c for c in statute_chunks(load_statute("stf")) if c.id == "stf:sv:25")

    assert passage(article).startswith("Código de Defesa do Consumidor, art. 49. Título I")
    # No path for a súmula vinculante: no empty segment between the title and the text.
    assert passage(sumula).startswith(
        "Súmula Vinculante 25 do STF. Súmula Vinculante 25: É ilícita"
    )


class LengthEmbedder:
    """Embeds a text as [its length] and records the size of each batch it was given."""

    model_name = "length"
    dim = 1

    def __init__(self) -> None:
        self.batches: list[int] = []

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_passages(texts)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [[float(len(t))] for t in texts]


def test_embed_passages_batches_by_length_and_keeps_the_order() -> None:
    passages = [("x" * n) for n in (50, 3, 700, 12, 3, 90) * 6]
    embedder = LengthEmbedder()

    vectors = embed_passages(embedder, passages)

    assert vectors == [[float(len(p))] for p in passages]
    assert max(embedder.batches) <= EMBED_BATCH
    assert sum(embedder.batches) == len(passages)
