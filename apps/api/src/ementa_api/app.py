"""Routes of the REST API. Dependencies are built lazily, so the app starts without a database."""

from collections.abc import Callable
from typing import Protocol

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ementa import services
from ementa.agents.legal_research import ResearchAnswer
from ementa.tools.research import (
    ANSWER_DESCRIPTION,
    AnswerLegalQuestionInput,
    AnswerLegalQuestionOutput,
)
from ementa_api.problems import (
    Problem,
    install_problem_handlers,
    problem_response,
    problem_responses,
)
from ementa_kit.config import get_settings


class Researcher(Protocol):
    def ask(self, question: str) -> ResearchAnswer: ...


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


def create_app(
    researcher: Callable[[], Researcher] = services.private_legal_research,
    chunks: Callable[[], int] = corpus_chunks,
) -> FastAPI:
    app = FastAPI(
        title="Ementa API",
        version="0.1.0",
        description="Agentes de IA para advocacia. Saídas são rascunho para o advogado conferir.",
    )
    install_problem_handlers(app)

    @app.post(
        "/v1/legal-research",
        response_model=AnswerLegalQuestionOutput,
        responses=problem_responses(422, 500, 503),
        summary="Pesquisa jurídica com citações verificadas",
        description=ANSWER_DESCRIPTION,
        tags=["legal-research"],
    )
    def legal_research(body: AnswerLegalQuestionInput) -> AnswerLegalQuestionOutput:
        return AnswerLegalQuestionOutput.from_answer(researcher().ask(body.question))

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
