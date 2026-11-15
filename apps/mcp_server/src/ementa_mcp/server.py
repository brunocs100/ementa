"""Entry point of `ementa-mcp`. The tools arrive in F1."""

from mcp.server.mcpserver import MCPServer

from ementa_kit.config import get_settings
from ementa_kit.telemetry import get_logger, setup

INSTRUCTIONS = (
    "Ementa: AI agents for a law firm. Answers are drafts for a lawyer to review, "
    "never legal advice."
)


def build_server() -> MCPServer:
    return MCPServer(name="ementa", instructions=INSTRUCTIONS)


def main() -> None:
    settings = get_settings()
    setup(settings)
    get_logger(__name__).info("mcp_server_starting", transport="stdio")
    build_server().run("stdio")


if __name__ == "__main__":
    main()
