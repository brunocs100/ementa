"""Routes of the REST API. Dependencies are built lazily, so the app starts without a database."""

from collections.abc import Callable
from typing import Annotated, Protocol

import psycopg
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from ementa import services
from ementa.agents.legal_research import ResearchAnswer
from ementa.tools.research import (
    ANSWER_DESCRIPTION,
    AnswerLegalQuestionInput,
    AnswerLegalQuestionOutput,
)
from ementa.users import FirmUser
from ementa_api.audit import AUDIT_DESCRIPTION, AuditEventOut, AuditMiddleware, AuditPage
from ementa_api.auth import (
    TOKEN_URL,
    User,
    authorized_user,
    check_jwt_settings,
    create_access_token,
    unauthorized,
)
from ementa_api.problems import (
    Problem,
    install_problem_handlers,
    problem_response,
    problem_responses,
)
from ementa_kit.audit import AuditLog
from ementa_kit.config import get_settings


class Researcher(Protocol):
    def ask(self, question: str) -> ResearchAnswer: ...


class Authenticator(Protocol):
    def authenticate(self, username: str, password: str) -> FirmUser | None: ...


class AccessToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Segundos até o token vencer")


TOKEN_DESCRIPTION = (
    "Fluxo OAuth2 de senha, o mesmo do botão Authorize do /docs. Devolve um JWT (HS256) que vale "
    "15 minutos por padrão (`EMENTA_JWT_TTL_MINUTES`); mande-o em `Authorization: Bearer <token>` "
    "nas outras rotas. Os usuários fictícios da demo e as senhas estão em "
    "docs/SEGURANCA-E-GOVERNANCA.md."
)


class Health(BaseModel):
    status: str


class Readiness(BaseModel):
    status: str
    chunks: int


def corpus_chunks() -> int:
    """Chunks in the knowledge base; raises when the database is unreachable."""
    with psycopg.connect(get_settings().database_url, connect_timeout=3) as conn:
        row = conn.execute("select count(*) from knowledge.chunks").fetchone()
    return int(row[0]) if row else 0


def firm_audit_log() -> AuditLog:
    """`services.audit_log`, looked up on every call: the API tests swap it for a list, so they
    never write to an append-only table that cannot be cleaned up."""
    return services.audit_log()


def create_app(
    researcher: Callable[[], Researcher] = services.private_legal_research,
    chunks: Callable[[], int] = corpus_chunks,
    users: Callable[[], Authenticator] = services.user_directory,
    audit: Callable[[], AuditLog] = firm_audit_log,
) -> FastAPI:
    check_jwt_settings(get_settings())  # fails here, at startup, in prod without a secret
    app = FastAPI(
        title="Ementa API",
        version="0.1.0",
        description="Agentes de IA para advocacia. Saídas são rascunho para o advogado conferir.",
    )
    install_problem_handlers(app)
    app.add_middleware(AuditMiddleware, audit=audit)

    @app.post(
        TOKEN_URL,
        response_model=AccessToken,
        responses=problem_responses(401, 422),
        summary="Troca usuário e senha por um token de acesso",
        description=TOKEN_DESCRIPTION,
        tags=["auth"],
    )
    def issue_token(
        request: Request,
        response: Response,
        form: Annotated[OAuth2PasswordRequestForm, Depends()],
    ) -> AccessToken:
        found = users().authenticate(form.username, form.password)
        if found is None:
            raise unauthorized("Wrong username or password.")
        user = User(id=found.id, name=found.name, role=found.role)
        request.state.user = user  # the audit event of this login names who logged in
        response.headers["Cache-Control"] = "no-store"  # RFC 6749: tokens are not cached
        return AccessToken(
            access_token=create_access_token(user),
            expires_in=60 * get_settings().jwt_ttl_minutes,
        )

    @app.post(
        "/v1/legal-research",
        response_model=AnswerLegalQuestionOutput,
        responses=problem_responses(401, 403, 422, 500, 503),
        dependencies=[Depends(authorized_user)],
        summary="Pesquisa jurídica com citações verificadas",
        description=ANSWER_DESCRIPTION,
        tags=["legal-research"],
    )
    def legal_research(body: AnswerLegalQuestionInput) -> AnswerLegalQuestionOutput:
        return AnswerLegalQuestionOutput.from_answer(researcher().ask(body.question))

    @app.get(
        "/v1/audit/events",
        response_model=AuditPage,
        responses=problem_responses(401, 403, 422),
        dependencies=[Depends(authorized_user)],
        summary="Trilha de auditoria, do evento mais novo ao mais antigo",
        description=AUDIT_DESCRIPTION,
        tags=["audit"],
    )
    def audit_events(
        cursor: Annotated[
            str | None,
            Query(pattern=r"^[1-9][0-9]{0,17}$", description="O `next_cursor` da página anterior"),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=200, description="Eventos por página")] = 50,
    ) -> AuditPage:
        records = audit().page(before=int(cursor) if cursor else None, limit=limit + 1)
        items = records[:limit]
        more = len(records) > limit
        return AuditPage(
            items=[AuditEventOut.from_record(r) for r in items],
            next_cursor=str(items[-1].id) if more else None,
        )

    @app.get("/healthz", response_model=Health, tags=["ops"], summary="O processo está de pé")
    def healthz() -> Health:
        return Health(status="ok")

    @app.get(
        "/readyz",
        response_model=Readiness,
        responses=problem_responses(503),
        tags=["ops"],
        summary="Banco acessível e corpus carregado",
    )
    def readyz() -> Readiness | JSONResponse:
        try:
            count = chunks()
        except Exception:
            return _not_ready("The database is not reachable.")
        if count == 0:
            return _not_ready("The knowledge base is empty; run `uv run ementa ingest`.")
        return Readiness(status="ready", chunks=count)

    return app


def _not_ready(detail: str) -> JSONResponse:
    return problem_response(
        Problem(
            type="/problems/not-ready",
            title="Not ready",
            status=503,
            detail=detail,
            instance="/readyz",
        )
    )
