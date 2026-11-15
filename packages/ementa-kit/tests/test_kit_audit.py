import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ementa_kit.audit import (
    AuditEvent,
    MemoryAuditLog,
    Outcome,
    canonical,
    chain_hash,
    check_event,
    verify_chain,
)
from ementa_kit.audit.chain import CONTENT_CHANGED, LINK_BROKEN
from ementa_kit.policy import DataClass

NOON = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def event(**changes: object) -> AuditEvent:
    base = AuditEvent(
        actor="u-1",
        action="http.request",
        resource="POST /v1/legal-research",
        data_class=DataClass.PUBLIC,
        outcome=Outcome.OK,
        details={"status": 200, "duration_ms": 12},
        occurred_at=NOON,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def chain(n: int) -> MemoryAuditLog:
    log = MemoryAuditLog()
    for i in range(n):
        log.append(event(actor=f"u-{i}"))
    return log


def test_canonical_form_is_sorted_compact_json_in_utc() -> None:
    expected = (
        '{"action":"http.request","actor":"u-1","data_class":"public",'
        '"details":{"duration_ms":12,"status":200},'
        '"occurred_at":"2026-11-15T12:00:00.000000+00:00","outcome":"ok","resource":"Ação"}'
    )

    assert canonical(event(resource="Ação")) == expected.encode()


def test_the_same_instant_in_another_zone_has_the_same_hash() -> None:
    brasilia = NOON.astimezone(timezone(timedelta(hours=-3)))

    assert canonical(event(occurred_at=brasilia)) == canonical(event())


def test_hash_is_sha256_of_prev_hash_and_canonical_event() -> None:
    first = chain_hash(None, event())
    second = chain_hash(first, event(actor="u-2"))

    assert first == hashlib.sha256(canonical(event())).digest()
    assert second == hashlib.sha256(first + canonical(event(actor="u-2"))).digest()


def test_records_link_to_the_one_before_and_verify() -> None:
    log = chain(3)
    first, second, third = log.records

    assert [r.id for r in log.records] == [1, 2, 3]
    assert first.prev_hash is None
    assert (second.prev_hash, third.prev_hash) == (first.hash, second.hash)
    check = log.verify()
    assert check.intact
    assert (check.checked, check.head) == (3, third.hash)


def test_a_changed_record_is_reported_where_it_was_changed() -> None:
    log = chain(3)
    tampered = log.records[1]
    log.records[1] = replace(tampered, event=replace(tampered.event, actor="intruso"))

    check = log.verify()

    assert check.broken is not None
    assert (check.broken.id, check.broken.reason) == (2, CONTENT_CHANGED)
    assert check.checked == 1


def test_a_rewritten_record_with_a_fresh_hash_breaks_the_next_link() -> None:
    log = chain(3)
    old = log.records[1]
    new_event = replace(old.event, actor="intruso")
    log.records[1] = replace(old, event=new_event, hash=chain_hash(old.prev_hash, new_event))

    check = log.verify()

    assert check.broken is not None
    assert (check.broken.id, check.broken.reason) == (3, LINK_BROKEN)


@pytest.mark.parametrize(("removed", "reported"), [(1, 3), (0, 2), (2, None)])
def test_removing_a_record_breaks_the_chain_unless_it_is_the_last(
    removed: int, reported: int | None
) -> None:
    log = chain(3)
    del log.records[removed]

    check = verify_chain(log.records)

    assert (check.broken.id if check.broken else None) == reported


def test_the_last_record_can_go_unnoticed_without_an_external_anchor() -> None:
    log = chain(3)
    head = log.verify().head
    del log.records[-1]

    assert log.verify().intact
    assert log.verify().head != head  # compare with a head kept elsewhere to catch it


def test_page_is_newest_first_from_a_cursor() -> None:
    log = chain(5)

    assert [r.id for r in log.page(limit=2)] == [5, 4]
    assert [r.id for r in log.page(before=4, limit=2)] == [3, 2]
    assert [r.id for r in log.page(before=2)] == [1]


@pytest.mark.parametrize(
    "bad",
    [
        {"details": {"cost_usd": 0.01}},
        {"details": {"nested": [{"ratio": 1.5}]}},
        {"details": {"when": NOON}},
        {"details": {1: "x"}},
        {"occurred_at": datetime(2026, 11, 15, 12, 0)},
    ],
)
def test_what_storage_would_change_is_refused_before_writing(bad: dict[str, object]) -> None:
    log = MemoryAuditLog()

    with pytest.raises((TypeError, ValueError)):
        log.append(event(**bad))
    assert log.records == []


def test_details_take_json_values_without_floats() -> None:
    check_event(event(details={"status": 200, "ok": True, "tags": ["a", None], "why": {"n": 1}}))
