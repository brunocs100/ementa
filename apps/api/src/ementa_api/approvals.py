"""Drafting runs and the lawyer's approval (F4): start a brief or a contract review, read the
inbox, decide.

A run goes through the worker: the start answers 202 with the run and its job, and the draft
shows up in the inbox once the agent stops for approval. The sócia sees and decides the
approvals of every run, the associado only those of the runs they asked for
(`ementa.domain.access.may_decide`); to anyone else an approval does not exist, so it answers
404. A decision is final: a different second one answers 409, and the same one again only queues
the resume once more, which is how a client retries after a 503.
"""

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any, Protocol, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ementa.agents.brief_drafting import export_docx
from ementa.documents import Document
from ementa.domain.access import decides_every_run, may_decide
from ementa_api.auth import User, authorized_user
from ementa_api.problems import Problem, problem_response, problem_responses
from ementa_kit.hitl import Action, ApprovalDecision
from ementa_kit.hitl.runner import (
    AlreadyDecidedError,
    Decided,
    IdempotencyKeyReusedError,
    Started,
    UnknownAgentError,
)
from ementa_kit.hitl.store import Approval, ApprovalStatus, Run, RunStatus
from ementa_kit.jobs import QueueUnavailableError
from ementa_kit.policy import DataClass

BRIEF_DRAFTING = "brief_drafting"
CONTRACT_REVIEW = "contract_review"
MAX_TEXT = 200_000  # characters of an edited draft

BRIEF_DESCRIPTION = (
    "Inicia a redação de uma peça pelo `brief_drafting` e responde 202 com a execução e a "
    "tarefa. O rascunho entra na caixa de aprovações (`GET /v1/approvals`) quando o agente "
    "para para a revisão do advogado; nada é finalizado sem a decisão. Com `Idempotency-Key`, "
    "o mesmo pedido de novo devolve a mesma execução e, se ela tinha falhado, a põe na fila "
    "outra vez. Só sócia e associado."
)
REVIEW_DESCRIPTION = (
    "Inicia a análise pelo `contract_review` de um contrato enviado em `POST /v1/documents` "
    "e responde 202 com a execução e a tarefa. O documento tem de ser seu; o de outra pessoa "
    "responde 404, como um que não existe. O relatório entra na caixa de aprovações antes de "
    "sair. Só sócia e associado."
)
INBOX_DESCRIPTION = (
    "A caixa de aprovações, da mais antiga à mais nova: por padrão as pendentes. A sócia vê "
    "as de todas as execuções; o associado, as das execuções que ele pediu. Para a próxima "
    "página, mande o `next_cursor` recebido em `cursor`."
)
DECIDE_DESCRIPTION = (
    "Aprova, rejeita ou edita um rascunho pendente e responde 202: a execução volta à fila e "
    "o agente retoma com a decisão. `edit` leva o texto editado em `text`; `approve` e "
    "`reject`, nenhum. A decisão é definitiva: outra depois dela responde 409, e a mesma de "
    "novo só põe a retomada na fila outra vez. Cada decisão é um evento de auditoria. A sócia "
    "decide qualquer execução; o associado, só as que pediu."
)


class Drafting(Protocol):
    async def start(
        self,
        agent: str,
        request: Mapping[str, Any],
        requested_by: str,
        idempotency_key: str | None = None,
    ) -> Started: ...

    async def decide(self, approval_id: str, decision: ApprovalDecision) -> Decided: ...

    def approval(self, approval_id: str) -> Approval | None: ...

    def approvals(
        self,
        status: ApprovalStatus,
        *,
        requested_by: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> list[Approval]: ...

    def run(self, run_id: str) -> Run | None: ...


class DocumentLookup(Protocol):
    def get(self, document_id: str) -> Document | None: ...


IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[\x21-\x7e]+$",
        description="Chave do cliente para repetir o pedido sem criar outra execução",
    ),
]


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class BriefRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: str = Field(
        min_length=1, max_length=20_000, description="Os fatos do caso, como o advogado os conta"
    )
    kind: str = Field(
        default="peticao_inicial",
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="Tipo da peça: `peticao_inicial`, `contestacao`, `recurso`",
    )
    case_id: UUID | None = Field(default=None, description="Processo da peça, se houver")
    template: str = Field(
        default="initial-petition-credit-listing",
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
        description=(
            "Modelo do escritório: `initial-petition-credit-listing` (negativação indevida) ou "
            "`initial-petition-product-defect` (vício do produto)"
        ),
    )


