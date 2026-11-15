from collections import defaultdict
from collections.abc import Iterable, Sequence

RRF_K = 60


def rrf_fuse(rankings: Iterable[Sequence[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: score(id) = sum of 1 / (k + rank), rank starting at 1."""
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
