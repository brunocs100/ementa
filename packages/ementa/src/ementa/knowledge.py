"""Knowledge base of the legal corpus: hybrid search and lookup by source id."""

import re
from collections.abc import Iterable, Sequence

from ementa_kit.rag import Embedder, Hit, PgHybridStore, rrf_fuse
from ementa_kit.telemetry import get_tracer

_ARTICLE_OF = re.compile(r"^([a-z]+:art\d+(?:-[A-Z])?)")

tracer = get_tracer(__name__)


def article_of(source_id: str) -> str:
    """`cdc:art49:pu` -> `cdc:art49`, the chunk that holds it."""
    match = _ARTICLE_OF.match(source_id)
    return match.group(1) if match else source_id


class KnowledgeBase:
    def __init__(self, store: PgHybridStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder

    def search(self, queries: Sequence[str], limit: int = 5) -> list[Hit]:
        """Hybrid search for each query, then RRF across the queries."""
        with tracer.start_as_current_span(
            "retrieval knowledge.chunks",
            attributes={
                "gen_ai.operation.name": "retrieval",
                "gen_ai.data_source.id": f"{self.store.schema}.chunks",
                "ementa.retrieval.queries": len(queries),
                "ementa.embedding.model": self.embedder.model_name,
            },
        ) as span:
            vectors = self.embedder.embed_queries(queries)
            per_query = [
                self.store.hybrid_search(q, v, limit=limit * 2)
                for q, v in zip(queries, vectors, strict=True)
            ]
            by_id = {hit.id: hit for hits in per_query for hit in hits}
            fused = rrf_fuse([[h.id for h in hits] for hits in per_query])[:limit]
            result = [by_id[i] for i, _ in fused]
            span.set_attribute("ementa.retrieval.hits", [h.id for h in result])
            return result

    def get(self, source_ids: Iterable[str]) -> list[Hit]:
        return self.store.get_many(article_of(i) for i in source_ids)
