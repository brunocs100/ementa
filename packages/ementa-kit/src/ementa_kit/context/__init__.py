"""Context assembly with fixed sections, an append-only conversation and its compaction."""

from ementa_kit.context.assembler import (
    SIZES_EVENT,
    Conversation,
    Section,
    approx_tokens,
    build_context,
    build_user_turn,
)
from ementa_kit.context.compaction import (
    COMPACTED_EVENT,
    SUMMARY,
    SUMMARY_TOKENS,
    Compaction,
    compact,
    conversation_tokens,
)
from ementa_kit.context.prefix import PrefixChangedError, assert_append_only

__all__ = [
    "COMPACTED_EVENT",
    "SIZES_EVENT",
    "SUMMARY",
    "SUMMARY_TOKENS",
    "Compaction",
    "Conversation",
    "PrefixChangedError",
    "Section",
    "approx_tokens",
    "assert_append_only",
    "build_context",
    "build_user_turn",
    "compact",
    "conversation_tokens",
]
