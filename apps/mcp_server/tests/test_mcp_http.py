"""The same server over Streamable HTTP, stateless, on a local port."""

import socket
import threading
import time
from collections.abc import Iterator

import anyio
import pytest
import uvicorn
from mcp import Client

from ementa_mcp.server import build_server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def http_url() -> Iterator[str]:
    def unused() -> object:
        raise AssertionError("the deadline tool needs no database or model")

    app = build_server(knowledge=unused, agent=unused).streamable_http_app(  # type: ignore[arg-type]
        stateless_http=True, json_response=True
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise TimeoutError("HTTP server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


def test_tools_over_streamable_http(http_url: str) -> None:
    async def run() -> tuple[list[str], dict[str, object]]:
        async with Client(http_url) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "calculate_deadline", {"start_date": "2026-12-03", "business_days": 15}
            )
            return sorted(t.name for t in tools.tools), result.structured_content or {}

    names, deadline = anyio.run(run)

    assert {"calculate_deadline", "get_statute_article", "search_legal_corpus"} <= set(names)
    assert deadline["due_date"] == "2027-01-26"
