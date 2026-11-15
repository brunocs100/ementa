"""Model spend caps: each call is priced from its span, and the next call is checked first.

The OpenAI instrumentation records the usage of every call on its `chat` span. `UsageRecorder`
prices it with `cost_usd`, as `CostMeter` does, and writes one row to `ai.model_usage`. Before
every call made through the `ModelRegistry`, `BudgetGuard` adds up the rows of the current UTC
day and month and raises `BudgetExceededError` when a cap is reached, so the provider is never
called. The caps are per environment (`env`) and, optionally, per practice area: calls inside
`practice_area("consumidor")` also count against the caps of that area.

The check runs before a call and the cost is known only after it, so the call that crosses a cap
still completes and the next one is refused. Days and months are UTC, as in the OpenAI usage
dashboard.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import psycopg
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from psycopg import sql
from psycopg.rows import dict_row

from ementa_kit.config import AreaBudget, Settings
from ementa_kit.llm.pricing import CallUsage, usage_from_attributes
from ementa_kit.telemetry.logging import get_logger

AREA_ATTRIBUTE = "ementa.area"

log = get_logger(__name__)

_area: ContextVar[str | None] = ContextVar("ementa_practice_area", default=None)


@contextmanager
def practice_area(area: str | None) -> Iterator[None]:
    """Model calls made inside the block count against the caps of `area` too."""
    token = _area.set(area)
    try:
        yield
    finally:
        _area.reset(token)


def current_practice_area() -> str | None:
    return _area.get()


class BudgetWindow(StrEnum):
    DAY = "day"
    MONTH = "month"


@dataclass(frozen=True)
class BudgetCap:
    window: BudgetWindow
    limit_usd: float
    area: str | None = None  # None: the whole environment


class BudgetExceededError(RuntimeError):
    """A spend cap is reached; model calls are refused until `resets_at`."""

    def __init__(self, cap: BudgetCap, env: str, spent_usd: float, resets_at: datetime) -> None:
        self.cap = cap
        self.env = env
        self.spent_usd = spent_usd
        self.resets_at = resets_at
        period = "daily" if cap.window is BudgetWindow.DAY else "monthly"
        scope = f"the {env} environment"
        if cap.area is not None:
            scope = f"the {cap.area} practice area in {scope}"
        super().__init__(
            f"The {period} model budget of {scope} is spent: US$ {spent_usd:.4f} of "
            f"US$ {cap.limit_usd:.2f}. Model calls resume at {resets_at:%Y-%m-%d %H:%M} UTC."
        )


@dataclass(frozen=True)
class ModelUsage:
    """One priced model call, as it goes to the ledger."""

    at: datetime
    env: str
    area: str | None
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float | None  # None: the model has no price in prices.toml
    trace_id: str | None = None
    span_id: str | None = None


@dataclass(frozen=True)
class Spend:
    day_usd: float
    month_usd: float


class UsageLedger(Protocol):
    def record(self, usage: ModelUsage) -> None: ...

    def spend(self, env: str, area: str | None, day: datetime, month: datetime) -> Spend:
        """What `env` (or one `area` of it) spent since `day` and since `month`."""
        ...


_SCHEMA = """
create schema if not exists {schema};
create table if not exists {schema}.model_usage (
    id bigint generated always as identity primary key,
    at timestamptz not null,
    env text not null,
    area text,
    model text not null,
    input_tokens integer not null,
    cached_input_tokens integer not null,
    output_tokens integer not null,
    cost_usd numeric,
    trace_id text,
    span_id text
);
create index if not exists model_usage_env_at on {schema}.model_usage (env, at);
"""

_SPEND = """
select
    coalesce(sum(cost_usd) filter (where at >= %(day)s), 0)::float8 as day_usd,
    coalesce(sum(cost_usd), 0)::float8 as month_usd
