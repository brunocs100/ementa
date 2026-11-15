from enum import StrEnum
from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    CI = "ci"
    PROD = "prod"


class Settings(BaseSettings):
    """Process settings. Every variable uses the `EMENTA_` prefix, except the provider keys."""

    model_config = SettingsConfigDict(
        env_prefix="EMENTA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = Environment.DEV
    service_name: str = "ementa"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql://ementa:ementa@localhost:5432/ementa"

    otel_enabled: bool = True
    otel_endpoint: str = "http://localhost:4318"

    embedding_model: str = "intfloat/multilingual-e5-large"

    # Cross-encoder over the fused candidates (ADR 0010). Off, retrieval is the F1 one: RRF, top 5.
    rerank_enabled: bool = True
    rerank_model: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    rerank_candidates: int = Field(default=30, ge=1)  # fused hits the cross-encoder scores
    rerank_top_k: int = Field(default=8, ge=1)  # hits kept for the agent context

    # Model per role (ADR 0005). Ids checked against the OpenAI /v1/models list on 2026-11-15.
    model_reasoning: str = "gpt-6.1-terra"
    model_default: str = "gpt-6-luna"  # cheaper; Terra stays for `reasoning`
    model_fast: str = "gpt-6-luna"
    model_judge: str = "gpt-6.1-sol"
    llm_timeout_s: float = 60.0

    # Skill scripts that may run, as "<skill>/<script>" (JSON list in the environment).
    # Anything not listed is refused.
    skill_scripts_allowed: list[str] = Field(default_factory=list)
    skill_script_timeout_s: float = 10.0

    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("EMENTA_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )

    @property
    def is_prod(self) -> bool:
        return self.env is Environment.PROD


@lru_cache
def get_settings() -> Settings:
    """Settings read once per process."""
    return Settings()
