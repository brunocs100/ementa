"""The `assistant` supervisor with scripted models: routing, personal data, memory and traces.

Every name and CPF here is fictitious. The Postgres tests need EMENTA_TEST_DATABASE_URL.
"""

import json
import os
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import sql
from pydantic import Field
from structlog.testing import capture_logs

from ementa.agents.assistant import (
    AssistantAgent,
    ChatEvent,
    CitationEvent,
    DoneEvent,
    StepEvent,
    ThreadNotFoundError,
    TokenEvent,
)
from ementa.agents.assistant import graph as assistant_graph
from ementa.agents.assistant.graph import DENIED, FAILED, INTERRUPTED, MALFORMED, MAX_ROUNDS
from ementa.agents.legal_research import Citation, LegalResearchAgent, ResearchAnswer
from ementa.agents.legal_research import graph as research_graph
from ementa.memory import EXTRACT_PROMPT, FIRM, case, lawyer, relevant_memories
from ementa.pii import pii_guard
from ementa.prompts import get_prompt
from ementa_kit.config import Settings
from ementa_kit.context import assert_append_only
from ementa_kit.llm import ModelRegistry, ModelRole, ModelSpec, ModelUnavailableError
from ementa_kit.memory import MemoryWriter, Namespace, memory_value, postgres_checkpointer
from ementa_kit.rag import Hit


class ScriptedChat(BaseChatModel):
    """Replies in order and records each request (a deep copy) and the tools it offered.

    It streams the text three characters at a time, so a marker arrives split, and the tool
    calls in a last chunk, as a provider does."""

    replies: list[AIMessage]
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    offered: list[tuple[list[str], str | None]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        names = [t["function"]["name"] for t in tools if isinstance(t, dict)]
        return self.bind(tools=names, tool_choice=tool_choice)

    def _reply(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> AIMessage:
        self.calls.append([m.model_copy(deep=True) for m in messages])
        self.offered.append((kwargs.get("tools", []), kwargs.get("tool_choice")))
        return self.replies.pop(0)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages, kwargs))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        reply = self._reply(messages, kwargs)
        for start in range(0, len(reply.text), 3):
            yield ChatGenerationChunk(message=AIMessageChunk(content=reply.text[start : start + 3]))
        raw: list[tuple[str | None, str | None, str | None]]
        raw = [(c["name"], json.dumps(c["args"]), c["id"]) for c in reply.tool_calls]
        raw += [(c["name"], c["args"], c["id"]) for c in reply.invalid_tool_calls]
        if raw:
            chunks = [
                tool_call_chunk(name=name, args=args, id=call_id, index=n)
                for n, (name, args, call_id) in enumerate(raw)
            ]
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=chunks))


def tool_call(name: str, call_id: str = "call_1", **args: Any) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])


QUESTION = "Comprei pela internet e me arrependi. Posso desistir?"
RESEARCH = "O consumidor pode desistir em 7 dias da compra fora do estabelecimento [cdc:art49]."
REPLY = "Pode, em 7 dias contados da compra ou do recebimento [cdc:art49]."


class FakeResearcher:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.questions: list[str] = []

    def ask(self, question: str) -> ResearchAnswer:
        self.questions.append(question)
        if self.error:
            raise self.error
        return ResearchAnswer(
            question=question,
            answer=RESEARCH,
            citations=[Citation("cdc:art49", "Código de Defesa do Consumidor, art. 49", "u")],
            removed=[],
            sources=["cdc:art49"],
            queries=[question],
            attempts=1,
            prompt="legal_research.answer@2",
            model="fake",
        )


def registry_of(model: ScriptedChat) -> ModelRegistry:
    """Every model role answered by `model`."""

    def factory(spec: ModelSpec) -> ScriptedChat:
        return model

    return ModelRegistry(Settings(_env_file=None), factory=factory)  # type: ignore[call-arg]


def make_agent(
    replies: list[AIMessage],
    researcher: FakeResearcher | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    *,
    memories: Callable[[], BaseStore] | None = None,
    writer: Callable[[], MemoryWriter] | None = None,
) -> tuple[AssistantAgent, ScriptedChat, FakeResearcher]:
    fast = ScriptedChat(replies=replies)
    research = researcher or FakeResearcher()
    agent = AssistantAgent(
        registry_of(fast),
        lambda: research,
        checkpointer or InMemorySaver(),
        pii_guard,
        memories=memories,
        writer=writer,
    )
    return agent, fast, research


