"""Output policy for MCP clients.

An MCP client (Copilot, Claude Desktop, any other) runs its own model in the cloud, so whatever a
tool returns leaves the firm. Each tool declares the class of data it returns:

- public: goes out as is (statutes, súmulas, deadline math);
- restricted: every text field is pseudonymized before it goes out; with no pseudonymizer
  configured the call is denied, so a missing guardrail never leaks data;
- secret: always denied to an MCP client.

Every decision is recorded on the tool span and in the log, with the client name, never the content.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar

from opentelemetry import trace
from pydantic import BaseModel

from ementa_kit.policy import DataClass
from ementa_kit.telemetry import get_logger

T = TypeVar("T", bound=BaseModel)

Protector = Callable[[str], str]
"""Pseudonymizes one text, e.g. "CPF 123.456.789-09" -> "CPF [CPF_1]"."""

UNKNOWN_CLIENT = "unknown"

log = get_logger(__name__)


class Decision(StrEnum):
    ALLOWED = "allowed"
    PSEUDONYMIZED = "pseudonymized"
    DENIED = "denied"


class ToolDeniedError(PermissionError):
    """The tool's data class may not be returned to this client."""


def client_label(name: str | None, version: str | None = None) -> str:
    if not name:
        return UNKNOWN_CLIENT
    return f"{name}/{version}" if version else name


def _protect_value(value: Any, protect: Protector) -> Any:
    if isinstance(value, str):
        return protect(value)
    if isinstance(value, list):
        return [_protect_value(v, protect) for v in value]
    if isinstance(value, dict):
        return {k: _protect_value(v, protect) for k, v in value.items()}
    return value


class OutputPolicy:
    def __init__(self, protect: Protector | None = None) -> None:
        self._protect = protect

    def decide(self, data_class: DataClass) -> Decision:
        if data_class is DataClass.PUBLIC:
            return Decision.ALLOWED
        if data_class is DataClass.RESTRICTED and self._protect is not None:
            return Decision.PSEUDONYMIZED
        return Decision.DENIED

    def apply(self, tool: str, data_class: DataClass, output: T, client: str) -> T:
        """Returns what may go to `client`, or raises `ToolDeniedError`."""
        decision = self.decide(data_class)
        span = trace.get_current_span()
        span.set_attribute("ementa.mcp.client", client)
        span.set_attribute("ementa.policy.decision", decision.value)
        log.info(
            "mcp_output_policy",
            tool=tool,
            client=client,
            data_class=data_class.value,
            decision=decision.value,
        )
        if decision is Decision.DENIED:
            raise ToolDeniedError(
                f"A ferramenta {tool} devolve dado {data_class.value} e não responde a cliente "
                "MCP externo. Use o console da Ementa."
            )
        if decision is Decision.PSEUDONYMIZED:
            assert self._protect is not None
            data = _protect_value(output.model_dump(mode="json"), self._protect)
            return type(output).model_validate(data)
        return output
