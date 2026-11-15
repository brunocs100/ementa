"""Entry point of `ementa-mcp`: the Ementa tools over MCP, on stdio or Streamable HTTP."""

import argparse
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from typing import Annotated, Literal, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from opentelemetry.trace import Span
from pydantic import Field

from ementa import services
from ementa.agents.legal_research import ResearchAnswer
from ementa.tools import deadlines, research, statutes
from ementa_kit.config import get_settings
from ementa_kit.rag import Hit
from ementa_kit.telemetry import get_logger, get_tracer, setup

INSTRUCTIONS = (
    "Ementa: AI agents for a law firm. Answers are drafts for a lawyer to review, "
    "never legal advice."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

tracer = get_tracer(__name__)


class Searcher(Protocol):
    def search(self, queries: list[str], limit: int = 5) -> list[Hit]: ...


class Answerer(Protocol):
    def ask(self, question: str) -> ResearchAnswer: ...


def _tool_span(name: str, data_class: str) -> AbstractContextManager[Span]:
    return tracer.start_as_current_span(
        f"execute_tool {name}",
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "ementa.data_class": data_class,
        },
    )


def build_server(
    knowledge: Callable[[], Searcher] = services.knowledge_base,
    agent: Callable[[], Answerer] = services.legal_research_agent,
) -> MCPServer:
    """`knowledge` and `agent` are built on first call, so listing tools needs no database."""
    server = MCPServer(name="ementa", instructions=INSTRUCTIONS)

    @server.tool(
        name="search_legal_corpus",
        title="Buscar no corpus jurídico",
        description=research.SEARCH_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def search_legal_corpus(
        query: Annotated[str, Field(min_length=3, max_length=500, description="O que buscar")],
        k: Annotated[int, Field(ge=1, le=10, description="Quantos artigos devolver")] = 5,
    ) -> research.SearchLegalCorpusOutput:
        request = research.SearchLegalCorpusInput(query=query, k=k)
        with _tool_span("search_legal_corpus", research.DATA_CLASS.value):
            hits = knowledge().search([request.query], limit=request.k)
        return research.SearchLegalCorpusOutput(
            query=request.query, hits=[research.SearchHit.from_hit(h) for h in hits]
        )

    @server.tool(
        name="answer_legal_question",
        title="Responder pergunta jurídica com citação verificada",
        description=research.ANSWER_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def answer_legal_question(
        question: Annotated[
            str, Field(min_length=5, max_length=2000, description="Pergunta em português")
        ],
    ) -> research.AnswerLegalQuestionOutput:
        request = research.AnswerLegalQuestionInput(question=question)
        with _tool_span("answer_legal_question", research.DATA_CLASS.value):
            answer = agent().ask(request.question)
        return research.AnswerLegalQuestionOutput.from_answer(answer)

    @server.tool(
        name="calculate_deadline",
        title="Calcular prazo processual",
        description=deadlines.DESCRIPTION,
        annotations=READ_ONLY,
    )
    def calculate_deadline(
        start_date: Annotated[date, Field(description="Dia do começo do prazo, AAAA-MM-DD")],
        business_days: Annotated[int, Field(ge=1, le=365, description="Prazo em dias úteis")],
        state: Annotated[Literal["RS"], Field(description="UF do tribunal")] = "RS",
        extra_closed_days: Annotated[
            list[date] | None,
            Field(description="Dias sem expediente no tribunal, além dos feriados"),
        ] = None,
    ) -> deadlines.CalculateDeadlineOutput:
        request = deadlines.CalculateDeadlineInput(
            start_date=start_date,
            business_days=business_days,
            state=state,
            extra_closed_days=extra_closed_days or [],
        )
        with _tool_span("calculate_deadline", deadlines.DATA_CLASS.value):
            return deadlines.calculate_deadline(request)

    @server.tool(
        name="get_statute_article",
        title="Consultar artigo de lei",
        description=statutes.DESCRIPTION,
        annotations=READ_ONLY,
    )
    def get_statute_article(
        source_id: Annotated[str, Field(description="Id de fonte, como cdc:art49 ou cdc:art42:pu")],
    ) -> statutes.StatuteArticleOutput:
        request = statutes.GetStatuteArticleInput(source_id=source_id)
        with _tool_span("get_statute_article", statutes.DATA_CLASS.value):
            return statutes.get_statute_article(request)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(prog="ementa-mcp")
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve Streamable HTTP (stateless, spec 2026-07-28) instead of stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    settings = get_settings()
    setup(settings)
    server = build_server()
    if args.http:
        get_logger(__name__).info(
            "mcp_server_starting", transport="streamable-http", port=args.port
        )
        server.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            stateless_http=True,
            json_response=True,
        )
    else:
        get_logger(__name__).info("mcp_server_starting", transport="stdio")
        server.run("stdio")


if __name__ == "__main__":
    main()
