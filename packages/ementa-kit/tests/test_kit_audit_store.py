"""The audit chain on a real Postgres. Set EMENTA_TEST_DATABASE_URL to run."""

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from ementa_kit.audit import AuditEvent, Outcome, PgAuditLog
from ementa_kit.audit.chain import CONTENT_CHANGED, LINK_BROKEN
from ementa_kit.policy import DataClass

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")


def event(n: int) -> AuditEvent:
    return AuditEvent(
        actor=f"u-{n}",
        action="http.request",
        resource="GET /v1/audit/events",
        data_class=DataClass.RESTRICTED,
        outcome=Outcome.OK,
        details={"status": 200, "duration_ms": n, "note": "ação"},
        occurred_at=datetime(2026, 11, 15, 12, 0, n, 123456, tzinfo=UTC),
    )


@pytest.fixture
def log() -> Iterator[PgAuditLog]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    log = PgAuditLog(DATABASE_URL, schema=f"test_audit_{uuid.uuid4().hex[:8]}")
    log.ensure_schema()
    log.ensure_schema()  # idempotent
    yield log
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(log.schema)))


def run(log: PgAuditLog, statement: str, *, bypass_trigger: bool = False) -> None:
    """Run SQL on the table directly, as an intruder with the owner's rights would."""
    assert DATABASE_URL
    table = sql.SQL("{}.events").format(sql.Identifier(log.schema))
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        if bypass_trigger:
            conn.execute(sql.SQL("alter table {} disable trigger user").format(table))
        conn.execute(sql.SQL(statement).format(table=table))


def test_events_come_back_as_written_and_the_chain_verifies(log: PgAuditLog) -> None:
    written = [log.append(event(n)) for n in range(1, 4)]

    page = log.page(limit=2)

    assert [r.id for r in page] == [written[2].id, written[1].id]
    assert page[0] == written[2]
    assert [r.id for r in log.page(before=written[1].id)] == [written[0].id]
    check = log.verify()
    assert check.intact
    assert (check.checked, check.head) == (3, written[2].hash)


@pytest.mark.parametrize(
    "statement",
    [
        "update {table} set actor = 'intruso'",
        "delete from {table}",
        "truncate {table}",
    ],
)
def test_the_table_refuses_change_even_from_its_owner(log: PgAuditLog, statement: str) -> None:
    log.append(event(1))

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        run(log, statement)
    assert log.verify().checked == 1


def test_a_row_changed_behind_the_trigger_is_reported(log: PgAuditLog) -> None:
    records = [log.append(event(n)) for n in range(1, 5)]

    tamper = f"update {{table}} set actor = 'intruso' where id = {records[2].id}"
    run(log, tamper, bypass_trigger=True)
    check = log.verify()

    assert check.broken is not None
    assert (check.broken.id, check.broken.reason) == (records[2].id, CONTENT_CHANGED)
    assert check.checked == 2


def test_a_row_deleted_behind_the_trigger_is_reported(log: PgAuditLog) -> None:
    records = [log.append(event(n)) for n in range(1, 5)]

    run(log, f"delete from {{table}} where id = {records[1].id}", bypass_trigger=True)
    check = log.verify()

    assert check.broken is not None
    assert (check.broken.id, check.broken.reason) == (records[2].id, LINK_BROKEN)


def test_concurrent_writers_keep_one_chain(log: PgAuditLog) -> None:
    def write(worker: int) -> None:
        for n in range(8):
            log.append(event(worker * 10 + n))

    threads = [threading.Thread(target=write, args=(w,)) for w in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check = log.verify()
    assert check.intact
    assert check.checked == 32


def test_the_chain_cannot_fork_even_without_the_lock(log: PgAuditLog) -> None:
    first = log.append(event(1))
    log.append(event(2))

    with pytest.raises(psycopg.errors.UniqueViolation):
        run(
            log,
            "insert into {table} (occurred_at, action, resource, data_class, outcome, details, "
            f"prev_hash, hash) values (now(), 'x', 'x', 'public', 'ok', '{{{{}}}}', "
            f"'\\x{first.hash.hex()}', '\\x00')",
        )
