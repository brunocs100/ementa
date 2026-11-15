"""Background jobs: an arq queue on Redis, with the state of each job kept in Postgres.

Redis holds what is waiting to run; Postgres (`ai.jobs`) holds what happened to each job, so the
state survives a Redis restart and `GET /v1/jobs/{id}` reads one table. There is one job per kind
and subject (a document, say): submitting it again returns the same job, and a failed one goes
back to the queue. arq refuses a second job under an id it already holds, so enqueueing the same
job twice runs it once. Job arguments travel as JSON, never pickle, arq's default: whoever can
write to Redis must not be able to run code in the worker.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from arq import create_pool
from arq.connections import RedisSettings
from opentelemetry import propagate
from opentelemetry.trace import SpanKind
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from redis.exceptions import RedisError

from ementa_kit.telemetry import get_tracer

QUEUE_NAME = "ementa:queue"

tracer = get_tracer(__name__)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"  # failed and waiting for the next try
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # for good: tries exhausted or an input that cannot work


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    subject: str  # what the job works on, such as a document id
    requested_by: str
    status: JobStatus
    attempts: int
    error: str | None
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class QueueUnavailableError(RuntimeError):
    """Redis did not take the job; the job stays queued in Postgres and can be sent again."""


_SCHEMA = """
create schema if not exists {schema};
create table if not exists {schema}.jobs (
    id text primary key,
    kind text not null,
    subject text not null,
    requested_by text not null,
    status text not null
        check (status in ('queued', 'running', 'retrying', 'succeeded', 'failed')),
    attempts integer not null default 0,
    error text,
    result jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz,
    unique (kind, subject)
);
"""

_COLUMNS = (
    "id, kind, subject, requested_by, status, attempts, error, result, created_at, updated_at, "
    "started_at, finished_at"
)


def _job(row: Mapping[str, Any]) -> Job:
    return Job(**{**row, "status": JobStatus(row["status"])})


class PgJobStore:
    """`ai.jobs` on Postgres: the state of every background job."""

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

    def submit(self, kind: str, subject: str, requested_by: str) -> Job:
        """The job of `kind` for `subject`: created queued when there is none, put back in the
        queue when it failed, returned as it is otherwise."""
        with self._connect() as conn, conn.transaction():
            conn.execute(
                self._sql(
                    "insert into {schema}.jobs (id, kind, subject, requested_by, status) "
                    "values (%s, %s, %s, %s, 'queued') on conflict (kind, subject) do nothing"
                ),
                (str(uuid.uuid4()), kind, subject, requested_by),
            )
            conn.execute(
                self._sql(
                    "update {schema}.jobs set status = 'queued', attempts = 0, error = null, "
                    "result = null, started_at = null, finished_at = null, updated_at = now() "
                    "where kind = %s and subject = %s and status = 'failed'"
                ),
                (kind, subject),
            )
            row = conn.execute(
                self._sql(
                    f"select {_COLUMNS} from {{schema}}.jobs where kind = %s and subject = %s"
                ),
                (kind, subject),
            ).fetchone()
        assert row is not None  # inserted or found in the same transaction
        return _job(row)

    def get(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute(
                self._sql(f"select {_COLUMNS} from {{schema}}.jobs where id = %s"), (job_id,)
            ).fetchone()
        return _job(row) if row else None

    def _update(self, job_id: str, assignments: str, params: tuple[object, ...]) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    f"update {{schema}}.jobs set {assignments}, updated_at = now() where id = %s"
                ),
                (*params, job_id),
            )

    def start(self, job_id: str, attempt: int) -> None:
        self._update(
            job_id,
            "status = 'running', attempts = %s, started_at = coalesce(started_at, now())",
            (attempt,),
        )

    def retry(self, job_id: str, attempt: int, error: str) -> None:
        self._update(job_id, "status = 'retrying', attempts = %s, error = %s", (attempt, error))

    def succeed(self, job_id: str, result: Mapping[str, Any]) -> None:
        self._update(
            job_id,
            "status = 'succeeded', result = %s, error = null, finished_at = now()",
            (Jsonb(dict(result)),),
        )

    def fail(self, job_id: str, error: str) -> None:
        self._update(job_id, "status = 'failed', error = %s, finished_at = now()", (error,))


def dump_job(data: dict[str, Any]) -> bytes:
    """arq payloads as JSON. What JSON cannot hold (an exception in a result) becomes text."""
    return json.dumps(data, default=str).encode()


def load_job(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("an arq payload is a JSON object")
    return data


def redis_settings(redis_url: str, conn_retries: int = 5) -> RedisSettings:
    return replace(RedisSettings.from_dsn(redis_url), conn_retries=conn_retries)


def worker_options(redis_url: str, queue_name: str = QUEUE_NAME) -> dict[str, Any]:
    """Arguments of an arq `Worker` that reads the jobs `JobQueue` sends."""
    return {
        "redis_settings": redis_settings(redis_url),
        "queue_name": queue_name,
        "job_serializer": dump_job,
        "job_deserializer": load_job,
    }


class JobQueue:
    """Sends jobs to the arq queue that `ementa-worker` reads, one connection per call."""

    def __init__(self, redis_url: str, queue_name: str = QUEUE_NAME) -> None:
        self.redis_url = redis_url
        self.queue_name = queue_name

    async def enqueue(self, kind: str, job_id: str, **kwargs: Any) -> bool:
        """Queue `kind(**kwargs)` as `job_id`, with the trace context in `trace_context`.

        Returns False when arq already holds a job with that id, queued or running: nothing is
        added. Raises `QueueUnavailableError` when Redis cannot be reached.
        """
        with tracer.start_as_current_span(
            f"send {kind}",
            kind=SpanKind.PRODUCER,
            attributes={
                "messaging.system": "arq",
                "messaging.operation.type": "send",
                "messaging.destination.name": self.queue_name,
                "messaging.message.id": job_id,
                "ementa.job.kind": kind,
            },
        ) as span:
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            try:
                redis = await create_pool(
                    redis_settings(self.redis_url, conn_retries=0),
                    job_serializer=dump_job,
                    job_deserializer=load_job,
                    default_queue_name=self.queue_name,
                )
                try:
                    job = await redis.enqueue_job(
                        kind, _job_id=job_id, trace_context=carrier, **kwargs
                    )
                finally:
                    await redis.aclose()
            except (RedisError, OSError) as error:  # TimeoutError is an OSError
                raise QueueUnavailableError(f"Redis did not take the job: {error}") from error
            span.set_attribute("ementa.job.enqueued", job is not None)
            return job is not None
