"""`KnowledgeBase.search` with fakes: the F1 fusion without rerank, the cross-encoder with it."""

from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ementa import knowledge
from ementa.knowledge import KnowledgeBase, passage
from ementa_kit.rag import Hit, PgHybridStore


def hit(i: str) -> Hit:
    return Hit(i, "t", f"Lei, art. {i}", ["Título I"], f"Texto do {i}.", [i], "u")


class FakeStore(PgHybridStore):
    """Each query text has its own ranking. Records the limit of every search."""

    def __init__(self, rankings: dict[str, list[str]]) -> None:
        super().__init__("postgresql://unused", dim=2)
        self.rankings = rankings
        self.limits: list[int] = []

    def hybrid_search(self, text: str, vector: Sequence[float], limit: int = 8) -> list[Hit]:
        self.limits.append(limit)
        return [hit(i) for i in self.rankings[text][:limit]]


class FakeEmbedder:
    model_name = "fake-embedder"
    dim = 2

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class FakeReranker:
    """Scores from a table by hit id (0 if absent). Records what it was asked."""

    model_name = "fake-reranker"

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = {passage(hit(i)): s for i, s in scores.items()}
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        self.calls.append((query, list(passages)))
        return [self.scores.get(p, 0.0) for p in passages]


# Fused by RRF: a1 (1/61 + 1/62), a3 (1/63 + 1/61), a2 (1/62), a5 (1/63), a4 (1/64).
RANKINGS = {"question": ["a1", "a2", "a3", "a4"], "rewrite": ["a3", "a1", "a5"]}


def test_without_reranker_the_f1_fusion_is_kept() -> None:
    store = FakeStore(RANKINGS)

    hits = KnowledgeBase(store, FakeEmbedder()).search(["question", "rewrite"], limit=2)

    assert [h.id for h in hits] == ["a1", "a3"]
    assert store.limits == [4, 4]  # twice the limit from each query, as in F1


def test_reranker_scores_the_fused_candidates_against_the_question() -> None:
    store = FakeStore(RANKINGS)
    reranker = FakeReranker({"a5": 3.0, "a2": 2.0})
    kb = KnowledgeBase(store, FakeEmbedder(), reranker, candidates=4)

    hits = kb.search(["question", "rewrite"], limit=3)

    assert store.limits == [4, 4]
    [(query, passages)] = reranker.calls
    assert query == "question"
    assert passages == [passage(hit(i)) for i in ["a1", "a3", "a2", "a5"]]  # a4 is the 5th
    assert [h.id for h in hits] == ["a5", "a2", "a1"]  # a1 and a3 tie at 0: fused order


def test_reranker_gets_at_least_the_hits_asked_for() -> None:
    store = FakeStore(RANKINGS)
    reranker = FakeReranker({})
    kb = KnowledgeBase(store, FakeEmbedder(), reranker, candidates=2)

    hits = kb.search(["question"], limit=3)

    assert store.limits == [3]
    assert [h.id for h in hits] == ["a1", "a2", "a3"]


def test_hits_far_below_the_best_score_stay_out_of_the_context() -> None:
    # a5 is the best (6.0); a2 is 4.0 below and stays; a1 and a3 at 0 are 6.0 below and go.
    reranker = FakeReranker({"a5": 6.0, "a2": 2.0})
    kb = KnowledgeBase(FakeStore(RANKINGS), FakeEmbedder(), reranker, candidates=4, margin=5.0)

    hits = kb.search(["question", "rewrite"], limit=4)

    assert [h.id for h in hits] == ["a5", "a2"]


def test_no_candidates_means_no_rerank() -> None:
    reranker = FakeReranker({})
    kb = KnowledgeBase(FakeStore({"question": []}), FakeEmbedder(), reranker)

    assert kb.search(["question"]) == []
    assert reranker.calls == []


def test_rerank_span_records_model_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(knowledge, "tracer", provider.get_tracer("test"))
    reranker = FakeReranker({"a5": 3.0, "a2": 2.0})
    kb = KnowledgeBase(FakeStore(RANKINGS), FakeEmbedder(), reranker, candidates=4)

    kb.search(["question", "rewrite"], limit=3)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    search, rerank = spans["retrieval knowledge.chunks"], spans["retrieval rerank"]
    assert rerank.parent is not None and search.context is not None
    assert rerank.parent.span_id == search.context.span_id
    assert rerank.attributes == {
        "gen_ai.operation.name": "retrieval",
        "ementa.retrieval.mode": "rerank",
        "ementa.rerank.model": "fake-reranker",
        "ementa.rerank.candidates": 4,
        "ementa.rerank.margin": 5.0,
        "ementa.rerank.kept": 3,
    }
    assert search.attributes is not None
    assert search.attributes["ementa.retrieval.hits"] == ("a5", "a2", "a1")


def test_passage_has_title_path_and_text() -> None:
    assert passage(hit("a1")) == "Lei, art. a1. Título I. Texto do a1."
