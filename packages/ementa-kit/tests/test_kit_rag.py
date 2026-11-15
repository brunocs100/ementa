from ementa_kit.rag import Chunk, fold, rrf_fuse


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
