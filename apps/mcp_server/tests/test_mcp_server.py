from typing import Any

import anyio
from mcp import Client

from ementa_mcp.server import build_server


def call(name: str, arguments: dict[str, Any]) -> Any:
    async def scenario() -> Any:
        async with Client(build_server()) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(scenario)


def test_server_lists_the_f1_tools() -> None:
    async def scenario() -> list[str]:
        async with Client(build_server()) as client:
            tools = await client.list_tools()
            return sorted(tool.name for tool in tools.tools)

    assert anyio.run(scenario) == ["calculate_deadline"]


def test_calculate_deadline_returns_structured_output() -> None:
    result = call("calculate_deadline", {"start_date": "2026-12-03", "business_days": 15})

    assert not result.is_error
    data = result.structured_content
    assert data["due_date"] == "2027-01-26"
    assert data["data_class"] == "public"
    assert any("art. 220" in p for p in data["premises"])


def test_calculate_deadline_rejects_invalid_days() -> None:
    result = call("calculate_deadline", {"start_date": "2026-12-03", "business_days": 0})

    assert result.is_error


def test_calculate_deadline_is_marked_read_only() -> None:
    async def scenario() -> Any:
        async with Client(build_server()) as client:
            [tool] = (await client.list_tools()).tools
            return tool.annotations

    annotations = anyio.run(scenario)
    assert annotations.read_only_hint is True
    assert annotations.open_world_hint is False