def done(events: Sequence[ChatEvent]) -> DoneEvent:
    last = events[-1]
    assert isinstance(last, DoneEvent)
    return last


def tokens(events: Iterable[ChatEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, TokenEvent))


def steps(events: Iterable[ChatEvent]) -> list[tuple[str, str]]:
    return [(e.name, e.status) for e in events if isinstance(e, StepEvent)]


def test_a_legal_question_goes_to_legal_research_and_the_reply_streams() -> None:
    agent, fast, research = make_agent(
        [tool_call("legal_research", question=QUESTION), AIMessage(REPLY)]
    )

    events = list(agent.chat(QUESTION, user_id="ana"))

    assert research.questions == [QUESTION]
    assert steps(events) == [
        ("supervisor", "started"),
        ("supervisor", "finished"),
        ("legal_research", "started"),
        ("legal_research", "finished"),
        ("supervisor", "started"),
        ("supervisor", "finished"),
    ]
    assert [e.source_id for e in events if isinstance(e, CitationEvent)] == ["cdc:art49"]
    assert done(events).message == tokens(events) == REPLY
    assert done(events).removed == []
    assert fast.offered == [(["legal_research", "calculate_deadline"], "auto")] * 2
    result = fast.calls[1][-1]
    assert isinstance(result, ToolMessage)
    assert json.loads(result.text) == {
        "answer": RESEARCH,
        "citations": ["cdc:art49"],
        "grounded": True,
    }
    assert_append_only(fast.calls)  # the tool result was appended, nothing the model saw changed


def test_personal_data_reaches_the_models_as_markers_and_the_user_as_typed() -> None:
    message = "Sou Maria Souza, CPF 123.456.789-09. Posso desistir da compra online?"
    agent, fast, research = make_agent(
        [
            tool_call("legal_research", question="[PESSOA_1] pode desistir da compra online?"),
            AIMessage("[PESSOA_1], CPF [CPF_1]: pode desistir em 7 dias [cdc:art49]."),
        ]
    )

    events = list(agent.chat(message, user_id="ana"))

    sent = [m.text for request in fast.calls for m in request] + research.questions
    assert not any("123.456.789-09" in text or "Maria" in text for text in sent)
    assert fast.calls[0][-1].text == "Sou [PESSOA_1], CPF [CPF_1]. Posso desistir da compra online?"
    expected = "Maria Souza, CPF 123.456.789-09: pode desistir em 7 dias [cdc:art49]."
    assert done(events).message == expected
    assert tokens(events) == expected  # the markers came split in pieces and went out whole
    assert all("[" not in e.text or "cdc" in e.text for e in events if isinstance(e, TokenEvent))


def test_a_deadline_is_counted_by_the_tool_never_by_the_model() -> None:
    reply = "O prazo vence em 26/01/2027. Só dias úteis contam (CPC, art. 219)."
    agent, fast, _ = make_agent(
        [
            tool_call("calculate_deadline", start_date="2026-12-03", business_days=15),
            AIMessage(reply),
        ]
    )

    events = list(
        agent.chat("Intimação em 03/12/2026, prazo de 15 dias úteis. Vence quando?", user_id="ana")
    )

    result = fast.calls[1][-1]
    assert isinstance(result, ToolMessage) and result.name == "calculate_deadline"
    seen = json.loads(result.text)
    assert seen["due_date"] == "2027-01-26"
    assert "counted_days" not in seen  # kept in the artifact, out of the model's context
    assert len(result.artifact["counted_days"]) == 15
    assert steps(events)[2:4] == [
        ("calculate_deadline", "started"),
        ("calculate_deadline", "finished"),
    ]
    assert done(events).message == reply  # a premise may name an article in prose
    assert not any(isinstance(e, CitationEvent) for e in events)


def test_only_the_allowed_tools_are_offered_and_run() -> None:
    agent, fast, research = make_agent(
        [tool_call("legal_research", question=QUESTION), AIMessage("Seu perfil não pesquisa.")]
    )

    events = list(agent.chat(QUESTION, user_id="bia", allowed_tools=["calculate_deadline"]))

    assert fast.offered[0] == (["calculate_deadline"], "auto")
    assert research.questions == []  # a call the model was not offered is refused
    assert ("legal_research", "denied") in steps(events)
    refused = fast.calls[1][-1]
    assert isinstance(refused, ToolMessage)
    assert (refused.text, refused.status) == (DENIED, "error")


