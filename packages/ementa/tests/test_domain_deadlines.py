from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ementa.domain.deadlines import (
    calculate_deadline,
    easter_sunday,
    is_business_day,
    is_recess,
    non_business_reason,
)

DATES = st.dates(min_value=date(2024, 1, 1), max_value=date(2030, 12, 31))
DAYS = st.integers(min_value=1, max_value=120)


def test_demo_case_crosses_the_recess() -> None:
    # Intimation on Thursday 03/12/2026, 15 business days at the TJRS.
    result = calculate_deadline(date(2026, 12, 3), 15)

    assert result.due_date == date(2027, 1, 26)
    assert result.counted_days[0] == date(2026, 12, 4)
    assert result.counted_days[10] == date(2026, 12, 18)
    assert result.counted_days[11] == date(2027, 1, 21)
    assert any("art. 220" in p for p in result.premises)


def test_start_day_is_excluded_and_weekend_skipped() -> None:
    # Friday 06/03/2026 + 1 business day = Monday 09/03/2026 (CPC, art. 224).
    result = calculate_deadline(date(2026, 3, 6), 1)

    assert result.due_date == date(2026, 3, 9)
    assert [d for d, _ in result.skipped_days] == [date(2026, 3, 7), date(2026, 3, 8)]


def test_national_and_rs_holidays_are_skipped() -> None:
    assert non_business_reason(date(2026, 11, 20)) is not None  # Consciência Negra
    assert non_business_reason(date(2026, 9, 21)) is None  # Monday after Data Magna
    assert non_business_reason(date(2027, 9, 20)) is not None  # Data Magna do RS, Monday
    assert non_business_reason(date(2026, 4, 3)) is not None  # Good Friday 2026


def test_extra_closed_days_are_respected() -> None:
    base = calculate_deadline(date(2026, 3, 2), 3)
    closed = calculate_deadline(date(2026, 3, 2), 3, extra_closed_days=[date(2026, 3, 4)])

    assert base.due_date == date(2026, 3, 5)
    assert closed.due_date == date(2026, 3, 6)


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, date(2024, 3, 31)), (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28))],
)
def test_easter(year: int, expected: date) -> None:
    assert easter_sunday(year) == expected


def test_rejects_non_positive_days() -> None:
    with pytest.raises(ValueError, match="business_days"):
        calculate_deadline(date(2026, 3, 2), 0)


@given(start=DATES, days=DAYS)
def test_due_date_is_a_business_day_after_start(start: date, days: int) -> None:
    result = calculate_deadline(start, days)

    assert result.due_date > start
    assert is_business_day(result.due_date)
    assert len(result.counted_days) == days
    assert result.counted_days[-1] == result.due_date


@given(start=DATES, days=DAYS)
def test_no_counted_day_falls_in_the_recess(start: date, days: int) -> None:
    result = calculate_deadline(start, days)

    assert not any(is_recess(d) for d in result.counted_days)


@given(start=DATES, days=DAYS)
def test_more_days_never_bring_the_due_date_forward(start: date, days: int) -> None:
    assert calculate_deadline(start, days + 1).due_date > calculate_deadline(start, days).due_date


@given(start=DATES, days=DAYS)
def test_every_day_between_is_either_counted_or_skipped(start: date, days: int) -> None:
    result = calculate_deadline(start, days)
    span = (result.due_date - start).days

    between = {start + timedelta(days=i) for i in range(1, span + 1)}
    assert between == set(result.counted_days) | {d for d, _ in result.skipped_days}
