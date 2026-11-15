"""What goes to the model, in a fixed order: stable system prompt (cached by the provider),
then each section, then the question last.

The conversation only grows. A message the model has seen is never edited: corrections go in
as new messages. That keeps the provider's prompt cache valid and makes switching providers
safe.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage


@dataclass(frozen=True)
class Section:
    name: str
    content: str

    def render(self) -> str:
        return f"<{self.name}>\n{self.content.strip()}\n</{self.name}>"


def build_user_turn(sections: Sequence[Section], question: str) -> HumanMessage:
    parts = [s.render() for s in sections]
    parts.append(Section("pergunta", question).render())
    return HumanMessage("\n\n".join(parts))


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