def test_without_allowed_tools_the_model_gets_none() -> None:
    agent, fast, _ = make_agent([AIMessage("Posso explicar o que faço.")])

    list(agent.chat("Olá", user_id="ana", allowed_tools=[]))

    assert fast.offered == [([], None)]


def test_bad_tool_arguments_go_back_to_the_model_without_their_values() -> None:
    agent, fast, _ = make_agent(
        [
            tool_call("calculate_deadline", start_date="ontem à tarde", business_days=0),
            AIMessage("Preciso da data da intimação."),
        ]
    )

    events = list(agent.chat("Quando vence o prazo?", user_id="ana"))

    result = fast.calls[1][-1].text
    assert ("calculate_deadline", "failed") in steps(events)
    assert result.startswith("Argumentos inválidos: start_date:")
    assert "business_days:" in result and "ontem" not in result


def test_a_call_with_broken_json_gets_a_result_so_the_history_stays_valid() -> None:
    broken = AIMessage(
        "",
        invalid_tool_calls=[
            {"name": "legal_research", "args": "pergunta sem JSON", "id": "call_9", "error": None}
        ],
    )
    agent, fast, research = make_agent([broken, AIMessage("Reformule, por favor.")])

    events = list(agent.chat(QUESTION, user_id="ana"))

    assert research.questions == []
    assert ("legal_research", "failed") in steps(events)
    result = fast.calls[1][-1]
    assert isinstance(result, ToolMessage)
    assert (result.tool_call_id, result.text) == ("call_9", MALFORMED)
    assert done(events).message == "Reformule, por favor."


def test_a_failing_tool_is_told_to_the_model_and_the_turn_goes_on() -> None:
    research = FakeResearcher(error=RuntimeError("banco fora do ar"))
    agent, fast, _ = make_agent(
        [
            tool_call("legal_research", question=QUESTION),
            AIMessage("Não consegui pesquisar agora."),
        ],
        researcher=research,
    )

    events = list(agent.chat(QUESTION, user_id="ana"))

    assert ("legal_research", "failed") in steps(events)
    assert fast.calls[1][-1].text == FAILED
    assert done(events).message == "Não consegui pesquisar agora."


def test_a_reply_citing_what_no_tool_verified_loses_that_sentence() -> None:
    agent, _, _ = make_agent(
        [
            tool_call("legal_research", question=QUESTION),
            AIMessage(f"{REPLY} A Súmula 999 do STJ dobra o prazo [stj:sumula:999]."),
        ]
    )

    events = list(agent.chat(QUESTION, user_id="ana"))

    assert done(events).message == REPLY
    assert [r.reference for r in done(events).removed] == ["stj:sumula:999"]


def test_the_next_turn_appends_to_the_thread_and_keeps_its_markers() -> None:
    agent, fast, _ = make_agent(
        [AIMessage("Anotado, [PESSOA_1]."), AIMessage("Sim, [CPF_1] é o CPF de [PESSOA_1].")]
    )
    first = list(agent.chat("Sou Maria Souza, CPF 123.456.789-09.", user_id="ana"))
    thread = done(first).thread_id
    frozen = agent.prompt.text
    agent.prompt = replace(agent.prompt, text="Um prompt novo, numa versão nova.")

    second = list(agent.chat("O CPF 12345678909 é o meu?", user_id="ana", thread_id=thread))

    assert_append_only(fast.calls)  # turn 2 only appended to what the model saw in turn 1
    assert fast.calls[1][0].text == frozen  # the system prompt froze when the thread started
    assert fast.calls[1][-1].text == "O CPF [CPF_1] é o meu?"  # same CPF, other punctuation
    assert done(second).message == "Sim, 123.456.789-09 é o CPF de Maria Souza."
    history = agent.history(thread, user_id="ana")
    assert [(m.role, m.content) for m in history.messages] == [
        ("user", "Sou Maria Souza, CPF 123.456.789-09."),
        ("assistant", "Anotado, Maria Souza."),
        ("user", "O CPF 123.456.789-09 é o meu?"),  # a value comes back as first typed
        ("assistant", "Sim, 123.456.789-09 é o CPF de Maria Souza."),
    ]


