"""Context assembly with fixed sections and an append-only conversation."""

from ementa_kit.context.assembler import (
    SIZES_EVENT,
    Conversation,
    Section,
    approx_tokens,
    build_context,
    build_user_turn,
)
from ementa_kit.context.prefix import PrefixChangedError, assert_append_only

__all__ = [
    "SIZES_EVENT",
    "Conversation",
    "PrefixChangedError",
    "Section",
    "approx_tokens",
    "assert_append_only",
    "build_context",
    "build_user_turn",
]
