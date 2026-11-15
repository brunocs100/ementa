from typing import Any

import anyio
import pytest
from mcp import Client
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from ementa.tools import deadlines
from ementa_kit.policy import DataClass
from ementa_mcp.policy import Decision, OutputPolicy, ToolDeniedError, client_label
from ementa_mcp.server import build_server


class Card(BaseModel):
    client: str
    notes: list[str]
    hours: int


CARD = Card(client="Maria Souza, CPF 123.456.789-09", notes=["ligar para Maria Souza"], hours=3)


def fake_protect(text: str) -> str:
    return text.replace("Maria Souza", "[PESSOA_1]").replace("123.456.789-09", "[CPF_1]")


def test_public_output_goes_out_unchanged() -> None:
    assert OutputPolicy().apply("t", DataClass.PUBLIC, CARD, "vscode") is CARD


def test_restricted_output_is_pseudonymized_in_every_text_field() -> None:
    out = OutputPolicy(fake_protect).apply("t", DataClass.RESTRICTED, CARD, "vscode")

    assert out == Card(client="[PESSOA_1], CPF [CPF_1]", notes=["ligar para [PESSOA_1]"], hours=3)
    assert CARD.client.startswith("Maria")  # the original is not mutated


def test_restricted_without_a_pseudonymizer_fails_closed() -> None:
    assert OutputPolicy().decide(DataClass.RESTRICTED) is Decision.DENIED
    with pytest.raises(ToolDeniedError):
        OutputPolicy().apply("t", DataClass.RESTRICTED, CARD, "vscode")


def test_secret_output_is_denied_even_with_a_pseudonymizer() -> None:
    with pytest.raises(ToolDeniedError, match=r"sigiloso|secret"):
        OutputPolicy(fake_protect).apply("lookup_case", DataClass.SECRET, CARD, "vscode")


def test_decision_and_client_are_recorded_on_the_span_without_content() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("t").start_as_current_span("execute_tool t"):
        OutputPolicy(fake_protect).apply("t", DataClass.RESTRICTED, CARD, "copilot/1.0")

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["ementa.mcp.client"] == "copilot/1.0"
    assert span.attributes["ementa.policy.decision"] == "pseudonymized"
    assert "Maria" not in str(dict(span.attributes))


def test_client_label() -> None:
    assert client_label(None) == "unknown"
    assert client_label("Visual Studio Code", "1.146.0") == "Visual Studio Code/1.146.0"
    assert client_label("claude-code") == "claude-code"


# The policy wired into the server: a tool's data class decides what reaches the MCP client.


def _call_deadline(server: Any) -> Any:
    async def scenario() -> Any:
        async with Client(server) as client:
            return await client.call_tool(
                "calculate_deadline", {"start_date": "2026-12-03", "business_days": 15}
            )

    return anyio.run(scenario)


def test_a_tool_marked_secret_is_denied_to_the_mcp_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deadlines, "DATA_CLASS", DataClass.SECRET)

    result = _call_deadline(build_server(policy=OutputPolicy(fake_protect)))

    assert result.is_error
    assert "não responde a cliente MCP externo" in result.content[0].text


def test_a_restricted_tool_goes_out_pseudonymized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deadlines, "DATA_CLASS", DataClass.RESTRICTED)

    def protect(text: str) -> str:
        return text.replace("CPC", "[LEI_1]")

    result = _call_deadline(build_server(policy=OutputPolicy(protect)))

    assert not result.is_error
    premises = " ".join(result.structured_content["premises"])
    assert "[LEI_1]" in premises
    assert "CPC" not in premises
