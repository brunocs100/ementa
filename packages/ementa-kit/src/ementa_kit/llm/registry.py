from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from langchain_core.language_models import BaseChatModel

from ementa_kit.config import Settings


class ModelRole(StrEnum):
    REASONING = "reasoning"
    DEFAULT = "default"
    FAST = "fast"
    JUDGE = "judge"


@dataclass(frozen=True)
class ModelSpec:
    role: ModelRole
    provider: str
    model: str
    reasoning_effort: str | None


class ModelUnavailableError(RuntimeError):
    pass


_EFFORT = {
    ModelRole.REASONING: "high",
    ModelRole.DEFAULT: "medium",
    ModelRole.FAST: "low",
    ModelRole.JUDGE: "medium",
}

ChatFactory = Callable[[ModelSpec], BaseChatModel]


class ModelRegistry:
    """Maps a role to a configured chat model. Tests pass `factory` to get fake models."""

    def __init__(self, settings: Settings, factory: ChatFactory | None = None) -> None:
        self._settings = settings
        self._factory = factory or self._openai
        self._cache: dict[ModelRole, BaseChatModel] = {}

    def spec(self, role: ModelRole) -> ModelSpec:
        model = {
            ModelRole.REASONING: self._settings.model_reasoning,
            ModelRole.DEFAULT: self._settings.model_default,
            ModelRole.FAST: self._settings.model_fast,
            ModelRole.JUDGE: self._settings.model_judge,
        }[role]
        return ModelSpec(role, "openai", model, _EFFORT[role])

    def chat(self, role: ModelRole) -> BaseChatModel:
        if role not in self._cache:
            self._cache[role] = self._factory(self.spec(role))
        return self._cache[role]

    def _openai(self, spec: ModelSpec) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        key = self._settings.openai_api_key
        if key is None or not key.get_secret_value():
            raise ModelUnavailableError("OPENAI_API_KEY is not set; see .env.example")
        return ChatOpenAI(
            model=spec.model,
            api_key=key,
            reasoning_effort=spec.reasoning_effort,
            timeout=self._settings.llm_timeout_s,
            max_retries=2,
        )
