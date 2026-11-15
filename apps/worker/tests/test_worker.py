"""The ingestion job with fakes: states, retries with backoff, failures and the span."""

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import pytest
from arq import Retry
from opentelemetry import propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from ementa.documents import DocumentRejectedError
from ementa_kit.config import Settings
from ementa_kit.jobs import QUEUE_NAME, dump_job, load_job
from ementa_worker import worker
from ementa_worker.worker import (
    MAX_TRIES,
    Resources,
    ingest_document,
    retry_delay,
    worker_settings,
)


class FakeJobs:
    def __init__(self, broken: bool = False) -> None:
        self.broken = broken
        self.events: list[tuple[Any, ...]] = []

    def _write(self, *event: Any) -> None:
        if self.broken and event[0] != "start":
            raise ConnectionError("database down")
        self.events.append(event)

    def start(self, job_id: str, attempt: int) -> None:
        self._write("start", job_id, attempt)

    def retry(self, job_id: str, attempt: int, error: str) -> None:
        self._write("retry", job_id, attempt, error)

    def succeed(self, job_id: str, result: Mapping[str, Any]) -> None:
        self._write("succeed", job_id, dict(result))

    def fail(self, job_id: str, error: str) -> None:
        self._write("fail", job_id, error)


class FakeIngestor:
    def __init__(self, outcome: int | Exception, seconds: float = 0.0) -> None:
        self.outcome = outcome
        self.seconds = seconds
        self.documents: list[str] = []

    def ingest(self, document_id: str) -> int:
        self.documents.append(document_id)
        time.sleep(self.seconds)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def run(
    ingestor: FakeIngestor, attempt: int = 1, jobs: FakeJobs | None = None, **kwargs: Any
) -> FakeJobs:
    jobs = jobs or FakeJobs()
    ctx = {"job_id": "job-1", "job_try": attempt, "ementa": Resources(jobs, ingestor, **kwargs)}
    asyncio.run(ingest_document(ctx, "doc-1"))
    return jobs


def test_job_indexes_the_document_and_records_the_result() -> None:
    ingestor = FakeIngestor(7)

    jobs = run(ingestor)

    assert ingestor.documents == ["doc-1"]
    assert jobs.events == [("start", "job-1", 1), ("succeed", "job-1", {"chunks": 7})]


def test_a_failure_that_may_pass_is_retried_with_backoff() -> None:
    jobs = FakeJobs()

    with pytest.raises(Retry) as raised:
        run(FakeIngestor(ConnectionError("database down")), attempt=2, jobs=jobs)

    assert jobs.events == [("start", "job-1", 2), ("retry", "job-1", 2, "ConnectionError")]
    delay_ms = raised.value.defer_score
    assert delay_ms is not None and 5_000 <= delay_ms <= 10_000  # second try: 5 to 10 s


def test_the_last_try_fails_the_job_with_the_error_class_only() -> None:
    jobs = FakeJobs()

    with pytest.raises(ConnectionError):
        run(FakeIngestor(ConnectionError("texto do contrato")), attempt=MAX_TRIES, jobs=jobs)

    assert jobs.events[-1] == ("fail", "job-1", f"ConnectionError after {MAX_TRIES} tries")


def test_a_document_that_cannot_be_read_fails_at_once() -> None:
    jobs = FakeJobs()
    error = DocumentRejectedError("unreadable", "The PDF could not be read.")

    with pytest.raises(DocumentRejectedError):
        run(FakeIngestor(error), attempt=1, jobs=jobs)

    assert jobs.events[-1] == ("fail", "job-1", "The PDF could not be read.")


def test_a_try_past_its_time_limit_is_retried() -> None:
    jobs = FakeJobs()

    with pytest.raises(Retry):
        run(FakeIngestor(3, seconds=0.5), jobs=jobs, ingest_timeout_s=0.05)

    assert jobs.events[-1] == ("retry", "job-1", 1, "TimeoutError")


def test_a_state_that_cannot_be_saved_does_not_hide_the_retry() -> None:
    with pytest.raises(Retry):
        run(FakeIngestor(3), jobs=FakeJobs(broken=True))  # succeed fails: try again


def test_retry_delay_doubles_with_jitter_up_to_a_cap() -> None:
    lowest = [retry_delay(n, rand=lambda: 0.0) for n in (1, 2, 3, 4)]
    highest = [retry_delay(n, rand=lambda: 1.0) for n in (1, 2, 3, 4)]

    assert lowest == [2.5, 5.0, 10.0, 20.0]
    assert highest == [5.0, 10.0, 20.0, 40.0]
    assert retry_delay(12, rand=lambda: 1.0) == 300.0


def test_job_span_continues_the_trace_of_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(worker, "tracer", provider.get_tracer("test"))
    carrier: dict[str, str] = {}
    with provider.get_tracer("test").start_as_current_span("send ingest_document") as producer:
        propagate.inject(carrier)
    ctx = {"job_id": "job-1", "job_try": 1, "ementa": Resources(FakeJobs(), FakeIngestor(4))}

    asyncio.run(ingest_document(ctx, "doc-1", trace_context=carrier))

    [span] = [s for s in exporter.get_finished_spans() if s.name == "process ingest_document"]
    assert span.kind is SpanKind.CONSUMER
    assert span.parent is not None
    assert span.parent.span_id == producer.get_span_context().span_id
    assert span.context.trace_id == producer.get_span_context().trace_id
    assert dict(span.attributes or {}) == {
        "messaging.system": "arq",
        "messaging.operation.type": "process",
        "messaging.message.id": "job-1",
        "ementa.job.kind": "ingest_document",
        "ementa.job.attempt": 1,
        "ementa.document.id": "doc-1",
        "ementa.job.outcome": "succeeded",
        "ementa.document.chunks": 4,
    }


def test_worker_reads_json_jobs_from_the_ementa_queue_and_keeps_no_result() -> None:
    settings = Settings(_env_file=None, redis_url="redis://fila.interna:6380/2")  # type: ignore[call-arg]
    options = worker_settings(settings)

    assert (options["queue_name"], options["keep_result"]) == (QUEUE_NAME, 0)
    assert (options["job_serializer"], options["job_deserializer"]) == (dump_job, load_job)
    [function] = options["functions"]
    assert function.name == "ingest_document"
    assert options["max_tries"] == MAX_TRIES + 1
    redis = options["redis_settings"]
    assert (redis.host, redis.port, redis.database) == ("fila.interna", 6380, 2)
