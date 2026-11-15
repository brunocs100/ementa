"""What goes to the model, in a fixed order: stable system prompt (cached by the provider),
then each section, then the question last. The size of each part goes to the trace.

The conversation only grows. A message the model has seen is never edited: corrections go in
as new messages. That keeps the provider's prompt cache valid and makes switching providers
safe.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from opentelemetry import trace

QUESTION = "pergunta"
SIZES_EVENT = "ementa.context.section_tokens"


@dataclass(frozen=True)
class Section:
    name: str
    content: str

    def render(self) -> str:
        return f"<{self.name}>\n{self.content.strip()}\n</{self.name}>"


def approx_tokens(text: str) -> int:
    """About 4 characters per token: enough to see what fills the context. The exact count is
    the provider's usage on the `chat` span."""
    return math.ceil(len(text) / 4)


def build_user_turn(sections: Sequence[Section], question: str) -> HumanMessage:
    parts = [s.render() for s in sections]
    parts.append(Section(QUESTION, question).render())
    return HumanMessage("\n\n".join(parts))


def build_context(
    system_prompt: str, sections: Sequence[Section], question: str
) -> list[BaseMessage]:
    """The opening messages: system prompt, then one user turn with the sections and the
    question. Records the size of each part as the `ementa.context.section_tokens` event on
    the current span: `<part>.chars` and `<part>.tokens` for `system`, each section and the
    question."""
    parts = [
        ("system", system_prompt),
        *((s.name, s.render()) for s in sections),
        (QUESTION, Section(QUESTION, question).render()),
    ]
    sizes: dict[str, int] = {}
    for name, text in parts:  # a repeated section name adds up
        sizes[f"{name}.chars"] = sizes.get(f"{name}.chars", 0) + len(text)
        sizes[f"{name}.tokens"] = sizes.get(f"{name}.tokens", 0) + approx_tokens(text)
    trace.get_current_span().add_event(SIZES_EVENT, sizes)
    return [SystemMessage(system_prompt), build_user_turn(sections, question)]


class Conversation:
    def __init__(self, system_prompt: str) -> None:
        self._messages: list[BaseMessage] = [SystemMessage(system_prompt)]

    @property
    def messages(self) -> tuple[BaseMessage, ...]:
        return tuple(self._messages)

    def append(self, message: BaseMessage) -> None:
        if isinstance(message, SystemMessage):
            raise ValueError("the system prompt is frozen when the conversation starts")
        self._messages.append(message)

    def sizes(self) -> list[int]:
        """Characters per message, for the trace."""
        return [len(m.text) for m in self._messages]