def test_a_thread_is_found_only_by_its_owner() -> None:
    agent, _, _ = make_agent([AIMessage("Olá.")])
    thread = done(list(agent.chat("Olá", user_id="ana"))).thread_id

    with pytest.raises(ThreadNotFoundError):
        agent.chat("Oi", user_id="bruno", thread_id=thread)  # raises before any event
    with pytest.raises(ThreadNotFoundError):
        agent.history(thread, user_id="bruno")
    with pytest.raises(ThreadNotFoundError):
        agent.chat("Oi", user_id="ana", thread_id=str(uuid.uuid4()))


def test_a_missing_model_key_fails_before_the_first_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OPENAI_API_KEY", "EMENTA_OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    registry = ModelRegistry(Settings(_env_file=None))  # type: ignore[call-arg]
    agent = AssistantAgent(registry, FakeResearcher, InMemorySaver(), pii_guard)

    with pytest.raises(ModelUnavailableError):
        agent.chat(QUESTION, user_id="ana")


def test_after_the_last_round_the_model_has_to_answer() -> None:
    calls = [
        tool_call("calculate_deadline", f"call_{n}", start_date="2026-12-03", business_days=15)
        for n in range(MAX_ROUNDS)
    ]
    agent, fast, _ = make_agent([*calls, AIMessage("Vence em 26/01/2027.")])

    events = list(agent.chat("Quando vence?", user_id="ana"))

    assert [choice for _, choice in fast.offered] == ["auto"] * MAX_ROUNDS + ["none"]
    assert done(events).message == "Vence em 26/01/2027."


class Crash(BaseException):
    """Stands for the process dying mid-turn: no `except Exception` stops it."""


def test_a_tool_call_left_open_by_a_crash_is_closed_before_the_next_turn() -> None:
    agent, fast, research = make_agent(
        [AIMessage("Olá."), tool_call("legal_research", question=QUESTION), AIMessage("Pronto.")]
    )
    thread = done(list(agent.chat("Olá", user_id="ana"))).thread_id
    research.error = Crash()
    with pytest.raises(Crash):
        list(agent.chat(QUESTION, user_id="ana", thread_id=thread))
    research.error = None

    list(agent.chat("E então?", user_id="ana", thread_id=thread))

    last = fast.calls[-1]
    assert [m.type for m in last[-3:]] == ["ai", "tool", "human"]
    assert last[-2].text == INTERRUPTED
    assert_append_only(fast.calls)  # closed by appending, not by editing


# Long-term memory ---------------------------------------------------------------------------


def remember(store: BaseStore, namespace: Namespace, text: str) -> None:
    store.put(namespace.path, uuid.uuid4().hex, memory_value(namespace, text, [], 0.9))


def test_memories_reach_the_model_masked_in_the_new_turn_and_stay_out_of_the_history() -> None:
    store = InMemoryStore()
    remember(store, lawyer("ana"), "Prefere respostas em tópicos.")
    remember(store, case("c1"), "A cliente Maria Souza, CPF 123.456.789-09, quer acordo.")
    remember(store, FIRM, "Cobrança indevida se devolve em dobro [cdc:art42:pu].")
    remember(store, lawyer("bia"), "Prefere textos longos.")  # someone else's preference
    agent, fast, _ = make_agent(
        [AIMessage("[PESSOA_1] quer acordo; o CPF é [CPF_1]."), AIMessage("Em tópicos.")],
        memories=lambda: store,
    )
    question = "Chegou a proposta de Maria Souza. Ela quer acordo?"

    first = list(agent.chat(question, user_id="ana", case_id="c1"))
    thread = done(first).thread_id
    list(agent.chat("E o formato?", user_id="ana", thread_id=thread))

    assert fast.calls[0][-1].text == (
        "<memorias>\n"
        "Preferências do advogado:\n- Prefere respostas em tópicos.\n"
        "Ficha do processo:\n- A cliente [PESSOA_1], CPF [CPF_1], quer acordo.\n"
        "Teses do escritório:\n- Cobrança indevida se devolve em dobro [cdc:art42:pu].\n"
        "</memorias>\n\n"
        "<pergunta>\nChegou a proposta de [PESSOA_1]. Ela quer acordo?\n</pergunta>"
    )
    sent = [m.text for request in fast.calls for m in request]
    assert not any("Maria" in t or "123.456.789-09" in t or "longos" in t for t in sent)
    reply = "Maria Souza quer acordo; o CPF é 123.456.789-09."  # the memory's marker came back
    assert done(first).message == tokens(first) == reply
    assert "Ficha do processo" not in fast.calls[1][-1].text  # no case named in turn 2
    assert_append_only(fast.calls)  # turn 2 came in a new message; turn 1's stayed as it was
    history = agent.history(thread, user_id="ana")
    assert [m.content for m in history.messages] == [question, reply, "E o formato?", "Em tópicos."]


