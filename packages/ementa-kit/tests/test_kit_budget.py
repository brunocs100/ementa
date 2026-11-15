"""Spend caps with fake models and a ledger in memory: no provider and no database."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatResult
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider

from ementa_kit.config import AreaBudget, Settings
from ementa_kit.llm import (
    Budget,
    BudgetExceededError,
    ModelRegistry,
    ModelRole,
    cost_usd,
    practice_area,
    track_usage,
)
from ementa_kit.llm.budget import BudgetCap, BudgetWindow, ModelUsage, Spend

NOW = datetime(2026, 11, 15, 14, 30, tzinfo=UTC)
QUESTION = [HumanMessage("Posso desistir da compra?")]


class MemoryLedger:
    def __init__(self) -> None:
        self.rows: list[ModelUsage] = []
        self.down = False

    def record(self, usage: ModelUsage) -> None:
        if self.down:
            raise ConnectionError("ledger down")
        self.rows.append(usage)

    def spend(self, env: str, area: str | None, day: datetime, month: datetime) -> Spend:
        rows = [
            r
            for r in self.rows
            if r.env == env and r.at >= month and (area is None or r.area == area)
        ]
        return Spend(
            sum(r.cost_usd or 0.0 for r in rows if r.at >= day),
            sum(r.cost_usd or 0.0 for r in rows),
        )

    def spent(
        self, cost: float, at: datetime = NOW, env: str = "dev", area: str | None = None
    ) -> None:
        self.rows.append(ModelUsage(at, env, area, "gpt-6-luna", 10, 1, 0, cost))


class CountingChat(GenericFakeChatModel):
    """Counts the calls that get past the callbacks, where the provider would be called."""

    calls: int = 0

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.calls += 1
        return super()._generate(messages, *args, **kwargs)


def chat() -> CountingChat:
    return CountingChat(messages=iter([AIMessage("ok")] * 5))


def budget(ledger: MemoryLedger, **overrides: Any) -> Budget:
    caps: dict[str, Any] = {"daily_usd": 2.0, "monthly_usd": 30.0, **overrides}
    return Budget(ledger, "dev", clock=lambda: NOW, **caps)


def registry_model(guard: Budget, model: CountingChat) -> CountingChat:
    registry = ModelRegistry(Settings(_env_file=None), factory=lambda _: model, budget=guard)  # type: ignore[call-arg]
    built = registry.chat(ModelRole.DEFAULT)
    assert built is model
    return model


def test_default_caps_are_the_plan_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DAILY_USD", "MONTHLY_USD", "AREAS"):
        monkeypatch.delenv(f"EMENTA_BUDGET_{name}", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert (settings.budget_daily_usd, settings.budget_monthly_usd) == (2.0, 30.0)
    assert settings.budget_areas == {}


def test_spent_daily_budget_stops_the_call_before_the_provider() -> None:
    ledger = MemoryLedger()
    ledger.spent(1.5, at=NOW - timedelta(hours=3))
    ledger.spent(0.5)
    model = registry_model(budget(ledger), chat())

    with pytest.raises(BudgetExceededError) as raised:
        model.invoke(QUESTION)

    assert model.calls == 0
    error = raised.value
    assert error.cap == BudgetCap(BudgetWindow.DAY, 2.0)
    assert error.resets_at == datetime(2026, 11, 16, tzinfo=UTC)
    assert str(error) == (
        "The daily model budget of the dev environment is spent: US$ 2.0000 of US$ 2.00. "
        "Model calls resume at 2026-11-16 00:00 UTC."
    )


def test_under_the_caps_the_call_reaches_the_model() -> None:
    ledger = MemoryLedger()
    ledger.spent(1.99)
    model = registry_model(budget(ledger), chat())

    assert model.invoke(QUESTION).text == "ok"
    assert model.calls == 1


def test_monthly_cap_counts_the_month_only_and_is_named_first() -> None:
    ledger = MemoryLedger()
    ledger.spent(29.0, at=datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    ledger.spent(50.0, at=datetime(2026, 10, 31, 23, 59, tzinfo=UTC))  # last month
    guard = budget(ledger)
    guard.check()  # 29 of 30 this month

    ledger.spent(3.0)  # also spends the daily cap: the monthly one blocks longer
    with pytest.raises(BudgetExceededError, match="monthly model budget") as raised:
        guard.check()
    assert raised.value.resets_at == datetime(2026, 12, 1, tzinfo=UTC)
    assert "resume at 2026-12-01 00:00 UTC" in str(raised.value)


def test_area_caps_apply_inside_their_practice_area_only() -> None:
    ledger = MemoryLedger()
    ledger.spent(0.5, area="consumidor")
    guard = budget(ledger, areas={"consumidor": AreaBudget(daily_usd=0.5)})
    model = registry_model(guard, chat())

    model.invoke(QUESTION)  # no area: only the environment caps
    with practice_area("trabalhista"):
        model.invoke(QUESTION)  # an area with no caps of its own
    with practice_area("consumidor"), pytest.raises(BudgetExceededError) as raised:
        model.invoke(QUESTION)

    assert model.calls == 2
    assert raised.value.cap == BudgetCap(BudgetWindow.DAY, 0.5, "consumidor")
    assert "of the consumidor practice area in the dev environment" in str(raised.value)


def test_spend_of_another_environment_does_not_count() -> None:
    ledger = MemoryLedger()
    ledger.spent(5.0, env="ci")

    budget(ledger).check()


def test_zero_cap_refuses_every_call() -> None:
    with pytest.raises(BudgetExceededError):
        budget(MemoryLedger(), daily_usd=0.0).check()


def test_guard_stops_async_streamed_and_bound_calls() -> None:
    ledger = MemoryLedger()
    ledger.spent(2.0)
    model = registry_model(budget(ledger), chat())

    with pytest.raises(BudgetExceededError):
        asyncio.run(model.ainvoke(QUESTION))
    with pytest.raises(BudgetExceededError):
        list(model.stream(QUESTION))
    with pytest.raises(BudgetExceededError):
        model.bind(stop=["fim"]).invoke(QUESTION)
    assert model.calls == 0


def test_without_a_budget_the_registry_adds_no_guard() -> None:
    model = chat()
    registry = ModelRegistry(Settings(_env_file=None), factory=lambda _: model)  # type: ignore[call-arg]

    registry.chat(ModelRole.FAST).invoke(QUESTION)
    assert model.callbacks is None


@pytest.fixture
def provider() -> Iterator[TracerProvider]:
    provider = TracerProvider()
    yield provider
    provider.shutdown()


def chat_span(provider: TracerProvider, model: str, **usage: int) -> ReadableSpan:
    """A span shaped like the OpenAI instrumentation's: request attributes when it starts,
    usage (`input_tokens` and `output_tokens`) right before it ends."""
    tracer = provider.get_tracer("openai-like")
    with tracer.start_as_current_span(
        f"chat {model}",
        attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": model},
    ) as span:
        span.set_attributes({f"gen_ai.usage.{name}": value for name, value in usage.items()})
    assert isinstance(span, ReadableSpan)
    return span


def test_recorder_writes_each_model_call_with_its_cost_and_area(provider: TracerProvider) -> None:
    ledger = MemoryLedger()
    track_usage(budget(ledger), provider)

    with practice_area("consumidor"):
        span = chat_span(provider, "gpt-6-luna", input_tokens=1000, output_tokens=100)
    with provider.get_tracer("t").start_as_current_span("retrieval knowledge.chunks"):
        pass  # not a model call: nothing to record

    [row] = ledger.rows
    context = span.get_span_context()
    assert (row.env, row.area, row.model, row.at) == ("dev", "consumidor", "gpt-6-luna", NOW)
    assert (row.input_tokens, row.output_tokens, row.cached_input_tokens) == (1000, 100, 0)
    assert row.cost_usd == pytest.approx(cost_usd("gpt-6-luna", 1000, 100))
    assert context is not None
    assert row.trace_id == format(context.trace_id, "032x")
    assert row.span_id == format(context.span_id, "016x")
    assert (span.attributes or {})["ementa.area"] == "consumidor"


def test_recorder_keeps_cached_tokens_and_unpriced_models(provider: TracerProvider) -> None:
    ledger = MemoryLedger()
    track_usage(budget(ledger), provider)

    chat_span(provider, "gpt-6.1-terra", input_tokens=2188, output_tokens=82)
    tracer = provider.get_tracer("t")
    attributes: dict[str, str | int] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "gpt-6.1-terra",
        "gen_ai.usage.input_tokens": 2188,
        "gen_ai.usage.output_tokens": 82,
        "gen_ai.usage.cache_read.input_tokens": 188,
    }
    with tracer.start_as_current_span("chat", attributes=attributes):
        pass
    chat_span(provider, "modelo-sem-preco", input_tokens=10, output_tokens=1)

    plain, cached, unpriced = ledger.rows
    assert cached.cached_input_tokens == 188
    assert cached.cost_usd == pytest.approx(cost_usd("gpt-6.1-terra", 2188, 82, 188))
    assert cached.cost_usd is not None and plain.cost_usd is not None
    assert cached.cost_usd < plain.cost_usd
    assert unpriced.cost_usd is None


def test_recorder_does_not_fail_a_call_when_the_ledger_is_down(provider: TracerProvider) -> None:
    ledger = MemoryLedger()
    ledger.down = True
    track_usage(budget(ledger), provider)

    chat_span(provider, "gpt-6-luna", input_tokens=10, output_tokens=1)  # does not raise

    assert ledger.rows == []


def test_track_usage_refuses_to_run_without_a_tracer_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: trace.ProxyTracerProvider())

    with pytest.raises(RuntimeError, match="setup"):
        track_usage(budget(MemoryLedger()))


def test_budget_from_settings_reads_the_caps_and_the_area_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMENTA_ENV", "ci")
    monkeypatch.setenv("EMENTA_BUDGET_DAILY_USD", "1.5")
    monkeypatch.setenv("EMENTA_BUDGET_MONTHLY_USD", "12")
    monkeypatch.setenv("EMENTA_BUDGET_AREAS", '{"consumidor": {"daily_usd": 0.5}}')

    guard = Budget.from_settings(Settings(_env_file=None), MemoryLedger())  # type: ignore[call-arg]

    assert guard.env == "ci"
    assert guard.caps("consumidor") == [
        BudgetCap(BudgetWindow.MONTH, 12.0),
        BudgetCap(BudgetWindow.DAY, 1.5),
        BudgetCap(BudgetWindow.DAY, 0.5, "consumidor"),
    ]


def test_area_budget_rejects_unknown_keys_and_negative_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMENTA_BUDGET_AREAS", '{"consumidor": {"daily": 1}}')
    with pytest.raises(ValueError, match="budget_areas"):
        Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setenv("EMENTA_BUDGET_AREAS", "{}")
    monkeypatch.setenv("EMENTA_BUDGET_DAILY_USD", "-1")
    with pytest.raises(ValueError, match="budget_daily_usd"):
        Settings(_env_file=None)  # type: ignore[call-arg]
