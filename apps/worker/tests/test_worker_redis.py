"""Upload to index through a real arq worker, Redis and Postgres, with a fake embedder.

Set EMENTA_TEST_DATABASE_URL and EMENTA_TEST_REDIS_URL to run; without them the tests skip.
"""

import asyncio
import hashlib
import math
import os
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import psycopg
import pytest
from arq import create_pool
from arq.worker import Worker, func
from psycopg import sql

from ementa.documents import (
    INGEST_JOB,
    DocumentIngestor,
    DocumentIntake,
    PgDocumentStore,
    Upload,
)
from ementa_kit.jobs import JobQueue, JobStatus, PgJobStore, redis_settings, worker_options
from ementa_kit.rag import PgHybridStore
from ementa_worker.worker import Resources, ingest_document

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("EMENTA_TEST_REDIS_URL")
DIM = 16

# Fictitious contract, written for the tests.
CONTRACT = """# Contrato de prestação de serviços

Contrato fictício entre Escritório Exemplo e Cliente Teste.

## Cláusula 1 - Honorários

Os honorários são mensais.
"""


class BagEmbedder:
    model_name = "fake-bag"
    dim = DIM

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * DIM
        for word in text.lower().split():
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class FlakyIngestor:
    """Fails its first `failures` tries, as a database that comes back would."""

    def __init__(self, ingestor: DocumentIngestor, failures: int) -> None:
        self.ingestor = ingestor
        self.failures = failures

    def ingest(self, document_id: str) -> int:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("database restarting")
        return self.ingestor.ingest(document_id)


@dataclass
class Env:
    queue: JobQueue
    documents: PgDocumentStore
    jobs: PgJobStore
    ingestor: DocumentIngestor


@pytest.fixture
def env() -> Iterator[Env]:
    if not (DATABASE_URL and REDIS_URL):
        pytest.skip("EMENTA_TEST_DATABASE_URL and EMENTA_TEST_REDIS_URL are needed")
    prefix = f"test_{uuid.uuid4().hex[:8]}"
    documents = PgDocumentStore(DATABASE_URL, schema=f"{prefix}_firm")
    jobs = PgJobStore(DATABASE_URL, schema=f"{prefix}_ai")
    chunks = PgHybridStore(DATABASE_URL, DIM, schema=f"{prefix}_firm_knowledge")
    for store in (documents, jobs, chunks):
        store.ensure_schema()
    yield Env(
        JobQueue(REDIS_URL, queue_name=f"{prefix}:queue"),
        documents,
        jobs,
        DocumentIngestor(documents, chunks, BagEmbedder()),
    )
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        for schema in (documents.schema, jobs.schema, chunks.schema):
            conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


async def drain(env: Env, ingestor: DocumentIngestor | FlakyIngestor) -> Worker:
    """Run an arq worker until the queue is empty, as `ementa-worker --burst` does."""
    assert REDIS_URL
    worker = Worker(
        functions=[func(ingest_document, name=INGEST_JOB)],
        **worker_options(REDIS_URL, env.queue.queue_name),
        burst=True,
        keep_result=0,
        poll_delay=0.05,
        ctx={"ementa": Resources(env.jobs, ingestor, retry_base_s=0.1)},
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()
    return worker


async def queued(env: Env) -> int:
    assert REDIS_URL
    redis = await create_pool(redis_settings(REDIS_URL))
    try:
        return int(await redis.zcard(env.queue.queue_name))
    finally:
        await redis.aclose()


def test_a_file_sent_twice_is_indexed_once(env: Env) -> None:
    intake = DocumentIntake(env.documents, env.jobs, env.queue)
    upload = Upload.accept("contrato.md", "text/markdown", CONTRACT.encode())

    async def scenario() -> None:
        first = await intake.submit(upload, "ana")
        again = await intake.submit(upload, "ana")
        assert (again.job.id, again.duplicate) == (first.job.id, True)
        assert await queued(env) == 1  # arq kept one job for the two sends

        worker = await drain(env, env.ingestor)
        assert (worker.jobs_complete, worker.jobs_failed, worker.jobs_retried) == (1, 0, 0)

        job = env.jobs.get(first.job.id)
        assert job is not None
        assert (job.status, job.attempts, job.result) == (JobStatus.SUCCEEDED, 1, {"chunks": 2})
        document = env.documents.get(first.document.id)
        assert document is not None and document.chunks == 2

        third = await intake.submit(upload, "ana")  # after it succeeded: nothing to queue
        assert third.job.status is JobStatus.SUCCEEDED
        assert await queued(env) == 0

    asyncio.run(scenario())


def test_a_failure_that_passes_is_retried_and_then_succeeds(env: Env) -> None:
    intake = DocumentIntake(env.documents, env.jobs, env.queue)
    upload = Upload.accept("contrato.md", "text/markdown", CONTRACT.encode())

    async def scenario() -> None:
        submission = await intake.submit(upload, "ana")
        worker = await drain(env, FlakyIngestor(env.ingestor, failures=1))

        assert (worker.jobs_complete, worker.jobs_retried) == (1, 1)
        job = env.jobs.get(submission.job.id)
        assert job is not None
        assert (job.status, job.attempts, job.result) == (JobStatus.SUCCEEDED, 2, {"chunks": 2})

    asyncio.run(scenario())