def extraction(*memories: dict[str, Any]) -> AIMessage:
    """What the memory writer's model answers."""
    return AIMessage(json.dumps({"memories": list(memories)}))


def writer_of(store: BaseStore, model: ScriptedChat) -> MemoryWriter:
    """The writer as `services.memory_writer()` builds it, on `model`."""
    return MemoryWriter(
        store,
        registry_of(model),
        pii_guard,
        get_prompt(*EXTRACT_PROMPT),
        subject_key=b"chave-de-teste",
        similarity=0.97,
        min_confidence=0.7,
    )


def test_after_done_the_turn_is_written_with_the_markers_of_the_thread() -> None:
    store = InMemoryStore()
    extractor = ScriptedChat(
        replies=[
            extraction(
                {"scope": "lawyer", "text": "Prefere respostas em tópicos.", "confidence": 0.9},
                {"scope": "case", "text": "[PESSOA_1] quer acordo.", "confidence": 0.8},
            ),
            extraction(),
        ]
    )
    agent, fast, _ = make_agent(
        [AIMessage("Anotado, [PESSOA_1]."), AIMessage("Em tópicos: [PESSOA_1] quer acordo.")],
        memories=lambda: store,
        writer=lambda: writer_of(store, extractor),
    )

    for event in agent.chat(
        "Maria Souza quer acordo. Responda sempre em tópicos.", user_id="ana", case_id="c1"
    ):
        if isinstance(event, DoneEvent):
            assert extractor.calls == []  # the user has the reply before anything is written

    [request] = extractor.calls
    assert "<escopos>\nlawyer, case\n</escopos>" in request[-1].text  # chat_targets("ana", "c1")
    assert "usuário: [PESSOA_1] quer acordo." in request[-1].text  # the thread's own marker
    assert "Maria" not in request[-1].text
    found = relevant_memories(store, "acordo", user_id="ana", case_id="c1")
    assert {(m.namespace, m.text) for m in found} == {
        (lawyer("ana"), "Prefere respostas em tópicos."),
        (case("c1"), "Maria Souza quer acordo."),
    }
    list(agent.chat("Como ela quer resolver?", user_id="ana", case_id="c1"))  # a new thread
    assert "Ficha do processo:\n- [PESSOA_1] quer acordo." in fast.calls[1][-1].text


def test_a_memory_store_that_fails_costs_the_turn_its_memories_not_its_reply() -> None:
    def no_store() -> BaseStore:
        raise RuntimeError("banco fora do ar")

    def no_writer() -> MemoryWriter:
        raise RuntimeError("banco fora do ar")

    agent, fast, _ = make_agent(
        [AIMessage("Olá."), AIMessage("Pronto.")], memories=no_store, writer=no_writer
    )

    with capture_logs() as logs:
        first = list(agent.chat("Bom dia", user_id="ana"))
        second = list(agent.chat("E agora?", user_id="ana", thread_id=done(first).thread_id))

    assert (done(first).message, done(second).message) == ("Olá.", "Pronto.")
    assert fast.calls[0][-1].text == "Bom dia"  # the message alone, with no memory section
    warnings = [(e["event"], e["error"]) for e in logs if e["log_level"] == "warning"]
    each_turn = [("memory_read_failed", "RuntimeError"), ("memory_write_failed", "RuntimeError")]
    assert warnings == each_turn * 2


ART49 = Hit(
    "cdc:art49",
    "cdc",
    "Código de Defesa do Consumidor, art. 49",
    ["Título I"],
    "Art. 49. O consumidor pode desistir do contrato, no prazo de 7 dias.",
    ["cdc:art49"],
    "https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
)