class RunAccepted(BaseModel):
    run_id: str = Field(description="A execução, que também é a thread do agente")
    job_id: str = Field(description="A tarefa na fila; o estado está em `GET /v1/jobs/{job_id}`")
    status: RunStatus
    duplicate: bool = Field(
        description="A mesma `Idempotency-Key` já tinha iniciado esta execução: nada foi gravado."
    )


class RunOut(BaseModel):
    id: str
    agent: str
    status: RunStatus
    requested_by: str
    result: dict[str, Any] | None = Field(
        description="O que o agente produziu, depois que a execução terminou"
    )
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class DecisionOut(BaseModel):
    action: Action
    text: str | None
    comment: str | None
    decided_by: str


class ApprovalSummary(BaseModel):
    id: str
    run_id: str
    agent: str
    kind: str = Field(description="O que se aprova: `brief`, `contract_report`")
    title: str
    summary: str = Field(description="Por que o agente parou e o que conferir")
    data_class: DataClass
    status: ApprovalStatus
    requested_by: str = Field(description="Quem pediu a execução")
    created_at: datetime
    decided_at: datetime | None


class ApprovalOut(ApprovalSummary):
    draft: str = Field(description="O rascunho como o agente o finalizaria")
    final_text: str | None = Field(
        description="O que segue adiante: o rascunho, se aprovado; o texto do advogado, se editado"
    )
    diff: str | None = Field(description="Do rascunho ao texto final, em diff unificado")
    decision: DecisionOut | None
    run: RunOut | None


class ApprovalPage(BaseModel):
    items: list[ApprovalSummary]
    next_cursor: str | None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    text: str | None = Field(
        default=None, max_length=MAX_TEXT, description="O texto editado; só com `edit`"
    )
    comment: str | None = Field(default=None, max_length=4000, description="Nota do advogado")

    @model_validator(mode="after")
    def _text_goes_with_edit(self) -> Self:
        if self.action == "edit" and not (self.text or "").strip():
            raise ValueError("an edit needs the edited text")
        if self.action != "edit" and self.text is not None:
            raise ValueError("text goes only with the edit action")
        return self


class DecisionAccepted(BaseModel):
    approval: ApprovalOut
    job_id: str = Field(description="A retomada na fila; o estado está em `GET /v1/jobs/{job_id}`")


def approval_summary(approval: Approval) -> ApprovalSummary:
    request = approval.request
    return ApprovalSummary(
        id=approval.id,
        run_id=approval.thread_id,
        agent=request.agent,
        kind=request.kind,
        title=request.title,
        summary=request.summary,
        data_class=request.data_class,
        status=approval.status,
        requested_by=approval.requested_by,
        created_at=approval.created_at,
        decided_at=approval.decided_at,
    )


def approval_out(approval: Approval, run: Run | None) -> ApprovalOut:
    decision = approval.decision
    return ApprovalOut(
        **approval_summary(approval).model_dump(),
        draft=approval.request.draft,
        final_text=approval.final_text,
        diff=approval.diff,
        decision=DecisionOut(
            action=decision.action,
            text=decision.text,
            comment=decision.comment,
            decided_by=decision.decided_by,
        )
        if decision is not None
        else None,
        run=RunOut(
            id=run.id,
            agent=run.agent,
            status=run.status,
            requested_by=run.requested_by,
            result=run.result,
            error=run.error,
            created_at=run.created_at,
            finished_at=run.finished_at,
        )
        if run is not None
        else None,
    )


