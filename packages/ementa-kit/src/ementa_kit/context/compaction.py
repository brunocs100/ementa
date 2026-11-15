"""Compaction: past its token budget, a conversation goes on as a new one that opens with a
summary of the old.

The old conversation is never edited (the append-only rule). Even the summary is asked for by
appending one instruction to it, so that request reuses the prefix the provider has cached, and
the new conversation keeps the same system prompt.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from opentelemetry import trace

from ementa_kit.context.assembler import Section, approx_tokens
from ementa_kit.prompts import Prompt

SUMMARY = "resumo"
SUMMARY_TOKENS = 800  # the budget of the summary section (docs/ARQUITETURA.md)
COMPACTED_EVENT = "ementa.context.compacted"


@dataclass(frozen=True)
class Compaction:
    """`messages` open the new conversation: the old system prompt, if any, then the summary."""

    messages: list[BaseMessage]
    summary: str
    tokens_before: int
    tokens_after: int


def conversation_tokens(messages: Sequence[BaseMessage]) -> int:
    """Approximate tokens of a conversation, 4 characters each, as the context sections count."""
    return sum(approx_tokens(m.text) for m in messages)


def compact(
    messages: Sequence[BaseMessage],
    model: Runnable[LanguageModelInput, BaseMessage],
    prompt: Prompt,
    *,
    budget: int,
    summary_tokens: int = SUMMARY_TOKENS,
) -> Compaction | None:
    """None while `messages` fit in `budget` tokens. Past it, `model` summarizes them, with
    `prompt` appended as a new message, and the result opens a new conversation: the same system
    prompt, then the summary in a `resumo` section, cut at `summary_tokens`.

    Call it between turns, before the next question goes in. `messages` are left as they are;
    the `ementa.context.compacted` event on the current span records the sizes and the prompt.
    `model` may carry the conversation's tools, bound with `tool_choice="none"`, so the request
    keeps the whole prefix the provider has cached, tool list included.
    """
    before = conversation_tokens(messages)
    if before <= budget:
        return None
    reply = model.invoke([*messages, HumanMessage(prompt.text)])
    summary = _cut(reply.text.strip(), summary_tokens)
    system = [m for m in messages[:1] if isinstance(m, SystemMessage)]
    opening: list[BaseMessage] = [*system, HumanMessage(Section(SUMMARY, summary).render())]
    after = conversation_tokens(opening)
    trace.get_current_span().add_event(
        COMPACTED_EVENT,
        {
            "messages.before": len(messages),
            "tokens.before": before,
            "tokens.after": after,
            "ementa.prompt.id": prompt.id,
            "ementa.prompt.version": prompt.version,
        },
    )
    return Compaction(opening, summary, before, after)


def _cut(text: str, max_tokens: int) -> str:
    """At most `max_tokens` (approximate), cut at a word boundary with an ellipsis."""
    limit = max_tokens * 4
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    space = head.rfind(" ")
    if space > limit // 2:
        head = head[:space]
    return head.rstrip() + "…"
