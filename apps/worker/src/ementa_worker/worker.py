"""The arq worker: runs the queued jobs, the ingestion of firm documents (F3) and each leg of a
drafting run, its start or a resume after the lawyer's decision (F4).

Every job must be idempotent: arq runs a job at least once (again when a worker dies halfway) and
the API enqueues the same job again when a file is re-sent. A failure that may pass (database,
model download, a slow try) is retried with exponential backoff and jitter, up to `MAX_TRIES`
tries in all; a document that cannot be read fails at once. The state of each job lives in
Postgres (`ai.jobs`), which `GET /v1/jobs/{id}` reads; arq keeps no results.
"""

import asyncio
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from arq import Retry
from arq.worker import func
from opentelemetry import propagate
from opentelemetry.trace import SpanKind

from ementa import services
from ementa.documents import INGEST_JOB, DocumentRejectedError
from ementa_kit.config import Settings
from ementa_kit.hitl.runner import DRAFTING_JOB, DraftingRunError, Progress
from ementa_kit.jobs import worker_options
from ementa_kit.telemetry import get_logger, get_tracer

MAX_TRIES = 5
RETRY_BASE_S = 5.0  # the first retry waits 2.5 to 5 s, and each one after it twice as long
RETRY_CAP_S = 300.0
INGEST_TIMEOUT_S = 600.0  # one try; past it, the try counts as a failure that may pass
# A drafting leg has no time limit per try: each model call has its own, and a try cut by a
# timeout would keep running in its thread beside the retry. arq's limit is the last resort.
DRAFTING_TIMEOUT_S = 3600.0

tracer = get_tracer(__name__)
log = get_logger(__name__)


class JobLog(Protocol):
    """The part of `PgJobStore` a job writes to."""

    def start(self, job_id: str, attempt: int) -> None: ...

    def retry(self, job_id: str, attempt: int, error: str) -> None: ...

    def succeed(self, job_id: str, result: Mapping[str, Any]) -> None: ...

    def fail(self, job_id: str, error: str) -> None: ...


class Ingestor(Protocol):
    def ingest(self, document_id: str) -> int: ...


class Drafting(Protocol):
    """The part of `DraftingRunner` a drafting leg uses."""

    def advance(self, run_id: str, approval_id: str | None = None) -> Progress: ...

    def fail(self, run_id: str, error: str) -> None: ...


@dataclass(frozen=True)
class Resources:
    """What the jobs use, built once by `startup` and kept in the arq context."""

    jobs: JobLog
    ingestor: Ingestor
    max_tries: int = MAX_TRIES
    retry_base_s: float = RETRY_BASE_S
    ingest_timeout_s: float = INGEST_TIMEOUT_S
    drafting: Drafting | None = None


def retry_delay(
    attempt: int,
    base_s: float = RETRY_BASE_S,
    cap_s: float = RETRY_CAP_S,
    rand: Callable[[], float] = random.random,
) -> float:
    """Seconds before the try after `attempt`: base * 2^(attempt - 1), capped, keeping a random
    half to all of it, so jobs that failed together do not come back together."""
    delay = min(cap_s, base_s * 2.0 ** (attempt - 1))
    return delay / 2 + rand() * delay / 2


async def _settle(update: Callable[..., None], *args: Any) -> None:
    """Write the state on the error path, best effort: the retry or the failure must still
    reach arq when Postgres is the thing that failed."""
    try:
        await asyncio.to_thread(update, *args)
    except Exception as error:
        log.warning("job_state_not_saved", error=type(error).__name__)


async def ingest_document(
    ctx: dict[str, Any], document_id: str, trace_context: dict[str, str] | None = None
) -> dict[str, int]:
    """Index one firm document (`DocumentIngestor.ingest`) as the job `ctx["job_id"]`.

    The span continues the trace of the request that queued the job.
    """
    resources: Resources = ctx["ementa"]
    job_id = str(ctx["job_id"])
    attempt = int(ctx["job_try"])
    with tracer.start_as_current_span(
        f"process {INGEST_JOB}",
        context=propagate.extract(trace_context or {}),
        kind=SpanKind.CONSUMER,
        attributes={
            "messaging.system": "arq",
            "messaging.operation.type": "process",
            "messaging.message.id": job_id,
            "ementa.job.kind": INGEST_JOB,
            "ementa.job.attempt": attempt,
            "ementa.document.id": document_id,
        },
    ) as span:
        try:
            await asyncio.to_thread(resources.jobs.start, job_id, attempt)
            chunks = await asyncio.wait_for(
                asyncio.to_thread(resources.ingestor.ingest, document_id),
                resources.ingest_timeout_s,
            )
            result = {"chunks": chunks}
            await asyncio.to_thread(resources.jobs.succeed, job_id, result)
        except DocumentRejectedError as error:  # another try would read the same bytes
            span.set_attribute("ementa.job.outcome", "failed")
            await _settle(resources.jobs.fail, job_id, str(error))
            raise
        except Exception as error:
            reason = type(error).__name__  # the class only: a message may quote the document
            if attempt >= resources.max_tries:
                span.set_attribute("ementa.job.outcome", "failed")
                await _settle(resources.jobs.fail, job_id, f"{reason} after {attempt} tries")
                raise
            delay = retry_delay(attempt, resources.retry_base_s)
            span.set_attributes({"ementa.job.outcome": "retry", "ementa.job.retry_in_s": delay})
            await _settle(resources.jobs.retry, job_id, attempt, reason)
            raise Retry(defer=delay) from error
        span.set_attributes({"ementa.job.outcome": "succeeded", "ementa.document.chunks": chunks})
        return result


