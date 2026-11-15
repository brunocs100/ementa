from enum import StrEnum
from functools import lru_cache

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    CI = "ci"
    PROD = "prod"


class AreaBudget(BaseModel):
    """Spend caps of one practice area, in USD. A window left out has no cap of its own."""

    model_config = ConfigDict(extra="forbid")

    daily_usd: float | None = Field(default=None, ge=0)
    monthly_usd: float | None = Field(default=None, ge=0)


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
    # Job queue (arq) that `ementa-worker` reads. 127.0.0.1, not localhost: Redis listens on
    # IPv4 only, and Windows tries ::1 first, which outlasts the 1 s connect timeout.
    redis_url: str = "redis://127.0.0.1:6379/0"

    otel_enabled: bool = True
    otel_endpoint: str = "http://localhost:4318"

    embedding_model: str = "intfloat/multilingual-e5-large"

    # Cross-encoder over the fused candidates (ADR 0010). Off, retrieval is the F1 one: RRF, top 5.
    rerank_enabled: bool = True
    rerank_model: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    rerank_candidates: int = Field(default=30, ge=1)  # fused hits the cross-encoder scores
    rerank_top_k: int = Field(default=8, ge=1)  # hits kept for the agent context
    # Drop hits scored more than this below the best one: on the 30 golden questions the
    # expected source was never more than 3.4 below, and the context fell from 8 to 4.7 hits.
    rerank_margin: float = Field(default=5.0, gt=0)

    # Model per role (ADR 0005). Ids checked against the OpenAI /v1/models list on 2026-11-15.
    model_reasoning: str = "gpt-6.1-terra"
    model_default: str = "gpt-6-luna"  # cheaper; Terra stays for `reasoning`
    model_fast: str = "gpt-6-luna"
    model_judge: str = "gpt-6.1-sol"
    llm_timeout_s: float = 60.0

    # Model spend caps in USD (docs/OBSERVABILIDADE.md), counted per environment (`env`) over
    # the UTC day and month. A call is refused once the spend reaches a cap, so 0 refuses all.
    budget_daily_usd: float = Field(default=2.0, ge=0)
    budget_monthly_usd: float = Field(default=30.0, ge=0)
    # Caps per practice area inside the environment, as JSON in the environment:
    # {"consumidor": {"daily_usd": 0.5, "monthly_usd": 10}}. An area not listed has none.
    budget_areas: dict[str, AreaBudget] = Field(default_factory=dict)

    # Skill scripts that may run, as "<skill>/<script>" (JSON list in the environment).
    # Anything not listed is refused.
    skill_scripts_allowed: list[str] = Field(default_factory=list)
    skill_script_timeout_s: float = 10.0

    # Long-term memory (F3). A new memory replaces a stored one about the same people when their
    # cosine similarity (local embedder, query prefix on both sides) reaches this. On 30
    # fictitious pairs, 0.97 merged 10 of 14 rewrites and none of 16 different facts.
    memory_dedupe_similarity: float = Field(default=0.97, gt=0, le=1)
    memory_min_confidence: float = Field(default=0.7, ge=0, le=1)  # below it, nothing is kept
    # The assistant writes what each finished chat turn taught, one `fast` call after `done`.
    # Off, it still reads the memories: for tests and runs that save the model calls.
    memory_write_enabled: bool = True
    # Key of the HMAC that turns a CPF, a name or an e-mail into the data subject id tagging
    # memories (LGPD forgetting). Required in prod; changing it orphans the ids already stored.
    data_subject_key: SecretStr | None = None
    # Signs the REST API tokens (HS256). Outside dev and ci the API does not start without it;
    # in dev and ci each process draws a random one, so tokens die with the process.
    jwt_secret: SecretStr | None = Field(default=None, min_length=32)
    jwt_ttl_minutes: int = Field(default=15, ge=1, le=60)

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
