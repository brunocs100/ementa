"""What a chat turn streams and what a thread's history returns.

Every text here carries the real values back: the model saw `[CPF_1]`, the user sees the CPF they
typed. A turn streams, in order, `step` and `token` events as they happen, a `citation` for each
source that `legal_research` verified, and `done` last.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ementa.agents.legal_research import NOTICE
from ementa.tools.research import CitationOut, RemovedOut


class StepEvent(BaseModel):
    """A step of the turn: the model at work (`supervisor`) or a tool running."""

    event: Literal["step"] = "step"
    name: str = Field(description="supervisor, legal_research ou calculate_deadline")
    status: Literal["started", "finished", "denied", "failed"] = Field(
        description="denied: ferramenta fora das permitidas ao papel; failed: erro na ferramenta"
    )


class TokenEvent(BaseModel):
    """A piece of the reply, whole words with the values in place of the markers."""

    event: Literal["token"] = "token"
    text: str


class CitationEvent(BaseModel):
    """A source that `legal_research` verified, sent as soon as the tool returns."""

    event: Literal["citation"] = "citation"
    source_id: str
    title: str
    url: str


class DoneEvent(BaseModel):
    """The last event. `message` is the reply after the citation check and prevails over the
    joined tokens: a sentence citing a source no tool verified is removed and listed in
    `removed`."""

    event: Literal["done"] = "done"
    thread_id: str = Field(description="Id da conversa, para continuar com o próximo POST")
    message: str
    removed: list[RemovedOut]
    notice: str = NOTICE


ChatEvent = Annotated[
    StepEvent | TokenEvent | CitationEvent | DoneEvent, Field(discriminator="event")
]


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    citations: list[CitationOut] = Field(default_factory=list)


class ThreadHistory(BaseModel):
    """The finished turns of a thread, as the user saw them."""

    thread_id: str
    messages: list[HistoryMessage]
