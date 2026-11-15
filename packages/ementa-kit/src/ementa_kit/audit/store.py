"""Where the chain lives: an append-only Postgres table, or a list for tests."""

import threading
from collections.abc import Iterator
from typing import Any, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ementa_kit.audit.chain import (
    AuditEvent,
    AuditRecord,
    ChainCheck,
    chain_hash,
    check_event,
    verify_chain,
)


class AuditLog(Protocol):
    def append(self, event: AuditEvent) -> AuditRecord: ...

    def page(self, before: int | None = None, limit: int = 50) -> list[AuditRecord]:
        """Newest first; `before` is the id where the previous page stopped."""
        ...

    def verify(self) -> ChainCheck: ...


class MemoryAuditLog:
    """The same chain in a list, for tests and for processes without a database."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> AuditRecord:
        check_event(event)
        with self._lock:
            last = self.records[-1] if self.records else None
            prev = last.hash if last else None
            record = AuditRecord(last.id + 1 if last else 1, event, prev, chain_hash(prev, event))
            self.records.append(record)
        return record

    def page(self, before: int | None = None, limit: int = 50) -> list[AuditRecord]:
        with self._lock:
            newest_first = [r for r in reversed(self.records) if before is None or r.id < before]
        return newest_first[:limit]

    def verify(self) -> ChainCheck:
        with self._lock:
            records = list(self.records)
        return verify_chain(records)


# Rows can be added, never changed: a trigger refuses UPDATE, DELETE and TRUNCATE, even from the
# table owner, and the unique `prev_hash` (one null at most) keeps the chain from forking.
_SCHEMA = """
create schema if not exists {schema};
create table if not exists {schema}.events (
    id bigint generated always as identity primary key,
    occurred_at timestamptz not null,
    actor text,
    action text not null,
    resource text not null,
    data_class text not null,
    outcome text not null,
    details jsonb not null,
    prev_hash bytea unique nulls not distinct,
    hash bytea not null
);
create or replace function {schema}.refuse_change() returns trigger
language plpgsql as $$
begin
    raise exception '%.% is append-only: % is not allowed', tg_table_schema, tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end
$$;
create or replace trigger events_append_only
    before update or delete on {schema}.events
    for each row execute function {schema}.refuse_change();
create or replace trigger events_no_truncate
    before truncate on {schema}.events
    for each statement execute function {schema}.refuse_change();
"""

_COLUMNS = "id, occurred_at, actor, action, resource, data_class, outcome, details, prev_hash, hash"


def _record(row: dict[str, Any]) -> AuditRecord:
    event = AuditEvent(
        actor=row["actor"],
        action=row["action"],
        resource=row["resource"],
        data_class=row["data_class"],
        outcome=row["outcome"],
        details=row["details"],
        occurred_at=row["occurred_at"],
    )
    prev = row["prev_hash"]
    return AuditRecord(
        int(row["id"]), event, bytes(prev) if prev is not None else None, bytes(row["hash"])
    )


class PgAuditLog:
    def __init__(self, database_url: str, schema: str = "audit") -> None:
        self.database_url = database_url
        self.schema = schema

    def _sql(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(schema=sql.Identifier(self.schema))

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(self._sql(_SCHEMA))

    def append(self, event: AuditEvent) -> AuditRecord:
        check_event(event)
        with self._connect() as conn, conn.transaction():
            # One writer at a time per table, held until commit: two events never claim the
            # same predecessor. An advisory lock needs no privilege beyond INSERT.
            conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"{self.schema}.events",))
            last = conn.execute(
                self._sql("select hash from {schema}.events order by id desc limit 1")
            ).fetchone()
            prev = bytes(last["hash"]) if last else None
            digest = chain_hash(prev, event)
            row = conn.execute(
                self._sql(
                    "insert into {schema}.events (occurred_at, actor, action, resource, "
                    "data_class, outcome, details, prev_hash, hash) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning id"
                ),
                (
                    event.occurred_at,
                    event.actor,
                    event.action,
                    event.resource,
                    str(event.data_class),
                    str(event.outcome),
                    Jsonb(dict(event.details)),
                    prev,
                    digest,
                ),
            ).fetchone()
        assert row is not None
        return AuditRecord(int(row["id"]), event, prev, digest)

    def page(self, before: int | None = None, limit: int = 50) -> list[AuditRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(
                    f"select {_COLUMNS} from {{schema}}.events "
                    "where %(before)s::bigint is null or id < %(before)s "
                    "order by id desc limit %(limit)s"
                ),
                {"before": before, "limit": limit},
            ).fetchall()
        return [_record(row) for row in rows]

    def records(self, batch: int = 1000) -> Iterator[AuditRecord]:
        """Every record, oldest first, read in batches."""
        after = 0
        with self._connect() as conn:
            while True:
                rows = conn.execute(
                    self._sql(
                        f"select {_COLUMNS} from {{schema}}.events "
                        "where id > %s order by id limit %s"
                    ),
                    (after, batch),
                ).fetchall()
                if not rows:
                    return
                for row in rows:
                    yield _record(row)
                after = int(rows[-1]["id"])

    def verify(self) -> ChainCheck:
        return verify_chain(self.records())
