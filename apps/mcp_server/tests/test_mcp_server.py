import anyio
from mcp import Client

from ementa_mcp.server import build_server


def test_server_answers_in_memory_client() -> None:
    async def scenario() -> list[str]:
        async with Client(build_server()) as client:
            tools = await client.list_tools()
            return [tool.name for tool in tools.tools]

    assert anyio.run(scenario) == []
