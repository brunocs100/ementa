"""POST /v1/documents and GET /v1/jobs/{id} with a fake intake: no database, no Redis."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ementa.agents.legal_research import ResearchAnswer
from ementa.documents import MAX_BYTES, Document, MediaType, Submission, Upload
from ementa_api.app import create_app
from ementa_api.auth import Role, User, current_user
from ementa_kit.jobs import Job, JobStatus, QueueUnavailableError
from ementa_kit.policy import DataClass

NOW = datetime(2026, 11, 15, 14, 30, tzinfo=UTC)
# Fictitious contract, written for the tests.
CONTRACT = "# Contrato\n\nContrato fictício entre Empresa Exemplo e Cliente Teste.\n"


class FakeIntake:
    """One document and one job per uploader and content, as the real intake keeps them."""

    def __init__(self, queue_down: bool = False) -> None:
        self.queue_down = queue_down
        self.jobs: dict[str, Job] = {}
        self.by_content: dict[tuple[str, str], Submission] = {}

    async def submit(self, upload: Upload, user_id: str) -> Submission:
        if self.queue_down:
            raise QueueUnavailableError("Redis did not take the job")
        key = (user_id, upload.sha256)
        if key in self.by_content:
            known = self.by_content[key]
            return Submission(known.job, known.document, duplicate=True)
        document = Document(
            str(uuid.uuid4()),
            user_id,
            user_id,
            [user_id],
            DataClass.RESTRICTED,
            upload.sha256,
            upload.filename,
            upload.media_type,
            len(upload.content),
            None,
            NOW,
            None,
        )
        job = Job(
            str(uuid.uuid4()),
            "ingest_document",
            document.id,
            user_id,
            JobStatus.QUEUED,
            0,
            None,
            None,
            NOW,
            NOW,
            None,
            None,
        )
        self.jobs[job.id] = job
        self.by_content[key] = Submission(job, document, duplicate=False)
        return self.by_content[key]

    def job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)


class NoResearch:
    def ask(self, question: str) -> ResearchAnswer:
        raise AssertionError("not used here")


UPLOADER = User(id="dev", name="Desenvolvimento", role=Role.ASSOCIADO)


def client(intake: FakeIntake, user: User | None = None) -> TestClient:
    app = create_app(researcher=NoResearch, chunks=lambda: 1, documents=lambda: intake)
    app.dependency_overrides[current_user] = lambda: user or UPLOADER
    return TestClient(app, raise_server_exceptions=False)


@dataclass(frozen=True)
class Sent:
    status_code: int
    headers: Mapping[str, str]
    body: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.body


def send(api: TestClient, name: str, content: bytes, media_type: str = "text/markdown") -> Sent:
    response = api.post("/v1/documents", files={"file": (name, content, media_type)})
    return Sent(response.status_code, response.headers, response.json())


def test_upload_is_accepted_with_the_job_id_and_where_to_follow_it() -> None:
    intake = FakeIntake()

    sent = send(client(intake), "contrato.md", CONTRACT.encode())

    assert sent.status_code == 202
    body = sent.json()
    assert set(body) == {"job_id", "document_id", "status", "duplicate"}
    assert (body["status"], body["duplicate"]) == ("queued", False)
    assert sent.headers["location"] == f"/v1/jobs/{body['job_id']}"
    [submission] = intake.by_content.values()
    assert submission.document.owner_id == "dev"  # the authenticated user
    assert submission.document.media_type is MediaType.MARKDOWN


def test_the_same_file_again_is_the_same_job_and_stores_nothing_new() -> None:
    intake = FakeIntake()
    api = client(intake)

    first = send(api, "contrato.md", CONTRACT.encode()).json()
    again = send(api, "renomeado.md", CONTRACT.encode()).json()

    assert again["job_id"] == first["job_id"]
    assert again["document_id"] == first["document_id"]
    assert again["duplicate"] is True
    assert len(intake.by_content) == 1


@pytest.mark.parametrize(
    ("name", "content", "media_type", "status", "problem"),
    [
        ("vazio.txt", b"", "text/plain", 422, "/problems/invalid-document"),
        (
            "latin1.txt",
            "contratação".encode("latin-1"),
            "text/plain",
            422,
            "/problems/invalid-document",
        ),
        (
            "contrato.docx",
            b"PK\x03\x04",
            "application/octet-stream",
            415,
            "/problems/unsupported-media-type",
        ),
        ("grande.txt", b"a" * (MAX_BYTES + 1), "text/plain", 413, "/problems/document-too-large"),
    ],
)
def test_rejected_files_are_problems(
    name: str, content: bytes, media_type: str, status: int, problem: str
) -> None:
    intake = FakeIntake()

    sent = send(client(intake), name, content, media_type)

    assert sent.status_code == status
    assert sent.headers["content-type"] == "application/problem+json"
    assert sent.json()["type"] == problem
    assert sent.json()["instance"] == "/v1/documents"
    assert intake.by_content == {}


def test_a_body_announced_far_over_the_limit_is_refused_before_it_is_read() -> None:
    sent = send(client(FakeIntake()), "enorme.txt", b"a" * (MAX_BYTES + 64 * 1024), "text/plain")

    assert sent.status_code == 413
    assert sent.json()["detail"].startswith("The request has")


def test_queue_down_is_503_and_says_the_file_can_be_sent_again() -> None:
    sent = send(client(FakeIntake(queue_down=True)), "contrato.md", CONTRACT.encode())

    assert sent.status_code == 503
    assert sent.json()["type"] == "/problems/queue-unavailable"
    assert "not be stored twice" in sent.json()["detail"]


def test_missing_file_field_is_a_validation_problem() -> None:
    response = client(FakeIntake()).post("/v1/documents", data={"outro": "x"})

    assert response.status_code == 422
    assert response.json()["type"] == "/problems/validation-error"


def test_only_the_uploader_sees_the_job() -> None:
    intake = FakeIntake()
    job_id = send(client(intake), "contrato.md", CONTRACT.encode()).json()["job_id"]

    own = client(intake).get(f"/v1/jobs/{job_id}")
    stranger = client(intake, User("bruno", "Bruno", Role.ESTAGIARIO)).get(f"/v1/jobs/{job_id}")
    unknown = client(intake).get(f"/v1/jobs/{uuid.uuid4()}")

    assert own.status_code == 200
    assert own.json() == {
        "id": job_id,
        "kind": "ingest_document",
        "subject": intake.jobs[job_id].subject,
        "status": "queued",
        "attempts": 0,
        "result": None,
        "error": None,
        "created_at": "2026-11-15T14:30:00Z",
        "started_at": None,
        "finished_at": None,
    }
    assert stranger.status_code == unknown.status_code == 404
    assert stranger.json() == unknown.json()  # nothing tells them apart
    assert stranger.json()["instance"] == "/v1/jobs/{job_id}"  # the template, not the id
    assert stranger.headers["content-type"] == "application/problem+json"