def _visible(approval: Approval | None, user: User) -> Approval:
    """The approval, if the user may decide it; to anyone else it does not exist."""
    if approval is None or not may_decide(user.role, user.id, approval.requested_by):
        raise HTTPException(status_code=404, detail="No approval with this id.")
    return approval


def _queue_down(instance: str, detail: str) -> JSONResponse:
    return problem_response(
        Problem(
            type="/problems/queue-unavailable",
            title="Queue unavailable",
            status=503,
            detail=detail,
            instance=instance,
        )
    )


async def _start(
    desk: Drafting,
    agent: str,
    request: Mapping[str, Any],
    user: User,
    key: str | None,
    response: Response,
    instance: str,
) -> RunAccepted | JSONResponse:
    try:
        started = await desk.start(agent, request, user.id, key)
    except UnknownAgentError:
        return problem_response(
            Problem(
                type="/problems/agent-unavailable",
                title="Agent unavailable",
                status=503,
                detail=f"The {agent} agent is not installed here.",
                instance=instance,
            )
        )
    except IdempotencyKeyReusedError:
        return problem_response(
            Problem(
                type="/problems/idempotency-key-reused",
                title="Idempotency key reused",
                status=422,
                detail="This Idempotency-Key started a run with another request; use a new key.",
                instance=instance,
            )
        )
    except QueueUnavailableError:
        return _queue_down(
            instance,
            "The job queue is not reachable. The run is recorded: send the request again with "
            "the same Idempotency-Key to queue it.",
        )
    response.headers["Location"] = f"/v1/jobs/{started.job.id}"
    return RunAccepted(
        run_id=started.run.id,
        job_id=started.job.id,
        status=started.run.status,
        duplicate=started.duplicate,
    )


