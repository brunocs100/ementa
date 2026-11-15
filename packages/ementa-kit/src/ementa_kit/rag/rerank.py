"""Local cross-encoder rerank. Text never leaves the machine to be scored (ADR 0010)."""

from collections.abc import Sequence
from functools import cached_property
from typing import Protocol

# Cross-encoders outside the fastembed 0.9 list, registered on first use: model file in the
# Hugging Face repo, license and size in GB (checked on the model page on 2026-11-15).
EXTRA_MODELS = {
    "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1": ("onnx/model.onnx", "apache-2.0", 0.47),
}

# One pair per run. A batch is padded to its longest passage, and on CPU the padding costs more
# than the batch saves: 1.8 s for 30 CDC articles one by one, 4.7 s in one batch (ADR 0010).
BATCH_SIZE = 1


class Reranker(Protocol):
    model_name: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """One relevance score per passage, in the order given. Higher is more relevant."""
        ...


class FastReranker:
    """fastembed cross-encoder (ONNX on CPU). The model is downloaded on first use and cached."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    @cached_property
    def _model(self):  # type: ignore[no-untyped-def]
        from fastembed.common.model_description import ModelSource
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        listed = {m["model"].lower() for m in TextCrossEncoder.list_supported_models()}
        if self.model_name.lower() not in listed and self.model_name in EXTRA_MODELS:
            model_file, license, size = EXTRA_MODELS[self.model_name]
            TextCrossEncoder.add_custom_model(
                self.model_name,
                ModelSource(hf=self.model_name),
                model_file=model_file,
                license=license,
                size_in_gb=size,
            )
        return TextCrossEncoder(self.model_name)

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        return [float(s) for s in self._model.rerank(query, list(passages), batch_size=BATCH_SIZE)]
