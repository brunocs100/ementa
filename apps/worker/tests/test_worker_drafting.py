"""The drafting leg with a fake runner: the job states, retries, failures and the span."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from arq import Retry
from opentelemetry import propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from ementa_kit.hitl.runner import DraftingRunError, Progress
from ementa_kit.hitl.store import Run, RunStatus
from ementa_worker import worker
from ementa_worker.worker import MAX_TRIES, Resources, run_drafting

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
RUN = Run(
    id="run-1",
    agent="brief_drafting",
    requested_by="u-associado",
    input={"facts": "Fatos fictícios."},
    status=RunStatus.FINALIZED,
    result={"text": "Peça fictícia."},
    error=None,
    created_at=NOW,
    updated_at=NOW,
    finished_at=NOW,
)


class FakeJobs:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def start(self, job_id: str, attempt: int) -> None:
        self.events.append(("start", job_id, attempt))

    def retry(self, job_id: str, attempt: int, error: str) -> None:
        self.events.append(("retry", job_id, attempt, error))

    def succeed(self, job_id: str, result: Mapping[str, Any]) -> None:
        self.events.append(("succeed", job_id, dict(result)))

    def fail(self, job_id: str, error: str) -> None:
        self.events.append(("fail", job_id, error))


class NoIngestor:
    def ingest(self, document_id: str) -> int:
        raise AssertionError("not used here")


class FakeDrafting:
    def __init__(self, outcome: Progress | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str | None]] = []
        self.failed: list[tuple[str, str]] = []

    def advance(self, run_id: str, approval_id: str | None = None) -> Progress:
        self.calls.append((run_id, approval_id))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def fail(self, run_id: str, error: str) -> None:
        self.failed.append((run_id, error))


def run(
    drafting: FakeDrafting | None,
    attempt: int = 1,
    approval_id: str | None = None,
    trace_context: dict[str, str] | None = None,
) -> FakeJobs:
    jobs = FakeJobs()
    resources = Resources(jobs, NoIngestor(), drafting=drafting)
    ctx = {"job_id": "job-1", "job_try": attempt, "ementa": resources}
    asyncio.run(run_drafting(ctx, "run-1", approval_id, trace_context))
    return jobs


def test_a_leg_moves_the_run_and_records_where_it_stopped() -> None:
    drafting = FakeDrafting(Progress(RUN, []))

    jobs = run(drafting, approval_id="approval-1")

    assert drafting.calls == [("run-1", "approval-1")]
    assert jobs.events == [
        ("start", "job-1", 1),
        ("succeed", "job-1", {"run_id": "run-1", "status": "finalized", "approvals": []}),
    ]


def test_a_failure_that_may_pass_is_retried_and_the_run_waits() -> None:
    drafting = FakeDrafting(ConnectionError("database restarting"))
    jobs = FakeJobs()
    ctx = {
        "job_id": "job-1",
        "job_try": 2,
        "ementa": Resources(jobs, NoIngestor(), drafting=drafting),
    }

    with pytest.raises(Retry) as raised:
        asyncio.run(run_drafting(ctx, "run-1"))

    assert jobs.events[-1] == ("retry", "job-1", 2, "ConnectionError")
    delay_ms = raised.value.defer_score
    assert delay_ms is not None and 5_000 <= delay_ms <= 10_000
    assert drafting.failed == []


def test_the_last_try_fails_the_job_and_the_run_with_the_error_class_only() -> None:
    drafting = FakeDrafting(ConnectionError("Maria Souza, CPF 123.456.789-09"))

    with pytest.raises(ConnectionError):
        run(drafting, attempt=MAX_TRIES)

    failure = f"ConnectionError after {MAX_TRIES} tries"
    assert drafting.failed == [("run-1", failure)]


def test_a_run_that_cannot_go_on_fails_the_job_at_once() -> None:
    error = DraftingRunError(
        "The agent ended without asking for approval, so nothing was finalized."
    )
    drafting = FakeDrafting(error)

    with pytest.raises(DraftingRunError):
        run(drafting)

    assert drafting.failed == []  # the runner already settled the run


def test_a_worker_without_drafting_agents_fails_the_leg() -> None:
    with pytest.raises(DraftingRunError, match="no drafting agent"):
        run(None)


def test_the_leg_span_continues_the_trace_of_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(worker, "tracer", provider.get_tracer("test"))
    carrier: dict[str, str] = {}
    with provider.get_tracer("test").start_as_current_span("send run_drafting") as producer:
        propagate.inject(carrier)

    run(FakeDrafting(Progress(RUN, [])), approval_id="approval-1", trace_context=carrier)

    [span] = [s for s in exporter.get_finished_spans() if s.name == "process run_drafting"]
    assert span.kind is SpanKind.CONSUMER
    assert span.parent is not None
    assert span.parent.span_id == producer.get_span_context().span_id
    assert dict(span.attributes or {}) == {
        "messaging.system": "arq",
        "messaging.operation.type": "process",
        "messaging.message.id": "job-1",
        "ementa.job.kind": "run_drafting",
        "ementa.job.attempt": 1,
        "ementa.run.id": "run-1",
        "ementa.approval.id": "approval-1",
        "ementa.job.outcome": "succeeded",
        "ementa.run.status": "finalized",
    }
