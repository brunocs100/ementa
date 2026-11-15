"""The services follow the settings: the rerank on and off, the assistant's memory. Nothing is
downloaded here; the assistant's turn loads the PII guard."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from ementa import services
from ementa.agents.assistant import DoneEvent
from ementa.agents.legal_research import LegalResearchAgent
from ementa.agents.legal_research.graph import CONTEXT_LIMIT
from ementa.memory import lawyer
from ementa_kit.config import Settings
from ementa_kit.llm import ModelRegistry
from ementa_kit.memory import Namespace, Written
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
    caches = (services.local_embedder, services.knowledge_base, services.legal_research_agent)
    for cached in caches:
        cached.cache_clear()
    yield agent
    for cached in caches:
        cached.cache_clear()


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


def test_the_checkpointer_is_built_once_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, database_url="postgresql://x@db/ementa")  # type: ignore[call-arg]
    built: list[str] = []

    def fake(database_url: str) -> InMemorySaver:
        built.append(database_url)
        return InMemorySaver()

    monkeypatch.setattr(services, "get_settings", lambda: settings)
    monkeypatch.setattr(services, "postgres_checkpointer", fake)
    services.checkpointer.cache_clear()
    try:
        assert services.checkpointer() is services.checkpointer()
    finally:
        services.checkpointer.cache_clear()
    assert built == ["postgresql://x@db/ementa"]


def test_the_assistant_is_built_once_without_loading_the_research_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = InMemorySaver()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr(services, "checkpointer", lambda: saver)
    monkeypatch.setattr(services, "model_registry", lambda: ModelRegistry(settings))

    def research_agent() -> LegalResearchAgent:
        raise AssertionError("built on the first turn, not with the assistant")

    monkeypatch.setattr(services, "legal_research_agent", research_agent)
    services.assistant_agent.cache_clear()
    try:
        agent = services.assistant_agent()
        assert services.assistant_agent() is agent
    finally:
        services.assistant_agent.cache_clear()
    assert agent.graph.checkpointer is saver


class RecordingWriter:
    def __init__(self) -> None:
        self.targets: list[list[Namespace]] = []

    def write(
        self,
        turn: Sequence[BaseMessage],
        targets: Sequence[Namespace],
        mapping: Mapping[str, str] | None = None,
    ) -> list[Written]:
        self.targets.append(list(targets))
        return []


@pytest.mark.parametrize("write", [True, False])
def test_the_assistant_opens_the_memory_on_the_first_turn_and_writes_when_enabled(
    monkeypatch: pytest.MonkeyPatch, write: bool
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, memory_write_enabled=write, context_budget_tokens=5000
    )
    chat = GenericFakeChatModel(messages=iter([AIMessage("Bom dia.")]))
    writer = RecordingWriter()
    opened: list[str] = []

    def registry() -> ModelRegistry:
        return ModelRegistry(settings, factory=lambda spec: chat)

    def memory_store() -> InMemoryStore:
        opened.append("store")
        return InMemoryStore()

    def memory_writer() -> RecordingWriter:
        opened.append("writer")
        return writer

    monkeypatch.setattr(services, "get_settings", lambda: settings)
    monkeypatch.setattr(services, "checkpointer", InMemorySaver)
    monkeypatch.setattr(services, "model_registry", registry)
    monkeypatch.setattr(services, "memory_store", memory_store)
    monkeypatch.setattr(services, "memory_writer", memory_writer)
    services.assistant_agent.cache_clear()
    try:
        agent = services.assistant_agent()
        assert opened == []  # nothing opens with the assistant
        assert agent.context_budget == 5000
        events = list(agent.chat("Bom dia", user_id="ana", allowed_tools=[]))
        first_turn = list(opened)
        last = events[-1]
        assert isinstance(last, DoneEvent)
        agent.history(last.thread_id, user_id="ana")
    finally:
        services.assistant_agent.cache_clear()

    assert first_turn == (["store", "writer"] if write else ["store"])
    assert opened == first_turn  # reading the history needed neither
    assert writer.targets == ([[lawyer("ana")]] if write else [])
