"""Load a corpus snapshot into the knowledge base: one chunk per article or súmula."""

from collections.abc import Sequence
from pathlib import Path

from ementa.ingest.corpus import DEFAULT_CORPUS_DIR, Statute, load_statute
from ementa_kit.rag import Chunk, Embedder, PgHybridStore

EMBED_BATCH = 16


def statute_chunks(statute: Statute) -> list[Chunk]:
    """Vetoed and revoked articles have no text and stay out of the index."""
    source = statute.source
    return [
        Chunk(
            id=article.id,
            source_id=source.id,
            title=article.title or f"{source.title}, art. {article.number}",
            path=article.path,
            text=article.render(),
            url=article.url or source.url,
            doc_sha256=source.sha256,
            provision_ids=[p.id for p in article.provisions],
        )
        for article in statute.articles
        if not (article.vetoed or article.revoked)
    ]


def passage(chunk: Chunk) -> str:
    """What gets embedded: the title, the place in the statute (when there is one) and the text."""
    return ". ".join(part for part in (chunk.title, " > ".join(chunk.path), chunk.text) if part)


def embed_passages(embedder: Embedder, passages: Sequence[str]) -> list[list[float]]:
    """Embed in small batches of passages of similar length, in the original order.

    fastembed pads each batch to its longest text, so a single batch with every article of the
    CPC costs about as much as embedding its longest article a thousand times.
    """
    order = sorted(range(len(passages)), key=lambda i: len(passages[i]))
    vectors: list[list[float]] = [[] for _ in passages]
    for start in range(0, len(order), EMBED_BATCH):
        batch = order[start : start + EMBED_BATCH]
        embedded = embedder.embed_passages([passages[i] for i in batch])
        for i, vector in zip(batch, embedded, strict=True):
            vectors[i] = vector
    return vectors


def ingest_statute(
    source_id: str,
    store: PgHybridStore,
    embedder: Embedder,
    *,
    reset: bool = False,
    corpus_dir: Path = DEFAULT_CORPUS_DIR,
) -> int:
    statute = load_statute(source_id, corpus_dir)
    if reset:
        store.drop_schema()
    store.ensure_schema()
    s = statute.source
    store.upsert_source(s.id, s.title, s.url, s.retrieved_at, s.sha256, s.license or None)
    chunks = statute_chunks(statute)
    embeddings = embed_passages(embedder, [passage(c) for c in chunks])
    return store.replace_chunks(s.id, chunks, embeddings, embedder.model_name)
