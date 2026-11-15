"""Process-wide services built from the settings, once, on first use."""

from functools import lru_cache

from ementa.agents.legal_research import LegalResearchAgent
from ementa.knowledge import KnowledgeBase
from ementa_kit.config import get_settings
from ementa_kit.llm import ModelRegistry
from ementa_kit.rag import FastEmbedder, PgHybridStore
from ementa_kit.telemetry import instrument_openai


@lru_cache
def knowledge_base() -> KnowledgeBase:
    settings = get_settings()
    embedder = FastEmbedder(settings.embedding_model)
    return KnowledgeBase(PgHybridStore(settings.database_url, embedder.dim), embedder)


@lru_cache
def model_registry() -> ModelRegistry:
    instrument_openai()
    return ModelRegistry(get_settings())


@lru_cache
def legal_research_agent() -> LegalResearchAgent:
    return LegalResearchAgent(knowledge_base(), model_registry())
