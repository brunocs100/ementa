"""Process-wide services built from the settings, once, on first use."""

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from ementa.agents.legal_research import LegalResearchAgent
from ementa.agents.legal_research.graph import CONTEXT_LIMIT
from ementa.knowledge import KnowledgeBase
from ementa.users import UserDirectory
from ementa_kit.config import get_settings
from ementa_kit.llm import ModelRegistry
from ementa_kit.rag import FastEmbedder, FastReranker, PgHybridStore
from ementa_kit.skills import SkillLibrary
from ementa_kit.telemetry import instrument_openai

if TYPE_CHECKING:
    from ementa.pii import PrivateResearcher

SKILLS_DIR = Path(__file__).resolve().parents[4] / "skills"


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
        margin=settings.rerank_margin,
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


@lru_cache
def skill_library() -> SkillLibrary:
    """The runtime skills in `skills/` at the repository root."""
    settings = get_settings()
    return SkillLibrary(
        SKILLS_DIR,
        allowed_scripts=settings.skill_scripts_allowed,
        script_timeout_s=settings.skill_script_timeout_s,
    )


@lru_cache
def user_directory() -> UserDirectory:
    """The firm's users, in `firm.users`; the schema is created on first use."""
    directory = UserDirectory(get_settings().database_url)
    directory.ensure_schema()
    return directory


@lru_cache
def private_legal_research() -> "PrivateResearcher":
    """What the API and the MCP server call: no personal data reaches the cloud model.

    Imported here because Presidio and spaCy take seconds to import.
    """
    from ementa.pii import PrivateResearcher

    return PrivateResearcher(legal_research_agent())
