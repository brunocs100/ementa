"""Policy primitives shared by tools and guardrails."""

from enum import StrEnum


class DataClass(StrEnum):
    """What a tool returns. It decides what may leave to a cloud model or an external MCP client."""

    PUBLIC = "public"
    RESTRICTED = "restricted"
    SECRET = "secret"


__all__ = ["DataClass"]
