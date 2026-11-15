"""Job state on a real Postgres and the queue on a real Redis.

Set EMENTA_TEST_DATABASE_URL and EMENTA_TEST_REDIS_URL to run the integration tests.
"""

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from arq import create_pool
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from psycopg import sql

from ementa_kit import jobs
from ementa_kit.jobs import (
    JobQueue,
    JobStatus,
    PgJobStore,
    QueueUnavailableError,
    dump_job,
    load_job,
    redis_settings,
)

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("EMENTA_TEST_REDIS_URL")


@pytest.fixture
def store() -> Iterator[PgJobStore]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    store = PgJobStore(DATABASE_URL, schema=f"test_{uuid.uuid4().hex[:8]}")
    store.ensure_schema()
    store.ensure_schema()
    yield store
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(store.schema)))


@pytest.mark.integration
def test_submit_keeps_one_job_per_kind_and_subject(store: PgJobStore) -> None:
    first = store.submit("ingest_document", "doc-1", "ana")
    again = store.submit("ingest_document", "doc-1", "ana")
    other = store.submit("ingest_document", "doc-2", "ana")

    assert again == first
    assert (first.status, first.attempts, first.requested_by) == (JobStatus.QUEUED, 0, "ana")
    assert other.id != first.id
    assert store.get(first.id) == first
    assert store.get("no-such-job") is None


@pytest.mark.integration
def test_a_job_goes_from_queued_to_succeeded_through_a_retry(store: PgJobStore) -> None:
    job = store.submit("ingest_document", "doc-1", "ana")

    store.start(job.id, 1)
    running = store.get(job.id)
    store.retry(job.id, 1, "OperationalError")
    retrying = store.get(job.id)
    store.start(job.id, 2)
    store.succeed(job.id, {"chunks": 3})
    done = store.get(job.id)

    assert running is not None and retrying is not None and done is not None
    assert (running.status, running.attempts, running.finished_at) == (JobStatus.RUNNING, 1, None)
    assert running.started_at is not None
    assert (retrying.status, retrying.error) == (JobStatus.RETRYING, "OperationalError")
    assert (done.status, done.attempts, done.result, done.error) == (
        JobStatus.SUCCEEDED,
        2,
        {"chunks": 3},
        None,
    )
    assert done.started_at == running.started_at  # the first try's start
    assert done.finished_at is not None


@pytest.mark.integration
def test_a_failed_job_goes_back_to_the_queue_when_submitted_again(store: PgJobStore) -> None:
    job = store.submit("ingest_document", "doc-1", "ana")
    store.start(job.id, 5)
    store.fail(job.id, "OperationalError after 5 tries")

    failed = store.get(job.id)
    requeued = store.submit("ingest_document", "doc-1", "ana")

    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.finished_at is not None
    assert requeued.id == job.id
    assert (requeued.status, requeued.attempts, requeued.error) == (JobStatus.QUEUED, 0, None)
    assert (requeued.started_at, requeued.finished_at) == (None, None)


def test_payloads_are_json_and_never_pickle() -> None:
    payload: dict[str, Any] = {"f": "ingest_document", "a": [], "k": {"document_id": "d"}, "et": 1}

    assert load_job(dump_job(payload)) == payload
    assert dump_job({"r": ValueError("x")}) == b'{"r": "x"}'
    with pytest.raises(ValueError):
        load_job(b"[1, 2]")


def test_unreachable_redis_is_queue_unavailable() -> None:
    queue = JobQueue("redis://127.0.0.1:1/0")

    with pytest.raises(QueueUnavailableError):
        asyncio.run(queue.enqueue("ingest_document", "job-1", document_id="doc-1"))


@pytest.mark.integration
def test_enqueue_twice_holds_one_json_job_with_the_trace_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not REDIS_URL:
        pytest.skip("EMENTA_TEST_REDIS_URL not set")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(jobs, "tracer", provider.get_tracer("test"))
    queue = JobQueue(REDIS_URL, queue_name=f"test:{uuid.uuid4().hex[:8]}")
    job_id = str(uuid.uuid4())

    async def scenario() -> tuple[bool, bool, bytes | None, int]:
        with provider.get_tracer("test").start_as_current_span("POST /v1/documents"):
            first = await queue.enqueue("ingest_document", job_id, document_id="doc-1")
            second = await queue.enqueue("ingest_document", job_id, document_id="doc-1")
        redis = await create_pool(redis_settings(REDIS_URL))
        try:
            raw = await redis.get(f"arq:job:{job_id}")
            queued = await redis.zcard(queue.queue_name)
            await redis.delete(f"arq:job:{job_id}", queue.queue_name)
        finally:
            await redis.aclose()
        return first, second, raw, queued

    first, second, raw, queued = asyncio.run(scenario())

    assert (first, second, queued) == (True, False, 1)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["f"] == "ingest_document"
    sends = [s for s in exporter.get_finished_spans() if s.name == "send ingest_document"]
    assert [s.kind for s in sends] == [SpanKind.PRODUCER, SpanKind.PRODUCER]
    assert [(s.attributes or {}).get("ementa.job.enqueued") for s in sends] == [True, False]
    trace_id = format(sends[0].context.trace_id, "032x")
    assert payload["k"]["document_id"] == "doc-1"
    assert trace_id in payload["k"]["trace_context"]["traceparent"]