def drafting_router(
    drafting: Callable[[], Drafting], documents: Callable[[], DocumentLookup]
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/briefs",
        status_code=202,
        response_model=RunAccepted,
        responses=problem_responses(401, 403, 422, 503),
        summary="Inicia a redação de uma peça, com aprovação do advogado",
        description=BRIEF_DESCRIPTION,
        tags=["drafting"],
    )
    async def start_brief(
        body: BriefRequest,
        response: Response,
        user: Annotated[User, Depends(authorized_user)],
        idempotency_key: IdempotencyKey = None,
    ) -> RunAccepted | JSONResponse:
        request = body.model_dump(mode="json") | {"requested_by": user.id}
        return await _start(
            drafting(), BRIEF_DRAFTING, request, user, idempotency_key, response, "/v1/briefs"
        )

    @router.post(
        "/v1/contracts/{document_id}/review",
        status_code=202,
        response_model=RunAccepted,
        responses=problem_responses(401, 403, 404, 422, 503),
        summary="Inicia a análise de um contrato enviado, com aprovação do advogado",
        description=REVIEW_DESCRIPTION,
        tags=["drafting"],
    )
    async def review_contract(
        document_id: UUID,
        response: Response,
        user: Annotated[User, Depends(authorized_user)],
        idempotency_key: IdempotencyKey = None,
    ) -> RunAccepted | JSONResponse:
        document = await asyncio.to_thread(documents().get, str(document_id))
        if document is None or user.id not in document.acl:  # another's is not there either
            raise HTTPException(status_code=404, detail="No document with this id.")
        request = {"document_id": document.id}
        instance = "/v1/contracts/{document_id}/review"
        return await _start(
            drafting(), CONTRACT_REVIEW, request, user, idempotency_key, response, instance
        )

    @router.get(
        "/v1/approvals",
        response_model=ApprovalPage,
        responses=problem_responses(401, 403, 422),
        summary="Caixa de aprovações",
        description=INBOX_DESCRIPTION,
        tags=["approvals"],
    )
    def inbox(
        user: Annotated[User, Depends(authorized_user)],
        status: Annotated[
            ApprovalStatus, Query(description="Pendentes por padrão")
        ] = ApprovalStatus.PENDING,
        cursor: Annotated[
            UUID | None, Query(description="O `next_cursor` da página anterior")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=200, description="Aprovações por página")] = 50,
    ) -> ApprovalPage:
        scope = None if decides_every_run(user.role) else user.id
        found = drafting().approvals(
            status, requested_by=scope, after=str(cursor) if cursor else None, limit=limit + 1
        )
        items = found[:limit]
        return ApprovalPage(
            items=[approval_summary(a) for a in items],
            next_cursor=items[-1].id if len(found) > limit else None,
        )

    @router.get(
        "/v1/approvals/{approval_id}",
        response_model=ApprovalOut,
        responses=problem_responses(401, 403, 404, 422),
        summary="Uma aprovação, com o rascunho e o estado da execução",
        tags=["approvals"],
    )
    def get_approval(
        approval_id: UUID, user: Annotated[User, Depends(authorized_user)]
    ) -> ApprovalOut:
        desk = drafting()
        approval = _visible(desk.approval(str(approval_id)), user)
        return approval_out(approval, desk.run(approval.thread_id))

    @router.get(
        "/v1/approvals/{approval_id}/docx",
        response_class=Response,
        responses={
            200: {"content": {DOCX: {}}, "description": "O documento em DOCX"},
            **problem_responses(401, 403, 404, 422),
        },
        summary="Baixa em DOCX: o rascunho com o aviso, ou o texto aprovado com quem aprovou",
        tags=["approvals"],
    )
    def approval_docx(
        approval_id: UUID, user: Annotated[User, Depends(authorized_user)]
    ) -> Response:
        approval = _visible(drafting().approval(str(approval_id)), user)
        final = approval.final_text
        if final is not None and approval.decision is not None:
            data = export_docx(
                approval.request.title,
                final,
                approved_by=approval.decision.decided_by,
                approved_at=approval.decided_at,
            )
        else:
            data = export_docx(approval.request.title, approval.request.draft)
        name = f"ementa-{approval.id}.docx"
        return Response(
            content=data,
            media_type=DOCX,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @router.post(
        "/v1/approvals/{approval_id}",
        status_code=202,
        response_model=DecisionAccepted,
        responses=problem_responses(401, 403, 404, 409, 422, 503),
        summary="Aprova, rejeita ou edita um rascunho pendente",
        description=DECIDE_DESCRIPTION,
        tags=["approvals"],
    )
    async def decide(
        approval_id: UUID,
        body: DecisionIn,
        response: Response,
        user: Annotated[User, Depends(authorized_user)],
    ) -> DecisionAccepted | JSONResponse:
        desk = drafting()
        instance = "/v1/approvals/{approval_id}"
        approval = _visible(await asyncio.to_thread(desk.approval, str(approval_id)), user)
        decision = ApprovalDecision(
            action=body.action, text=body.text, comment=body.comment, decided_by=user.id
        )
        try:
            decided = await desk.decide(approval.id, decision)
        except AlreadyDecidedError as error:
            return problem_response(
                Problem(
                    type="/problems/approval-decided",
                    title="Approval already decided",
                    status=409,
                    detail=f"The approval is already {error.approval.status}: decisions are final.",
                    instance=instance,
                )
            )
        except QueueUnavailableError:
            return _queue_down(
                instance,
                "The job queue is not reachable. The decision is recorded: send the same "
                "decision again to queue the resume.",
            )
        run = await asyncio.to_thread(desk.run, decided.approval.thread_id)
        response.headers["Location"] = f"/v1/jobs/{decided.job.id}"
        return DecisionAccepted(approval=approval_out(decided.approval, run), job_id=decided.job.id)

    return router
