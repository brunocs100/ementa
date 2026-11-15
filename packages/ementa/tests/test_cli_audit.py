import os
import uuid
from collections.abc import Iterator
from dataclasses import replace

import psycopg
import pytest
from psycopg import sql

from ementa import cli, services
from ementa_kit.audit import AuditEvent, AuditLog, MemoryAuditLog, PgAuditLog

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")


def event(n: int) -> AuditEvent:
    return AuditEvent(f"u-{n}", "http.request", "POST /v1/legal-research", "public", "ok")


def verify(log: AuditLog, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(cli, "setup", lambda settings: None)
    monkeypatch.setattr(services, "audit_log", lambda: log)
    return cli.main(["audit", "verify"])


def test_verify_reports_an_intact_chain_and_its_head(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = MemoryAuditLog()
    for n in range(3):
        log.append(event(n))

    assert verify(log, monkeypatch) == 0
    assert capsys.readouterr().out == (
        f"audit.events: 3 events, chain intact; head {log.records[-1].hash.hex()}\n"
    )


def test_verify_reports_the_first_broken_link(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = MemoryAuditLog()
    for n in range(3):
        log.append(event(n))
    second = log.records[1]
    log.records[1] = replace(second, event=replace(second.event, outcome="denied"))

    assert verify(log, monkeypatch) == 1
    out = capsys.readouterr().out
    assert out.startswith("audit.events: chain broken at event 2: hash does not match the content")
    assert "(1 events before it verified)" in out


@pytest.fixture
def pg_log() -> Iterator[PgAuditLog]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    log = PgAuditLog(DATABASE_URL, schema=f"test_audit_{uuid.uuid4().hex[:8]}")
    log.ensure_schema()
    yield log
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(log.schema)))


@pytest.mark.integration
def test_verify_catches_a_row_changed_behind_the_trigger(
    pg_log: PgAuditLog, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert DATABASE_URL
    records = [pg_log.append(event(n)) for n in range(4)]
    assert verify(pg_log, monkeypatch) == 0

    table = sql.SQL("{}.events").format(sql.Identifier(pg_log.schema))
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("alter table {} disable trigger user").format(table))
        conn.execute(
            sql.SQL("update {} set outcome = 'denied' where id = %s").format(table),
            (records[2].id,),
        )

    assert verify(pg_log, monkeypatch) == 1
    assert f"chain broken at event {records[2].id}" in capsys.readouterr().out
