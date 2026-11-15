"""Procedural deadlines in business days, following the CPC (Lei 13.105/2015).

- Art. 219: only business days count.
- Art. 220: the deadline is suspended from 20 December to 20 January, inclusive.
- Art. 224: the start day is excluded and the last day is included; counting starts on the first
  business day after the start date.

The calendar covers national holidays, Good Friday and the RS state holiday. Municipal holidays
and court closures are not known here: the caller passes them in `extra_closed_days`.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

MAX_BUSINESS_DAYS = 365

NATIONAL_FIXED: dict[tuple[int, int], str] = {
    (1, 1): "Confraternização Universal (Lei 662/1949)",
    (4, 21): "Tiradentes (Lei 662/1949)",
    (5, 1): "Dia do Trabalho (Lei 662/1949)",
    (9, 7): "Independência (Lei 662/1949)",
    (10, 12): "Nossa Senhora Aparecida (Lei 6.802/1980)",
    (11, 2): "Finados (Lei 662/1949)",
    (11, 15): "Proclamação da República (Lei 662/1949)",
    (11, 20): "Dia Nacional de Zumbi e da Consciência Negra (Lei 14.759/2023)",
    (12, 25): "Natal (Lei 662/1949)",
}

STATE_FIXED: dict[str, dict[tuple[int, int], str]] = {
    "RS": {(9, 20): "Data Magna do Rio Grande do Sul (lei estadual)"},
}

SUPPORTED_STATES = tuple(STATE_FIXED)


@dataclass(frozen=True)
class DeadlineResult:
    start_date: date
    business_days: int
    due_date: date
    counted_days: tuple[date, ...]
    skipped_days: tuple[tuple[date, str], ...]
    premises: tuple[str, ...]


def easter_sunday(year: int) -> date:
    """Gregorian Easter (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def is_recess(day: date) -> bool:
    """CPC, art. 220: 20 December to 20 January, inclusive."""
    return (day.month == 12 and day.day >= 20) or (day.month == 1 and day.day <= 20)


def non_business_reason(
    day: date, state: str = "RS", extra_closed_days: frozenset[date] = frozenset()
) -> str | None:
    """Why `day` does not count, or None when it is a business day."""
    if day.weekday() == 5:
        return "sábado"
    if day.weekday() == 6:
        return "domingo"
    if is_recess(day):
        return "suspensão de 20/12 a 20/01 (CPC, art. 220)"
    key = (day.month, day.day)
    if key in NATIONAL_FIXED:
        return f"feriado nacional: {NATIONAL_FIXED[key]}"
    if key in STATE_FIXED.get(state, {}):
        return f"feriado estadual: {STATE_FIXED[state][key]}"
    if day == easter_sunday(day.year) - timedelta(days=2):
        return "Sexta-feira Santa"
    if day in extra_closed_days:
        return "dia sem expediente informado"
    return None


def is_business_day(
    day: date, state: str = "RS", extra_closed_days: frozenset[date] = frozenset()
) -> bool:
    return non_business_reason(day, state, extra_closed_days) is None


def calculate_deadline(
    start_date: date,
    business_days: int,
    state: str = "RS",
    extra_closed_days: Iterable[date] = (),
) -> DeadlineResult:
    """Last day of a deadline of `business_days`, counted from `start_date` (excluded)."""
    if not 1 <= business_days <= MAX_BUSINESS_DAYS:
        raise ValueError(f"business_days must be between 1 and {MAX_BUSINESS_DAYS}")
    if state not in STATE_FIXED:
        raise ValueError(f"state must be one of {', '.join(SUPPORTED_STATES)}")

    closed = frozenset(extra_closed_days)
    counted: list[date] = []
    skipped: list[tuple[date, str]] = []
    day = start_date
    while len(counted) < business_days:
        day += timedelta(days=1)
        reason = non_business_reason(day, state, closed)
        if reason is None:
            counted.append(day)
        else:
            skipped.append((day, reason))

    premises = [
        f"Dia do começo excluído: {start_date:%d/%m/%Y} (CPC, art. 224).",
        "Só dias úteis contam (CPC, art. 219).",
        "Prazo suspenso de 20/12 a 20/01, inclusive (CPC, art. 220).",
        f"Feriados considerados: nacionais, Sexta-feira Santa e estaduais de {state}.",
        "Feriados municipais e dias sem expediente do tribunal não entram, salvo os informados; "
        "confira o calendário do tribunal.",
        "A data inicial é o dia do começo do prazo, conforme a forma de intimação (CPC, art. 231).",
    ]
    if closed:
        listed = ", ".join(f"{d:%d/%m/%Y}" for d in sorted(closed))
        premises.append(f"Dias sem expediente informados: {listed}.")

    return DeadlineResult(
        start_date=start_date,
        business_days=business_days,
        due_date=counted[-1],
        counted_days=tuple(counted),
        skipped_days=tuple(skipped),
        premises=tuple(premises),
    )
