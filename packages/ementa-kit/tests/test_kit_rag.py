from collections.abc import Iterator, Sequence

from ementa_kit.rag import Chunk, FastReranker, fold, rrf_fuse


def test_rrf_rewards_items_ranked_by_both_lists() -> None:
    fused = rrf_fuse([["a", "b", "c"], ["b", "d"]])

    assert [item for item, _ in fused] == ["b", "a", "d", "c"]
    assert fused[0][1] == 1 / 62 + 1 / 61


def test_rrf_breaks_ties_by_id() -> None:
    assert [i for i, _ in rrf_fuse([["b"], ["a"]])] == ["a", "b"]


def test_fold_drops_accents_and_case() -> None:
    assert fold("Cobrança INDÉBITA, ação") == "cobranca indebita, acao"


def test_chunk_search_text_has_title_path_and_text() -> None:
    chunk = Chunk("x:art1", "x", "Lei, art. 1", ["Título I"], "Até já", "u", "h")

    assert chunk.search_text() == "lei, art. 1 titulo i ate ja"


def test_fast_reranker_loads_nothing_until_there_is_something_to_score() -> None:
    reranker = FastReranker("org/not-downloaded")

    assert reranker.score("pergunta", []) == []
    assert "_model" not in vars(reranker)


class LengthEncoder:
    """Stands in for the fastembed cross-encoder: the score is the passage length."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], int]] = []

    def rerank(self, query: str, documents: Sequence[str], batch_size: int) -> Iterator[float]:
        self.calls.append((query, list(documents), batch_size))
        return (float(len(d)) for d in documents)


def test_fast_reranker_scores_each_pair_alone_in_the_order_asked() -> None:
    reranker = FastReranker("org/not-downloaded")
    encoder = LengthEncoder()
    reranker.__dict__["_model"] = encoder  # what the cached property holds once loaded

    assert reranker.score("pergunta", ["ccc", "a", "bb"]) == [3.0, 1.0, 2.0]
    assert encoder.calls == [("pergunta", ["ccc", "a", "bb"], 1)]  # one pair per run, no padding
