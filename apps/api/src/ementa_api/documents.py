"""Firm documents: upload for queued ingestion and the state of the job (F3).

A document is firm data, not public corpus: it is stored as restricted, with the uploader as
owner, and only the uploader sees its job. The bytes stay in Postgres; the worker indexes them.
"""

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from ementa.documents import MAX_BYTES, DocumentRejectedError, Submission, Upload
from ementa_api.auth import User, authorized_user
from ementa_api.problems import Problem, problem_response, problem_responses
from ementa_kit.jobs import Job, JobStatus, QueueUnavailableError

# Room for the multipart boundaries and headers around the file in the request body.
_MULTIPART_OVERHEAD = 16 * 1024

UPLOAD_DESCRIPTION = (
    "Envia um documento do escritório (texto, Markdown ou PDF com camada de texto, até "
    f"{MAX_BYTES // (1024 * 1024)} MiB) para ingestão em fila e responde 202 com o id da "
    "tarefa. O documento fica restrito, com quem enviou como dono. Reenviar o mesmo arquivo "
    "devolve a mesma tarefa e não grava nada de novo; se a tarefa tinha falhado, ela volta "
    "para a fila."
)


class Intake(Protocol):
    async def submit(self, upload: Upload, user_id: str) -> Submission: ...

    def job(self, job_id: str) -> Job | None: ...


class JobAccepted(BaseModel):
    job_id: str
    document_id: str
    status: JobStatus
    duplicate: bool = Field(
        description="O mesmo arquivo já tinha sido enviado por este usuário: nada novo foi gravado."
    )


class JobState(BaseModel):
    id: str
    kind: str
    subject: str = Field(description="O que a tarefa processa; em `ingest_document`, o documento.")
    status: JobStatus
    attempts: int
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


_REJECTED = {
    "too_large": (413, "/problems/document-too-large", "Document too large"),
    "unsupported": (415, "/problems/unsupported-media-type", "Unsupported media type"),
    "empty": (422, "/problems/invalid-document", "Invalid document"),
    "unreadable": (422, "/problems/invalid-document", "Invalid document"),
    "missing": (422, "/problems/invalid-document", "Invalid document"),
}


def _rejected(reason: str, detail: str, instance: str) -> JSONResponse:
    status, type_, title = _REJECTED[reason]
    return problem_response(
        Problem(type=type_, title=title, status=status, detail=detail, instance=instance)
    )


class _UploadLimit(APIRoute):
    """Refuses a body announced as too big before FastAPI spools it to a temporary file."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def limited(request: Request) -> Response:
            length = request.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BYTES + _MULTIPART_OVERHEAD:
                return _rejected(
                    "too_large",
                    f"The request has {length} bytes; a document may have {MAX_BYTES}.",
                    request.url.path,
                )
            return await handler(request)

        return limited


def documents_router(intake: Callable[[], Intake]) -> APIRouter:
    router = APIRouter(route_class=_UploadLimit)

    @router.post(
        "/v1/documents",
        status_code=202,
        response_model=JobAccepted,
        responses=problem_responses(413, 415, 422, 503),
        summary="Envia um documento do escritório para ingestão em fila",
        description=UPLOAD_DESCRIPTION,
        tags=["documents"],
    )
    async def upload_document(
        file: Annotated[UploadFile, File(description="Arquivo .txt, .md ou .pdf")],
        response: Response,
        user: Annotated[User, Depends(authorized_user)],
    ) -> JobAccepted | JSONResponse:
        path = "/v1/documents"
        content = await file.read(MAX_BYTES + 1)
        try:
            upload = Upload.accept(file.filename, file.content_type, content)
            submission = await intake().submit(upload, user.id)
        except DocumentRejectedError as error:
            return _rejected(error.reason, str(error), path)
        except QueueUnavailableError:
            return problem_response(
                Problem(
                    type="/problems/queue-unavailable",
                    title="Queue unavailable",
                    status=503,
                    detail="The job queue is not reachable. Send the file again later: "
                    "it will not be stored twice.",
                    instance=path,
                )
            )
        response.headers["Location"] = f"/v1/jobs/{submission.job.id}"
        return JobAccepted(
            job_id=submission.job.id,
            document_id=submission.document.id,
            status=submission.job.status,
            duplicate=submission.duplicate,
        )

    @router.get(
        "/v1/jobs/{job_id}",
        response_model=JobState,
        responses=problem_responses(404, 422),
        summary="Estado de uma tarefa em fila",
        tags=["jobs"],
    )
    def get_job(job_id: str, user: Annotated[User, Depends(authorized_user)]) -> JobState:
        job = intake().job(job_id)
        if job is None or job.requested_by != user.id:  # another user's job does not exist here
            raise HTTPException(status_code=404, detail="No job with this id.")
        return JobState(
            id=job.id,
            kind=job.kind,
            subject=job.subject,
            status=job.status,
            attempts=job.attempts,
            result=job.result,
            error=job.error,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )

    return router
