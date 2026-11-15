"""`POST /v1/chat` over SSE and `GET /v1/threads/{id}`. The CPF here is fictitious."""

import json
import re
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import Field

from ementa.agents.assistant import (
    TOOLS,
    AssistantAgent,
    ChatEvent,
    CitationEvent,
    DoneEvent,
    HistoryMessage,
    StepEvent,
    ThreadHistory,
    ThreadNotFoundError,
    TokenEvent,
)
from ementa.agents.assistant import graph as assistant_graph
from ementa.agents.assistant.graph import Researcher
from ementa.agents.legal_research import NOTICE
from ementa.pii import pii_guard
from ementa_api.app import create_app
from ementa_api.auth import Role, User, current_user
from ementa_api.chat import Assistant
from ementa_kit.config import Settings
from ementa_kit.llm import ModelRegistry, ModelSpec, ModelUnavailableError

DEV_USER = User(id="dev", name="Desenvolvimento", role=Role.SOCIA)
THREAD = str(uuid.uuid4())
OTHERS = str(uuid.uuid4())


class FakeAssistant:
    """Owns THREAD for the dev user and OTHERS for someone else."""

    def __init__(self, unavailable: bool = False, fail_midway: bool = False) -> None:
        self.unavailable = unavailable
        self.fail_midway = fail_midway
        self.owners = {THREAD: DEV_USER.id, OTHERS: "ana"}
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        message: str,
        *,
        user_id: str,
        thread_id: str | None = None,
        allowed_tools: Sequence[str] = TOOLS,
    ) -> Iterator[ChatEvent]:
        self.calls.append({"user_id": user_id, "thread_id": thread_id, "tools": allowed_tools})
        if thread_id is not None and self.owners.get(thread_id) != user_id:
            raise ThreadNotFoundError(thread_id)
        if self.unavailable:
            raise ModelUnavailableError("OPENAI_API_KEY is not set")
        return self._events(thread_id or THREAD)

    def _events(self, thread_id: str) -> Iterator[ChatEvent]:
        yield StepEvent(name="supervisor", status="started")
        yield TokenEvent(text="Pode ")
        if self.fail_midway:
            raise RuntimeError("secret internals")
        yield TokenEvent(text="desistir [cdc:art49].")
        yield StepEvent(name="supervisor", status="finished")
        yield CitationEvent(source_id="cdc:art49", title="CDC, art. 49", url="https://x")
        yield DoneEvent(thread_id=thread_id, message="Pode desistir [cdc:art49].", removed=[])

    def history(self, thread_id: str, *, user_id: str) -> ThreadHistory:
        if self.owners.get(thread_id) != user_id:
            raise ThreadNotFoundError(thread_id)
        return ThreadHistory(
            thread_id=thread_id,
            messages=[
                HistoryMessage(role="user", content="Posso desistir?"),
                HistoryMessage(role="assistant", content="Pode desistir [cdc:art49]."),
            ],
        )


def client(
    assistant: Assistant | None = None,
    can_use_tool: Callable[[Role, str], bool] | None = None,
    user: User | None = None,
) -> TestClient:
    fake = assistant or FakeAssistant()
    if can_use_tool is None:
        app = create_app(chunks=lambda: 1, assistant=lambda: fake)
    else:
        app = create_app(chunks=lambda: 1, assistant=lambda: fake, can_use_tool=can_use_tool)
    app.dependency_overrides[current_user] = lambda: user or DEV_USER
    return TestClient(app, raise_server_exceptions=False)


def events_of(body: str) -> list[dict[str, Any]]:
    """The JSON in the `data` of each SSE event, in order; keep-alive comments are skipped."""
    events = []
    for block in body.split("\n\n"):
        data = [
            line.removeprefix("data: ") for line in block.splitlines() if line.startswith("data: ")
        ]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


