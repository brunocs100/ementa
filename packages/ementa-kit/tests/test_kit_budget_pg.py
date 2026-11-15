"""The usage ledger on a real Postgres. Set EMENTA_TEST_DATABASE_URL to run."""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from ementa_kit.llm import Budget, BudgetExceededError, CallUsage, PgUsageLedger
from ementa_kit.llm.budget import ModelUsage, Spend

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")
NOW = datetime(2026, 11, 15, 14, 30, tzinfo=UTC)
DAY = datetime(2026, 11, 15, tzinfo=UTC)
MONTH = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def ledger() -> Iterator[PgUsageLedger]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    ledger = PgUsageLedger(DATABASE_URL, schema=f"test_{uuid.uuid4().hex[:8]}")
    ledger.ensure_schema()
    ledger.ensure_schema()  # idempotent, as every process runs it on start
    yield ledger
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(ledger.schema)))


def usage(
    at: datetime, cost: float | None, env: str = "dev", area: str | None = None
) -> ModelUsage:
    return ModelUsage(at, env, area, "gpt-6-luna", 1000, 100, 0, cost)


def test_spend_adds_up_the_day_and_the_month_of_one_environment(ledger: PgUsageLedger) -> None:
    for row in [
        usage(datetime(2026, 10, 31, 23, 59, tzinfo=UTC), 9.0),  # last month
        usage(datetime(2026, 11, 1, tzinfo=UTC), 1.25),  # first instant of the month
        usage(datetime(2026, 11, 14, 23, 59, tzinfo=UTC), 0.5, area="consumidor"),
        usage(DAY, 0.125, area="consumidor"),  # first instant of the day
        usage(NOW, 0.25),
        usage(NOW, None),  # unpriced model: tokens kept, no cost
        usage(NOW, 7.0, env="ci"),
    ]:
        ledger.record(row)

    assert ledger.spend("dev", None, DAY, MONTH) == Spend(0.375, 2.125)
    assert ledger.spend("dev", "consumidor", DAY, MONTH) == Spend(0.125, 0.625)
    assert ledger.spend("ci", None, DAY, MONTH) == Spend(7.0, 7.0)
    assert ledger.spend("prod", None, DAY, MONTH) == Spend(0.0, 0.0)


def test_budget_on_postgres_refuses_the_call_after_the_one_that_crossed_the_cap(
    ledger: PgUsageLedger,
) -> None:
    budget = Budget(ledger, "dev", daily_usd=0.001, monthly_usd=30.0, clock=lambda: NOW)
    budget.check()

    budget.record(CallUsage("gpt-6.1-terra", input_tokens=2000, output_tokens=100))  # US$ 0.0025

    with pytest.raises(BudgetExceededError, match=r"US\$ 0\.0025 of US\$ 0\.00"):
        budget.check()