class OneArticle:
    def search(self, queries: Sequence[str], limit: int = 5) -> list[Hit]:
        return [ART49]

    def get(self, source_ids: Iterable[str]) -> list[Hit]:
        return [ART49]


def by_name(spans: Iterable[ReadableSpan]) -> dict[str, ReadableSpan]:
    return {span.name: span for span in spans}


def test_legal_research_runs_inside_the_turn_as_a_conversation_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(assistant_graph, "tracer", provider.get_tracer("test"))
    monkeypatch.setattr(research_graph, "tracer", provider.get_tracer("test"))
    question = "O que diz o art. 49 do CDC?"
    models = {
        ModelRole.FAST: ScriptedChat(
            replies=[
                tool_call("legal_research", question=question),
                AIMessage("Sete dias [cdc:art49]."),
            ]
        ),
        ModelRole.DEFAULT: ScriptedChat(
            replies=[AIMessage("RESPOSTA INTERNA, 7 dias [cdc:art49].")]
        ),
    }

    def factory(spec: ModelSpec) -> ScriptedChat:
        return models[spec.role]

    registry = ModelRegistry(Settings(_env_file=None), factory=factory)  # type: ignore[call-arg]
    saver = InMemorySaver()
    research = LegalResearchAgent(OneArticle(), registry)
    agent = AssistantAgent(registry, lambda: research, saver, pii_guard)

    events = list(agent.chat(question, user_id="ana"))

    assert models[ModelRole.DEFAULT].calls[0][1].text.startswith("<fontes>")  # its own context
    assert "INTERNA" not in tokens(events)  # its tokens are not the chat's
    assert done(events).message == "Sete dias [cdc:art49]."
    namespaces = {c.config["configurable"]["checkpoint_ns"] for c in saver.list(None)}
    assert namespaces == {""}  # nothing of it was stored in the chat's thread
    spans = by_name(exporter.get_finished_spans())
    root, tool = spans["invoke_agent assistant"], spans["execute_tool legal_research"]
    assert tool.parent and tool.parent.span_id == root.context.span_id
    inner = spans["invoke_agent legal_research"]
    assert inner.parent and inner.parent.span_id == tool.context.span_id
    check = spans["guardrail citation_verifier"]
    assert check.parent and check.parent.span_id == root.context.span_id
    assert (root.attributes or {})["gen_ai.conversation.id"] == done(events).thread_id
    assert (tool.attributes or {})["ementa.tool.status"] == "finished"


DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL", "")


@pytest.fixture
def schema() -> Iterator[str]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    name = f"test_{uuid.uuid4().hex[:8]}"
    yield name
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name)))


@pytest.mark.integration
def test_a_thread_survives_a_restart(schema: str) -> None:
    """F3 acceptance: a turn, then a new agent and a new pool on the same database go on."""
    before = postgres_checkpointer(DATABASE_URL, schema)
    agent, fast, _ = make_agent(
        [
            tool_call("legal_research", question="[PESSOA_1] pode desistir da compra online?"),
            AIMessage("[PESSOA_1], pode desistir em 7 dias [cdc:art49]."),
        ],
        checkpointer=before,
    )
    first = list(agent.chat("Sou Maria Souza. Posso desistir da compra online?", user_id="ana"))
    thread = done(first).thread_id
    before.conn.close()

    after = postgres_checkpointer(DATABASE_URL, schema)
    try:
        restarted, fast_after, _ = make_agent(
            [AIMessage("Sim, [PESSOA_1]: os mesmos 7 dias [cdc:art49].")], checkpointer=after
        )
        second = list(
            restarted.chat("E se o produto chegou com defeito?", user_id="ana", thread_id=thread)
        )
        history = restarted.history(thread, user_id="ana")
    finally:
        after.conn.close()

    assert_append_only([*fast.calls, *fast_after.calls])  # the new process saw the same history
    assert done(second).message == "Sim, Maria Souza: os mesmos 7 dias [cdc:art49]."
    assert [m.content for m in history.messages] == [
        "Sou Maria Souza. Posso desistir da compra online?",
        "Maria Souza, pode desistir em 7 dias [cdc:art49].",
        "E se o produto chegou com defeito?",
        "Sim, Maria Souza: os mesmos 7 dias [cdc:art49].",
    ]
    assert [c.source_id for c in history.messages[3].citations] == ["cdc:art49"]
