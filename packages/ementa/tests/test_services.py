"""The settings switch the rerank on and off. Nothing is downloaded or loaded here."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from ementa import services
from ementa.agents.legal_research import LegalResearchAgent
from ementa.agents.legal_research.graph import CONTEXT_LIMIT
from ementa_kit.config import Settings
from ementa_kit.llm import ModelRegistry
from ementa_kit.rag import FastReranker


class LazyEmbedder:
    """Stands in for FastEmbedder, whose `dim` would load the model."""

    dim = 1024

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., LegalResearchAgent]]:
    def agent(**overrides: Any) -> LegalResearchAgent:
        settings = Settings(_env_file=None, **overrides)  # type: ignore[call-arg]
        monkeypatch.setattr(services, "get_settings", lambda: settings)
        monkeypatch.setattr(services, "FastEmbedder", LazyEmbedder)
        monkeypatch.setattr(services, "model_registry", lambda: ModelRegistry(settings))
        return services.legal_research_agent()

    for name in ("ENABLED", "MODEL", "CANDIDATES", "TOP_K"):
        monkeypatch.delenv(f"EMENTA_RERANK_{name}", raising=False)
    services.knowledge_base.cache_clear()
    services.legal_research_agent.cache_clear()
    yield agent
    services.knowledge_base.cache_clear()
    services.legal_research_agent.cache_clear()


def test_rerank_is_on_with_the_default_settings(
    build: Callable[..., LegalResearchAgent],
) -> None:
    agent = build()
    kb = services.knowledge_base()

    assert isinstance(kb.reranker, FastReranker)
    assert kb.reranker.model_name == Settings.model_fields["rerank_model"].default
    assert kb.candidates == 30
    assert agent.context_limit == 8


def test_rerank_settings_reach_the_knowledge_base_and_the_agent(
    build: Callable[..., LegalResearchAgent],
) -> None:
    agent = build(rerank_model="org/other-reranker", rerank_candidates=20, rerank_top_k=6)
    kb = services.knowledge_base()

    assert kb.reranker is not None and kb.reranker.model_name == "org/other-reranker"
    assert kb.candidates == 20
    assert agent.context_limit == 6


def test_rerank_off_is_the_f1_retrieval(build: Callable[..., LegalResearchAgent]) -> None:
    agent = build(rerank_enabled=False)

    assert services.knowledge_base().reranker is None
    assert agent.context_limit == CONTEXT_LIMIT == 5
