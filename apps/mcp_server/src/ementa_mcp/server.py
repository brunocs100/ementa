"""Entry point of `ementa-mcp`: the Ementa tools over MCP (stdio in F1)."""

from datetime import date
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from ementa.tools import deadlines, statutes
from ementa_kit.config import get_settings
from ementa_kit.telemetry import get_logger, get_tracer, setup

INSTRUCTIONS = (
    "Ementa: AI agents for a law firm. Answers are drafts for a lawyer to review, "
    "never legal advice."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

tracer = get_tracer(__name__)


def build_server() -> MCPServer:
    server = MCPServer(name="ementa", instructions=INSTRUCTIONS)

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
        with tracer.start_as_current_span(
            "execute_tool calculate_deadline",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "calculate_deadline",
                "ementa.data_class": deadlines.DATA_CLASS.value,
            },
        ):
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
        with tracer.start_as_current_span(
            "execute_tool get_statute_article",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "get_statute_article",
                "ementa.data_class": statutes.DATA_CLASS.value,
            },
        ):
            return statutes.get_statute_article(request)

    return server


def main() -> None:
    settings = get_settings()
    setup(settings)
    get_logger(__name__).info("mcp_server_starting", transport="stdio")
    build_server().run("stdio")


if __name__ == "__main__":
    main()