def test_chat_streams_the_turn_as_server_sent_events() -> None:
    fake = FakeAssistant()

    response = client(fake).post("/v1/chat", json={"message": "Posso desistir da compra?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    events = events_of(response.text)
    assert [e["event"] for e in events] == ["step", "token", "token", "step", "citation", "done"]
    assert events[-1] == {
        "event": "done",
        "thread_id": THREAD,
        "message": "Pode desistir [cdc:art49].",
        "removed": [],
        "notice": NOTICE,
    }
    assert fake.calls == [{"user_id": DEV_USER.id, "thread_id": None, "tools": list(TOOLS)}]


def test_the_next_turn_goes_to_the_thread_it_names() -> None:
    fake = FakeAssistant()

    response = client(fake).post("/v1/chat", json={"message": "E agora?", "thread_id": THREAD})

    assert events_of(response.text)[-1]["thread_id"] == THREAD
    assert fake.calls[0]["thread_id"] == THREAD


def test_a_thread_of_someone_else_is_404_before_the_stream() -> None:
    response = client().post("/v1/chat", json={"message": "Oi", "thread_id": OTHERS})

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Thread not found.",
        "instance": "/v1/chat",
    }


CPF = "123.456.789-09"


def test_an_invalid_chat_request_is_a_problem_without_the_values() -> None:
    for payload in (
        {"message": ""},
        {"message": f"Meu CPF é {CPF}", "thread_id": "nao-e-um-uuid"},
        {"message": f"Meu CPF é {CPF}", "cpf": CPF},
    ):
        response = client().post("/v1/chat", json=payload)

        assert response.status_code == 422
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["type"] == "/problems/validation-error"
        assert CPF not in response.text and "nao-e-um-uuid" not in response.text


def test_a_missing_model_key_is_503_before_the_stream() -> None:
    response = client(FakeAssistant(unavailable=True)).post("/v1/chat", json={"message": "Oi"})

    assert response.status_code == 503
    assert response.json()["type"] == "/problems/model-unavailable"


def test_an_error_after_the_stream_started_is_its_last_event() -> None:
    response = client(FakeAssistant(fail_midway=True)).post("/v1/chat", json={"message": "Oi"})

    assert response.status_code == 200  # sent with the first event
    events = events_of(response.text)
    assert [e["event"] for e in events] == ["step", "token", "error"]
    assert events[-1] == {
        "event": "error",
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "instance": "/v1/chat",
    }
    assert "secret" not in response.text


def test_the_callers_role_decides_which_tools_the_assistant_gets() -> None:
    fake = FakeAssistant()
    intern = User(id=DEV_USER.id, name="Estagiária", role=Role.ESTAGIARIO)

    def can_use_tool(role: Role, tool: str) -> bool:
        return not (role is Role.ESTAGIARIO and tool == "legal_research")

    client(fake, can_use_tool, user=intern).post("/v1/chat", json={"message": "Oi"})

    assert fake.calls[0]["tools"] == ["calculate_deadline"]


def test_a_thread_history_is_for_its_owner_only() -> None:
    mine = client().get(f"/v1/threads/{THREAD}")
    theirs = client().get(f"/v1/threads/{OTHERS}")
    unknown = client().get(f"/v1/threads/{uuid.uuid4()}")
    malformed = client().get("/v1/threads/nada")

    assert mine.status_code == 200
    assert [m["role"] for m in mine.json()["messages"]] == ["user", "assistant"]
    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json()  # same answer: nothing tells them apart
    assert theirs.json()["instance"] == "/v1/threads/{thread_id}"  # the template, not the id
    assert malformed.status_code == 422


class EchoChat(BaseChatModel):
    """Answers with the markers it received, as a cloud model would, and keeps what it saw."""

    seen: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "echo"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(messages[-1].text)
        markers = " e ".join(re.findall(r"\[[A-Z]+_[0-9]+\]", messages[-1].text))
        reply = AIMessage(f"Recebi o pedido de {markers}.")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def no_research() -> Researcher:
    raise AssertionError("the echo model calls no tool")


def echo_agent() -> tuple[AssistantAgent, EchoChat]:
    """The real assistant, on the echo model and an in-memory checkpointer."""
    model = EchoChat()

    def factory(spec: ModelSpec) -> EchoChat:
        return model

    registry = ModelRegistry(Settings(_env_file=None), factory=factory)  # type: ignore[call-arg]
    return AssistantAgent(registry, no_research, InMemorySaver(), pii_guard), model


def test_cpf_in_the_chat_reaches_the_model_as_a_marker_and_the_user_as_typed() -> None:
    agent, model = echo_agent()
    http = client(agent)
    message = f"Meu CPF é {CPF} e fui negativado sem aviso. Posso pedir indenização?"

    events = events_of(http.post("/v1/chat", json={"message": message}).text)
    done = events[-1]
    history = http.get(f"/v1/threads/{done['thread_id']}").json()

    assert model.seen == ["Meu CPF é [CPF_1] e fui negativado sem aviso. Posso pedir indenização?"]
    assert done["message"] == f"Recebi o pedido de {CPF}."
    assert "".join(e["text"] for e in events if e["event"] == "token") == done["message"]
    assert [m["content"] for m in history["messages"]] == [message, f"Recebi o pedido de {CPF}."]


def test_a_turn_stays_one_trace_when_the_api_streams_it_from_worker_threads(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """FastAPI pulls each event in a worker thread with its own copy of the context. A span
    held across a yield would be detached in another context and orphan the spans after it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(assistant_graph, "tracer", provider.get_tracer("test"))
    agent, _ = echo_agent()

    response = client(agent).post("/v1/chat", json={"message": "Posso desistir da compra?"})

    assert response.status_code == 200
    spans = {span.name: span for span in exporter.get_finished_spans()}
    root, check = spans["invoke_agent assistant"], spans["guardrail citation_verifier"]
    assert check.parent and check.parent.span_id == root.context.span_id
    assert "Failed to detach context" not in caplog.text
