"""Load a statute snapshot into the knowledge base: one chunk per article."""

from pathlib import Path

from ementa.ingest.corpus import DEFAULT_CORPUS_DIR, Statute, load_statute
from ementa_kit.rag import Chunk, Embedder, PgHybridStore


def statute_chunks(statute: Statute) -> list[Chunk]:
    """Vetoed articles have no text and stay out of the index."""
    source = statute.source
    return [
        Chunk(
            id=article.id,
            source_id=source.id,
            title=f"{source.title}, art. {article.number}",
            path=article.path,
            text=article.render(),
            url=source.url,
            doc_sha256=source.sha256,
            provision_ids=[p.id for p in article.provisions],
        )
        for article in statute.articles
        if not article.vetoed
    ]


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
    passages = [f"{c.title}. {' > '.join(c.path)}. {c.text}" for c in chunks]
    embeddings = embedder.embed_passages(passages)
    return store.replace_chunks(s.id, chunks, embeddings, embedder.model_name)