from {schema}.model_usage
where env = %(env)s and at >= %(month)s and (%(area)s::text is null or area = %(area)s)
"""


class PgUsageLedger:
    """`ai.model_usage` on Postgres: one row per model call that reported its usage."""

    def __init__(self, database_url: str, schema: str = "ai") -> None:
        self.database_url = database_url
        self.schema = schema

    def _sql(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(schema=sql.Identifier(self.schema))

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(
            self.database_url, autocommit=True, row_factory=dict_row, connect_timeout=5
        )

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(self._sql(_SCHEMA))

    def record(self, usage: ModelUsage) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    "insert into {schema}.model_usage (at, env, area, model, input_tokens, "
                    "cached_input_tokens, output_tokens, cost_usd, trace_id, span_id) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                ),
                (
                    usage.at,
                    usage.env,
                    usage.area,
                    usage.model,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.cost_usd,
                    usage.trace_id,
                    usage.span_id,
                ),
            )

    def spend(self, env: str, area: str | None, day: datetime, month: datetime) -> Spend:
        with self._connect() as conn:
            row = conn.execute(
                self._sql(_SPEND), {"env": env, "area": area, "day": day, "month": month}
            ).fetchone()
        return Spend(row["day_usd"], row["month_usd"]) if row else Spend(0.0, 0.0)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _next_month(month: datetime) -> datetime:
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1)


class Budget:
    """Spend caps of one environment, and of its practice areas, over a ledger of model calls."""

    def __init__(
        self,
        ledger: UsageLedger,
        env: str,
        daily_usd: float,
        monthly_usd: float,
        areas: Mapping[str, AreaBudget] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.ledger = ledger
        self.env = env
        self.daily_usd = daily_usd
        self.monthly_usd = monthly_usd
        self.areas = dict(areas or {})
        self.clock = clock

    @classmethod
    def from_settings(cls, settings: Settings, ledger: UsageLedger) -> "Budget":
        return cls(
            ledger,
            settings.env.value,
            settings.budget_daily_usd,
            settings.budget_monthly_usd,
            settings.budget_areas,
        )

    def caps(self, area: str | None = None) -> list[BudgetCap]:
        """The caps a call in `area` must respect, the monthly ones first: they block longer."""
        caps = [BudgetCap(BudgetWindow.MONTH, self.monthly_usd)]
        limits = self.areas.get(area) if area is not None else None
        if limits is not None and limits.monthly_usd is not None:
            caps.append(BudgetCap(BudgetWindow.MONTH, limits.monthly_usd, area))
        caps.append(BudgetCap(BudgetWindow.DAY, self.daily_usd))
        if limits is not None and limits.daily_usd is not None:
            caps.append(BudgetCap(BudgetWindow.DAY, limits.daily_usd, area))
        return caps

    def check(self, area: str | None = None) -> None:
        """Raise `BudgetExceededError` when the spend of the day or of the month reached a cap."""
        now = self.clock().astimezone(UTC)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month = day.replace(day=1)
        spends: dict[str | None, Spend] = {}
        for cap in self.caps(area):
            if cap.area not in spends:
                spends[cap.area] = self.ledger.spend(self.env, cap.area, day, month)
            spend = spends[cap.area]
            if cap.window is BudgetWindow.DAY:
                spent, resets_at = spend.day_usd, day + timedelta(days=1)
            else:
                spent, resets_at = spend.month_usd, _next_month(month)
            if spent >= cap.limit_usd:
                raise BudgetExceededError(cap, self.env, spent, resets_at)

    def record(
        self,
        usage: CallUsage,
        area: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> None:
        self.ledger.record(
            ModelUsage(
                at=self.clock(),
                env=self.env,
                area=area,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cost_usd=usage.cost_usd,
                trace_id=trace_id,
                span_id=span_id,
            )
        )


class BudgetGuard(BaseCallbackHandler):
    """Callback on every chat model of the registry: checks the budget as the call starts.

    LangChain runs it before sending the request and, with `raise_error`, lets the error out, so
    a spent budget stops the call before it reaches the provider.
    """

    raise_error = True

    def __init__(self, budget: Budget) -> None:
        self.budget = budget

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.budget.check(current_practice_area())

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.budget.check(current_practice_area())


class UsageRecorder(SpanProcessor):
    """Writes the usage of each model call span to the budget's ledger.

    GenAI spans opened inside `practice_area()` get the `ementa.area` attribute, so the area is
    in the trace and in the ledger row.
    """

    def __init__(self, budget: Budget) -> None:
        self.budget = budget

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        area = current_practice_area()
        if area is not None and "gen_ai.operation.name" in (span.attributes or {}):
            span.set_attribute(AREA_ATTRIBUTE, area)

    def on_end(self, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        usage = usage_from_attributes(attributes)
        if usage is None:
            return
        area = attributes.get(AREA_ATTRIBUTE)
        context = span.get_span_context()
        try:
            self.budget.record(
                usage,
                area=str(area) if area is not None else None,
                trace_id=format(context.trace_id, "032x") if context else None,
                span_id=format(context.span_id, "016x") if context else None,
            )
        except Exception as error:  # the call already happened; a ledger outage must not fail it
            log.warning("budget_usage_not_recorded", model=usage.model, error=type(error).__name__)
            return
        if usage.cost_usd is None:
            log.warning("budget_model_unpriced", model=usage.model)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def track_usage(budget: Budget, provider: TracerProvider | None = None) -> UsageRecorder:
    """Record every model call of the process in the budget's ledger.

    Call it after `ementa_kit.telemetry.setup()`: the cost of a call comes from its span, so with
    no tracer provider nothing would be counted and the caps would never stop anything.
    """
    target = provider if provider is not None else trace.get_tracer_provider()
    if not isinstance(target, TracerProvider):
        raise RuntimeError(
            "No tracer provider: call ementa_kit.telemetry.setup() before track_usage(); "
            "the budget reads the cost of each model call from its span."
        )
    recorder = UsageRecorder(budget)
    target.add_span_processor(recorder)
    return recorder
