"""Firm documents on a real Postgres: storage, ingestion and the intake. Set
EMENTA_TEST_DATABASE_URL to run. The queue is a fake that behaves like arq."""

import asyncio
import hashlib
import math
import os
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from ementa.documents import (
    INGEST_JOB,
    DocumentIngestor,
    DocumentIntake,
    DocumentRejectedError,
    PgDocumentStore,
    Upload,
)
from ementa_kit.jobs import JobQueue, JobStatus, PgJobStore
from ementa_kit.policy import DataClass
from ementa_kit.rag import PgHybridStore

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")
DIM = 16

# Fictitious contract, written for the tests.
CONTRACT = """# Contrato de locação

Contrato fictício entre Locadora Exemplo e Locatário Teste.

## Cláusula 1 - Prazo

O prazo da locação é de 30 meses.

## Cláusula 2 - Multa

A multa por rescisão antecipada é de três aluguéis.
"""


class BagEmbedder:
    """Bag of hashed words, counting what it embeds: enough to test the SQL."""

    model_name = "fake-bag"
    dim = DIM

    def __init__(self) -> None:
        self.embedded = 0

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * DIM
        for word in text.lower().split():
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return [self._one(t) for t in texts]


class FakeQueue(JobQueue):
    """Holds job ids like arq: a second job under an id it holds is refused."""

    def __init__(self) -> None:
        super().__init__("redis://unused")
        self.held: dict[str, dict[str, Any]] = {}
        self.sent: list[str] = []

    async def enqueue(self, kind: str, job_id: str, **kwargs: Any) -> bool:
        self.sent.append(job_id)
        if job_id in self.held:
            return False
        self.held[job_id] = {"kind": kind, **kwargs}
        return True


@dataclass
class Stores:
    documents: PgDocumentStore
    jobs: PgJobStore
    chunks: PgHybridStore


@pytest.fixture
def stores() -> Iterator[Stores]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    prefix = f"test_{uuid.uuid4().hex[:8]}"
    stores = Stores(
        PgDocumentStore(DATABASE_URL, schema=f"{prefix}_firm"),
        PgJobStore(DATABASE_URL, schema=f"{prefix}_ai"),
        PgHybridStore(DATABASE_URL, DIM, schema=f"{prefix}_firm_knowledge"),
    )
    stores.documents.ensure_schema()
    stores.jobs.ensure_schema()
    stores.chunks.ensure_schema()
    yield stores
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        for schema in (stores.documents.schema, stores.jobs.schema, stores.chunks.schema):
            conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


def upload(text: str = CONTRACT, filename: str = "contrato.md") -> Upload:
    return Upload.accept(filename, "text/markdown", text.encode())


def test_same_file_from_the_same_user_is_one_restricted_document(stores: Stores) -> None:
    first, created = stores.documents.register(upload(), "ana")
    again, created_again = stores.documents.register(upload(filename="outro-nome.md"), "ana")
    other_user, _ = stores.documents.register(upload(), "bruno")

    assert (created, created_again) == (True, False)
    assert again == first
    assert other_user.id != first.id  # another uploader: another document, another owner
    assert (first.uploaded_by, first.owner_id, first.acl) == ("ana", "ana", ["ana"])
    assert first.secrecy_level is DataClass.RESTRICTED
    assert (first.filename, first.size_bytes, first.chunks) == (
        "contrato.md",
        len(CONTRACT.encode()),
        None,
    )
    assert stores.documents.content(first.id) == CONTRACT.encode()
    assert stores.documents.get("not-a-uuid") is None
    assert stores.documents.get(str(uuid.uuid4())) is None


def test_ingest_writes_restricted_chunks_once(stores: Stores) -> None:
    document, _ = stores.documents.register(upload(), "ana")
    embedder = BagEmbedder()
    ingestor = DocumentIngestor(stores.documents, stores.chunks, embedder)

    written = ingestor.ingest(document.id)
    again = ingestor.ingest(document.id)

    assert written == again == 3
    assert embedder.embedded == 3  # the second run found the document indexed
    indexed = stores.documents.get(document.id)
    assert indexed is not None and indexed.chunks == 3 and indexed.indexed_at is not None
    with psycopg.connect(DATABASE_URL or "", row_factory=dict_row) as conn:
        rows = conn.execute(
            sql.SQL(
                "select id, source_id, title, path, secrecy_level, acl, doc_sha256 "
                "from {}.chunks order by id"
            ).format(sql.Identifier(stores.chunks.schema))
        ).fetchall()
    source_id = f"doc:{document.id}"
    assert [r["id"] for r in rows] == [f"{source_id}:{n}" for n in (1, 2, 3)]
    assert rows[1]["path"] == ["Contrato de locação", "Cláusula 1 - Prazo"]
    assert {(r["source_id"], r["title"], r["secrecy_level"]) for r in rows} == {
        (source_id, "contrato.md", "restricted")
    }
    assert all(r["acl"] == ["ana"] and r["doc_sha256"] == document.sha256 for r in rows)
    hits = stores.chunks.hybrid_search("multa rescisão", embedder._one("multa rescisão"), 1)
    assert hits[0].id == f"{source_id}:3"


def test_ingest_of_a_document_that_is_not_stored_fails_for_good(stores: Stores) -> None:
    ingestor = DocumentIngestor(stores.documents, stores.chunks, BagEmbedder())

    with pytest.raises(DocumentRejectedError) as raised:
        ingestor.ingest(str(uuid.uuid4()))

    assert raised.value.reason == "missing"


def test_intake_queues_a_file_once_and_again_only_after_a_failure(stores: Stores) -> None:
    queue = FakeQueue()
    intake = DocumentIntake(stores.documents, stores.jobs, queue)

    first = asyncio.run(intake.submit(upload(), "ana"))
    again = asyncio.run(intake.submit(upload(), "ana"))

    assert (first.duplicate, again.duplicate) == (False, True)
    assert again.job.id == first.job.id and again.document.id == first.document.id
    assert (first.job.kind, first.job.subject, first.job.status) == (
        INGEST_JOB,
        first.document.id,
        JobStatus.QUEUED,
    )
    assert queue.held == {first.job.id: {"kind": INGEST_JOB, "document_id": first.document.id}}
    assert intake.job(first.job.id) == first.job

    stores.jobs.fail(first.job.id, "OperationalError after 5 tries")
    queue.held.clear()  # arq dropped the failed job
    retried = asyncio.run(intake.submit(upload(), "ana"))
    assert retried.job.status is JobStatus.QUEUED
    assert first.job.id in queue.held

    stores.jobs.succeed(first.job.id, {"chunks": 3})
    sent = len(queue.sent)
    done = asyncio.run(intake.submit(upload(), "ana"))
    assert done.job.status is JobStatus.SUCCEEDED
    assert len(queue.sent) == sent  # nothing left to do: nothing sent