async def run_drafting(
    ctx: dict[str, Any],
    run_id: str,
    approval_id: str | None = None,
    trace_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One leg of a drafting run as the job `ctx["job_id"]`: the start (`approval_id` None) or
    the resume with the decision on `approval_id` (`DraftingRunner.advance`).

    A run that cannot go on (`DraftingRunError`) fails at once; the runner says whether the run
    fails with it. Any other error is retried, and on the last try the run fails too.
    """
    resources: Resources = ctx["ementa"]
    job_id = str(ctx["job_id"])
    attempt = int(ctx["job_try"])
    attributes: dict[str, str | int] = {
        "messaging.system": "arq",
        "messaging.operation.type": "process",
        "messaging.message.id": job_id,
        "ementa.job.kind": DRAFTING_JOB,
        "ementa.job.attempt": attempt,
        "ementa.run.id": run_id,
    }
    if approval_id is not None:
        attributes["ementa.approval.id"] = approval_id
    with tracer.start_as_current_span(
        f"process {DRAFTING_JOB}",
        context=propagate.extract(trace_context or {}),
        kind=SpanKind.CONSUMER,
        attributes=attributes,
    ) as span:
        drafting = resources.drafting
        try:
            if drafting is None:
                raise DraftingRunError("This worker runs no drafting agent.")
            await asyncio.to_thread(resources.jobs.start, job_id, attempt)
            progress = await asyncio.to_thread(drafting.advance, run_id, approval_id)
            result = progress.job_result()
            await asyncio.to_thread(resources.jobs.succeed, job_id, result)
        except DraftingRunError as error:  # another try would stop at the same place
            span.set_attribute("ementa.job.outcome", "failed")
            await _settle(resources.jobs.fail, job_id, str(error))
            raise
        except Exception as error:
            reason = type(error).__name__  # the class only: a message may quote the draft
            if attempt >= resources.max_tries:
                span.set_attribute("ementa.job.outcome", "failed")
                failure = f"{reason} after {attempt} tries"
                await _settle(resources.jobs.fail, job_id, failure)
                if drafting is not None:
                    await _settle(drafting.fail, run_id, failure)
                raise
            delay = retry_delay(attempt, resources.retry_base_s)
            span.set_attributes({"ementa.job.outcome": "retry", "ementa.job.retry_in_s": delay})
            await _settle(resources.jobs.retry, job_id, attempt, reason)
            raise Retry(defer=delay) from error
        span.set_attributes(
            {"ementa.job.outcome": "succeeded", "ementa.run.status": str(progress.run.status)}
        )
        return result


async def startup(ctx: dict[str, Any]) -> None:
    """Builds the resources of the jobs; the embedding model loads now, not on the first job,
    and the drafting runner shares it for the episodic memory."""
    ingestor = await asyncio.to_thread(services.document_ingestor)
    drafting = await asyncio.to_thread(services.drafting_runner)
    ctx["ementa"] = Resources(jobs=services.job_store(), ingestor=ingestor, drafting=drafting)


def worker_settings(settings: Settings) -> dict[str, Any]:
    """Arguments of the arq `Worker` that `ementa-worker` runs."""
    return {
        **worker_options(settings.redis_url),
        "functions": [
            func(ingest_document, name=INGEST_JOB, timeout=INGEST_TIMEOUT_S + 60),
            func(run_drafting, name=DRAFTING_JOB, timeout=DRAFTING_TIMEOUT_S),
        ],
        "on_startup": startup,
        # One spare try for a job cut by a shutdown, so the job itself has the last word.
        "max_tries": MAX_TRIES + 1,
        # Embedding is CPU bound: one document at a time. A drafting leg takes the same slot
        # while it waits on the model; a second worker process takes the load off.
        "max_jobs": 1,
        "keep_result": 0,  # the state is in Postgres; a kept result would block a re-send
        "health_check_interval": 60,
    }
