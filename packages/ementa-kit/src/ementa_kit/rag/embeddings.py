"""Local embeddings. Text never leaves the machine to be embedded (ADR 0010)."""

from collections.abc import Sequence
from functools import cached_property
from typing import Protocol

# E5 models were trained with these prefixes and lose recall without them.
_PREFIXES = {"intfloat/multilingual-e5-large": ("query: ", "passage: ")}


class Embedder(Protocol):
    model_name: str

    @property
    def dim(self) -> int: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


class FastEmbedder:
    """fastembed (ONNX on CPU). The model is downloaded on first use and cached."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._query_prefix, self._passage_prefix = _PREFIXES.get(model_name, ("", ""))

    @cached_property
    def _model(self):  # type: ignore[no-untyped-def]
        from fastembed import TextEmbedding

        return TextEmbedding(self.model_name)

    @cached_property
    def dim(self) -> int:
        return len(self.embed_queries(["dim"])[0])

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in vector] for vector in self._model.embed(list(texts))]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([self._query_prefix + t for t in texts])

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([self._passage_prefix + t for t in texts])
