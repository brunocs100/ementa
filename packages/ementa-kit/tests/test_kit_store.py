"""Hybrid search on a real Postgres with pgvector. Set EMENTA_TEST_DATABASE_URL to run."""

import hashlib
import math
import os
import uuid
from collections.abc import Iterator, Sequence

import pytest

from ementa_kit.rag import Chunk, PgHybridStore

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")
DIM = 16


def fake_embedding(text: str) -> list[float]:
    """Bag of hashed words: texts sharing words are close. Enough to test the SQL."""
    vector = [0.0] * DIM
    for word in text.lower().split():
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


CHUNKS = [
    Chunk(
        "t:art1",
        "t",
        "Lei, art. 1",
        ["Título I"],
        "O consumidor pode desistir do contrato.",
        "u",
        "h",
        ["t:art1"],
    ),
    Chunk(
        "t:art2",
        "t",
        "Lei, art. 2",
        ["Título I"],
        "A cobrança indevida é devolvida em dobro.\nExceção.",
        "u",
        "h",
        ["t:art2", "t:art2:pu"],
    ),
    Chunk(
        "t:art3",
        "t",
        "Lei, art. 3",
        ["Título II"],
        "O fabricante responde pelo defeito.",
        "u",
        "h",
        ["t:art3"],
    ),
]


def embed(texts: Sequence[str]) -> list[list[float]]:
    return [fake_embedding(t) for t in texts]


@pytest.fixture
def store() -> Iterator[PgHybridStore]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    store = PgHybridStore(DATABASE_URL, DIM, schema=f"test_{uuid.uuid4().hex[:8]}")
    store.ensure_schema()
    store.upsert_source("t", "Lei de teste", "u", "2026-11-15", "h", None)
    store.replace_chunks("t", CHUNKS, embed([c.text for c in CHUNKS]), "fake")
    yield store
    store.drop_schema()


def test_hybrid_search_ranks_the_matching_article_first(store: PgHybridStore) -> None:
    query = "cobrança indevida em dobro"
    hits = store.hybrid_search(query, fake_embedding(query), limit=3)

    assert hits[0].id == "t:art2"
    assert hits[0].vector_rank == 1 and hits[0].text_rank == 1
    assert hits[0].provision_ids == ["t:art2", "t:art2:pu"]


def test_text_search_ignores_accents(store: PgHybridStore) -> None:
    hits = store.hybrid_search("cobranca", [0.0] * (DIM - 1) + [1.0], limit=3)

    assert next(h for h in hits if h.text_rank == 1).id == "t:art2"


def test_get_many_keeps_the_order_and_skips_unknown(store: PgHybridStore) -> None:
    assert [h.id for h in store.get_many(["t:art3", "nope", "t:art1"])] == ["t:art3", "t:art1"]


def test_replace_chunks_is_idempotent(store: PgHybridStore) -> None:
    store.replace_chunks("t", CHUNKS[:2], embed([c.text for c in CHUNKS[:2]]), "fake")

    assert store.count("t") == 2


def test_dimension_mismatch_is_reported(store: PgHybridStore) -> None:
    other = PgHybridStore(store.database_url, DIM + 1, schema=store.schema)

    with pytest.raises(RuntimeError, match="vector"):
        other.ensure_schema()
