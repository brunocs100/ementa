from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from langchain_core.callbacks import BaseCallbackHandler, BaseCallbackManager
from langchain_core.language_models import BaseChatModel

from ementa_kit.config import Settings
from ementa_kit.llm.budget import Budget, BudgetGuard


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


def _add_callback(model: BaseChatModel, handler: BaseCallbackHandler) -> None:
    if isinstance(model.callbacks, BaseCallbackManager):
        model.callbacks.add_handler(handler)
    else:
        model.callbacks = [*(model.callbacks or []), handler]


class ModelRegistry:
    """Maps a role to a configured chat model. Tests pass `factory` to get fake models.

    With a `budget`, every model it hands out checks the spend caps before each call, also after
    `bind_tools` or inside a graph, and raises `BudgetExceededError` instead of calling.
    """

    def __init__(
        self, settings: Settings, factory: ChatFactory | None = None, budget: Budget | None = None
    ) -> None:
        self._settings = settings
        self._factory = factory or self._openai
        self._cache: dict[ModelRole, BaseChatModel] = {}
        self.budget = budget

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
            model = self._factory(self.spec(role))
            if self.budget is not None:
                _add_callback(model, BudgetGuard(self.budget))
            self._cache[role] = model
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
