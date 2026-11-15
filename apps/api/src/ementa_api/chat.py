"""The `assistant` over HTTP: `POST /v1/chat` streams a turn as Server-Sent Events and
`GET /v1/threads/{id}` reads a conversation back, for its owner only."""

from collections.abc import Callable, Iterator, Sequence
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.sse import EventSourceResponse
from pydantic import BaseModel, ConfigDict, Field

from ementa.agents.assistant import (
    TOOLS,
    ChatEvent,
    CitationEvent,
    DoneEvent,
    StepEvent,
    ThreadHistory,
    ThreadNotFoundError,
    TokenEvent,
)
from ementa_api.auth import Role, User, authorized_user
from ementa_api.problems import (
    Problem,
    problem_for,
    problem_responses,
    stream_problem_responses,
)
from ementa_kit.telemetry import get_logger

log = get_logger(__name__)

CHAT_DESCRIPTION = (
    "Conversa com o assistente da Ementa, que encaminha o pedido ao `legal_research` ou ao "
    "`calculate_deadline`. A resposta é um fluxo SSE (`text/event-stream`): cada evento traz um "
    "JSON em `data`, com o tipo no campo `event`. `step`: um passo começou ou terminou (o modelo, "
    "`supervisor`, ou uma ferramenta). `token`: um pedaço da resposta, palavra inteira, com os "
    "dados pessoais de volta. `citation`: uma fonte verificada pelo `legal_research`. `done`: o "
    "último evento, com o `thread_id` e a resposta final depois da conferência das citações, que "
    "prevalece sobre os tokens. `error`: um problema RFC 9457, quando o erro acontece com o fluxo "
    "já aberto. Sem `thread_id` começa uma conversa nova; com ele, continua uma conversa sua."
)


class Assistant(Protocol):
    def chat(
        self,
        message: str,
        *,
        user_id: str,
        thread_id: str | None = None,
        allowed_tools: Sequence[str] = TOOLS,
    ) -> Iterator[ChatEvent]: ...

    def history(self, thread_id: str, *, user_id: str) -> ThreadHistory: ...


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000, description="Mensagem do usuário")
    thread_id: UUID | None = Field(
        default=None, description="Conversa a continuar; sem ele, começa uma nova"
    )


class ErrorEvent(Problem):
    """An error after the stream started, when no status can be sent: the RFC 9457 problem the
    handlers would have answered with, as the last event."""

    event: Literal["error"] = "error"


StreamEvent = Annotated[
    StepEvent | TokenEvent | CitationEvent | DoneEvent | ErrorEvent,
    Field(discriminator="event"),
]


def any_tool(role: Role, tool: str) -> bool:
    """Every role may use every assistant tool until the role-to-tool matrix is plugged in."""
    return True


def add_chat_routes(
    app: FastAPI,
    assistant: Callable[[], Assistant],
    can_use_tool: Callable[[Role, str], bool] = any_tool,
) -> None:
    def start_turn(
        body: ChatRequest, user: Annotated[User, Depends(authorized_user)]
    ) -> Iterator[ChatEvent]:
        """Opens the turn before the response starts: a thread that is not the caller's is a
        404 and a model without a key a 503, both with their status."""
        allowed = [tool for tool in TOOLS if can_use_tool(user.role, tool)]
        thread_id = str(body.thread_id) if body.thread_id else None
        try:
            return assistant().chat(
                body.message, user_id=user.id, thread_id=thread_id, allowed_tools=allowed
            )
        except ThreadNotFoundError:
            raise HTTPException(status_code=404, detail="Thread not found.") from None

    @app.post(
        "/v1/chat",
        response_class=EventSourceResponse,
        responses=stream_problem_responses(404, 422, 500, 503),
        response_model_exclude_none=True,  # an `error` event reads like the HTTP problem
        summary="Conversa com o assistente, em SSE",
        description=CHAT_DESCRIPTION,
        tags=["chat"],
    )
    def chat(
        request: Request, events: Annotated[Iterator[ChatEvent], Depends(start_turn)]
    ) -> Iterator[StreamEvent]:
        try:
            yield from events
        except Exception as exc:  # the status went out with the first event
            log.error("chat_stream_failed", error=type(exc).__name__)
            yield ErrorEvent(**problem_for(exc, request.url.path).model_dump())

    @app.get(
        "/v1/threads/{thread_id}",
        response_model=ThreadHistory,
        responses=problem_responses(404, 422, 500),
        summary="Histórico de uma conversa",
        description="As mensagens de uma conversa sua, com os dados pessoais como foram digitados "
        "na primeira vez. Conversa de outra pessoa responde 404, como uma que não existe.",
        tags=["chat"],
    )
    def thread(thread_id: UUID, user: Annotated[User, Depends(authorized_user)]) -> ThreadHistory:
        try:
            return assistant().history(str(thread_id), user_id=user.id)
        except ThreadNotFoundError:
            raise HTTPException(status_code=404, detail="Thread not found.") from None
