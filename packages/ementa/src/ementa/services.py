"""Process-wide services built from the settings, once, on first use."""

from functools import lru_cache

from ementa.agents.legal_research import LegalResearchAgent
from ementa.agents.legal_research.graph import CONTEXT_LIMIT
from ementa.knowledge import KnowledgeBase
from ementa_kit.config import get_settings
from ementa_kit.llm import ModelRegistry
from ementa_kit.rag import FastEmbedder, FastReranker, PgHybridStore
from ementa_kit.telemetry import instrument_openai


@lru_cache
def knowledge_base() -> KnowledgeBase:
    settings = get_settings()
    embedder = FastEmbedder(settings.embedding_model)
    reranker = FastReranker(settings.rerank_model) if settings.rerank_enabled else None
    return KnowledgeBase(
        PgHybridStore(settings.database_url, embedder.dim),
        embedder,
        reranker,
        candidates=settings.rerank_candidates,
    )


@lru_cache
def model_registry() -> ModelRegistry:
    instrument_openai()
    return ModelRegistry(get_settings())


@lru_cache
def legal_research_agent() -> LegalResearchAgent:
    settings = get_settings()
    context_limit = settings.rerank_top_k if settings.rerank_enabled else CONTEXT_LIMIT
    return LegalResearchAgent(knowledge_base(), model_registry(), context_limit=context_limit)
