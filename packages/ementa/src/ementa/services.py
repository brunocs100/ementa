"""Process-wide services built from the settings, once, on first use."""

from functools import lru_cache
from pathlib import Path

from ementa.agents.legal_research import LegalResearchAgent
from ementa.knowledge import KnowledgeBase
from ementa_kit.config import get_settings
from ementa_kit.llm import ModelRegistry
from ementa_kit.rag import FastEmbedder, PgHybridStore
from ementa_kit.skills import SkillLibrary
from ementa_kit.telemetry import instrument_openai

SKILLS_DIR = Path(__file__).resolve().parents[4] / "skills"


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


@lru_cache
def skill_library() -> SkillLibrary:
    """The runtime skills in `skills/` at the repository root."""
    settings = get_settings()
    return SkillLibrary(
        SKILLS_DIR,
        allowed_scripts=settings.skill_scripts_allowed,
        script_timeout_s=settings.skill_script_timeout_s,
    )
