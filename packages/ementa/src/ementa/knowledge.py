"""Knowledge base of the legal corpus: hybrid search, rerank and lookup by source id."""

import re
from collections.abc import Iterable, Sequence

from ementa_kit.rag import Embedder, Hit, PgHybridStore, Reranker, rrf_fuse
from ementa_kit.telemetry import get_tracer

_ARTICLE_OF = re.compile(r"^([a-z]+:art\d+(?:-[A-Z])?)")

RERANK_CANDIDATES = 30  # fused hits the cross-encoder scores

tracer = get_tracer(__name__)


def article_of(source_id: str) -> str:
    """`cdc:art49:pu` -> `cdc:art49`, the chunk that holds it. A súmula is its own chunk."""
    match = _ARTICLE_OF.match(source_id)
    return match.group(1) if match else source_id


def passage(hit: Hit) -> str:
    """What the cross-encoder reads: title, path and text, as the embedder indexed the chunk."""
    return f"{hit.title}. {' > '.join(hit.path)}. {hit.text}"


class KnowledgeBase:
    def __init__(
        self,
        store: PgHybridStore,
        embedder: Embedder,
        reranker: Reranker | None = None,
        candidates: int = RERANK_CANDIDATES,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.candidates = candidates

    def search(self, queries: Sequence[str], limit: int = 5) -> list[Hit]:
        """Hybrid search for each query, RRF across the queries, then the rerank, if any.

        The cross-encoder scores the top `candidates` of the fusion against the first query, the
        question as asked (the others are rewrites that only widen the net), and keeps `limit`.
        """
        fetch = max(limit, self.candidates) if self.reranker is not None else limit * 2
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
                self.store.hybrid_search(q, v, limit=fetch)
                for q, v in zip(queries, vectors, strict=True)
            ]
            by_id = {hit.id: hit for hits in per_query for hit in hits}
            fused = [by_id[i] for i, _ in rrf_fuse([[h.id for h in hits] for hits in per_query])]
            if self.reranker is None or not fused:
                result = fused[:limit]
            else:
                result = self._rerank(self.reranker, queries[0], fused[:fetch], limit)
            span.set_attribute("ementa.retrieval.hits", [h.id for h in result])
            return result

    def _rerank(self, reranker: Reranker, query: str, hits: list[Hit], limit: int) -> list[Hit]:
        with tracer.start_as_current_span(
            "retrieval rerank",
            attributes={
                "gen_ai.operation.name": "retrieval",
                "ementa.retrieval.mode": "rerank",
                "ementa.rerank.model": reranker.model_name,
                "ementa.rerank.candidates": len(hits),
            },
        ) as span:
            scores = reranker.score(query, [passage(h) for h in hits])
            # sorted() is stable: hits with the same score keep the fused order.
            ranked = sorted(zip(scores, hits, strict=True), key=lambda pair: -pair[0])
            result = [hit for _, hit in ranked[:limit]]
            span.set_attribute("ementa.rerank.kept", len(result))
            return result

    def get(self, source_ids: Iterable[str]) -> list[Hit]:
        return self.store.get_many(article_of(i) for i in source_ids)
