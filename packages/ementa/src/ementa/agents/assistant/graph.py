"""`assistant`: the chat supervisor. On the `fast` model it routes each turn to `legal_research`
or `calculate_deadline` and answers from what they return. The conversation lives in the
checkpointer, one thread per conversation, so it survives a restart.

supervisor -> tools -> supervisor ... -> finalize. Only pseudonymized text reaches the model: the
user's message is masked before it enters the graph, with one marker mapping per thread kept in
the thread state, and the reply gets the real values back on its way out, word by word. The
long-term memories relevant to the message enter with it, masked with the same mapping, and
what the finished turn taught is written to the long-term memory after `done`. Past its context
budget, the model's view of a thread starts again from a summary; the state keeps every message.
"""

import json
import operator
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, NamedTuple, Protocol, TypedDict, cast

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import StreamMode
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import BaseModel, ValidationError

from ementa.agents.assistant.schemas import (
    ChatEvent,
    CitationEvent,
    DoneEvent,
    HistoryMessage,
    StepEvent,
    ThreadHistory,
    TokenEvent,
)
from ementa.agents.legal_research import ResearchAnswer
from ementa.domain.citations import verify_citations
from ementa.memory import chat_targets, memory_section, relevant_memories
from ementa.prompts import get_prompt
from ementa.tools import deadlines, research
from ementa.tools.research import CitationOut, RemovedOut
from ementa_kit.context import Section, build_user_turn, compact
from ementa_kit.llm import ModelRegistry, ModelRole
from ementa_kit.memory import MemoryWriter, Namespace
from ementa_kit.policy import DataClass
from ementa_kit.telemetry import get_logger, get_tracer

if TYPE_CHECKING:  # Presidio and spaCy take seconds to import; they load on the first turn
    from ementa_kit.guardrails.pii import PiiGuard

AGENT_ID = "assistant"
SUPERVISOR_PROMPT = ("assistant.supervisor", 1)
SUMMARY_PROMPT = ("context.summarize", 1)
LEGAL_RESEARCH = "legal_research"
CALCULATE_DEADLINE = "calculate_deadline"
TOOLS = (LEGAL_RESEARCH, CALCULATE_DEADLINE)  # in the order the model is offered them
MAX_ROUNDS = 3  # model calls per turn that may call tools; the next one has to answer
MAX_TOOL_CHARS = 6000  # a tool result is cut before the model sees it, never after

DENIED = "Ferramenta não permitida para o papel de quem pergunta."
FAILED = "A ferramenta falhou. Diga que não foi possível concluir agora."
MALFORMED = "Argumentos inválidos: não são um objeto JSON."
INTERRUPTED = "A execução parou antes do resultado desta ferramenta."
NO_REPLY = "Não consegui concluir a resposta com fontes verificadas. Reformule o pedido, por favor."

tracer = get_tracer(__name__)
log = get_logger(__name__)

ToolStatus = Literal["finished", "denied", "failed"]
_MODEL_DONE = StepEvent(name="supervisor", status="finished")


class Researcher(Protocol):
    def ask(self, question: str) -> ResearchAnswer: ...


class ThreadNotFoundError(LookupError):
    """No thread with this id belongs to the caller. It may not exist at all: callers answer
    the same for both, so a thread id reveals nothing."""


class SourceRecord(TypedDict):
    source_id: str
    title: str
    url: str


class RemovedRecord(TypedDict):
    reference: str
    reason: str


class TurnRecord(TypedDict):
    """A finished turn as the user sees it, still pseudonymized: rehydrated when read."""

    user: str
    reply: str  # after the citation check
    citations: list[str]
    removed: list[RemovedRecord]


class State(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], operator.add]  # all the model saw; only grows
    view_start: int  # index of the last summary; the view is the system prompt, then from here
    pii_mapping: dict[str, str]  # marker -> value for the whole thread; only grows
    owner_id: str
    prompt: str  # id@version of the system prompt, frozen with it when the thread starts
    allowed_tools: list[str]  # for this turn, from the caller's role
    rounds: int  # model calls in this turn
    question: str  # this turn's message, masked, without the memories that came with it
    sources: Annotated[list[SourceRecord], operator.add]  # verified by legal_research
    turns: Annotated[list[TurnRecord], operator.add]


class ToolResult(NamedTuple):
    content: str  # what the model sees
    artifact: dict[str, Any]  # the whole output: kept in the state, never sent to the model
    sources: list[SourceRecord]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    args: type[BaseModel]
    data_class: DataClass
    run: Callable[[Any], ToolResult]

    def definition(self) -> dict[str, Any]:
        """The OpenAI-style function definition the model is offered."""
        spec = convert_to_openai_tool(self.args)
        spec["function"].update(name=self.name, description=self.description)
        return spec


def model_view(state: State) -> list[BaseMessage]:
    """What the model receives, derived from the thread's messages as stored.

    Until a compaction, all of them, system prompt first. After one, a new conversation: the
    system prompt, then the messages from `view_start` on, the summary first. The state keeps
    the old ones for audit. Nothing is edited, dropped or reordered between two requests of a
    view, so each starts with the previous one and the provider's prompt cache holds. New
    context only enters as a new message at the end.
    """
    messages = state.get("messages", [])
    start = state.get("view_start", 0)
    return [*messages[:1], *messages[start:]] if start else list(messages)


class AssistantAgent:
    """`chat` runs a turn and streams its events; `history` reads a thread back.

    `researcher`, `guard`, `memories` (the long-term memory store) and `writer` are factories,
    called on first use: the research agent and the store load the embedding model and the
    guard loads spaCy, none of them needed to read a history. Without `memories` a turn reads no
    memory; without `writer` it writes none. Once the model's view of a thread passes
    `context_budget` tokens, the next turn starts it again from a summary; None never does.
    """

    def __init__(
        self,
        models: ModelRegistry,
        researcher: Callable[[], Researcher],
        checkpointer: BaseCheckpointSaver[Any],
        guard: Callable[[], "PiiGuard"],
        *,
        memories: Callable[[], BaseStore] | None = None,
        writer: Callable[[], MemoryWriter] | None = None,
        context_budget: int | None = None,
    ) -> None:
        self.models = models
        self._researcher = researcher
        self._guard = guard
        self._memories = memories
        self._writer = writer
        self.context_budget = context_budget
        self.prompt = get_prompt(*SUPERVISOR_PROMPT)
        self.prompt_label = f"{self.prompt.id}@{self.prompt.version}"
        self.summary_prompt = get_prompt(*SUMMARY_PROMPT)
        self.tools = {
            LEGAL_RESEARCH: AgentTool(
                LEGAL_RESEARCH,
                research.ANSWER_DESCRIPTION,
                research.AnswerLegalQuestionInput,
                research.DATA_CLASS,
                self._legal_research,
            ),
            CALCULATE_DEADLINE: AgentTool(
                CALCULATE_DEADLINE,
                deadlines.DESCRIPTION,
                deadlines.CalculateDeadlineInput,
                deadlines.DATA_CLASS,
                self._calculate_deadline,
            ),
        }
        self._definitions = {name: tool.definition() for name, tool in self.tools.items()}
        self.graph = self._build(checkpointer)

    # Tools -------------------------------------------------------------------------------

    def _legal_research(self, args: research.AnswerLegalQuestionInput) -> ToolResult:
        output = research.AnswerLegalQuestionOutput.from_answer(
            self._researcher().ask(args.question)
        )
        seen = {
            "answer": output.answer,
            "citations": [c.source_id for c in output.citations],
            "grounded": output.grounded,
        }
        sources = [
            SourceRecord(source_id=c.source_id, title=c.title, url=c.url) for c in output.citations
        ]
        return ToolResult(
            json.dumps(seen, ensure_ascii=False), output.model_dump(mode="json"), sources
        )

    def _calculate_deadline(self, args: deadlines.CalculateDeadlineInput) -> ToolResult:
        output = deadlines.calculate_deadline(args)
        # Every counted day is in the artifact; the model gets the due date, skips and premises.
        seen = output.model_dump_json(exclude={"counted_days"})
        return ToolResult(seen, output.model_dump(mode="json"), [])

    def _execute(
        self, call: ToolCall, allowed: set[str]
    ) -> tuple[ToolMessage, list[SourceRecord], ToolStatus]:
        name, call_id = call["name"], call.get("id") or ""
        tool = self.tools.get(name)
        status: ToolStatus
        with tracer.start_as_current_span(
            f"execute_tool {name}",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "gen_ai.tool.call.id": call_id,
                "ementa.data_class": tool.data_class.value if tool else "unknown",
            },
        ) as span:
            result: ToolResult | None = None
            if tool is None or name not in allowed:  # the model was not offered it
                status, content = "denied", DENIED
            else:
                try:
                    result = tool.run(tool.args.model_validate(call["args"]))
                    status, content = "finished", result.content
                except ValidationError as exc:  # the model may fix the call in the next round
                    status, content = "failed", f"Argumentos inválidos: {_field_errors(exc)}"
                except Exception as exc:  # the turn goes on; the model hears the tool failed
                    log.warning("tool_failed", tool=name, error=type(exc).__name__)
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR))
                    status, content = "failed", FAILED
            content = _truncate(content)
            span.set_attribute("ementa.tool.status", status)
            span.set_attribute("ementa.tool.output_chars", len(content))
        message = ToolMessage(
            content,
            tool_call_id=call_id,
            name=name,
            id=_new_id(),
            status="success" if status == "finished" else "error",
            artifact=result.artifact if result else None,
        )
        return message, result.sources if result else [], status

    def _model(self, tools: Sequence[str], choice: str) -> Runnable[LanguageModelInput, AIMessage]:
        """The `fast` model offering `tools`. The list keeps the same order and stays even when
        `choice` is "none", so the requests of a thread keep their prefix."""
        chat = self.models.chat(ModelRole.FAST)
        offered = [self._definitions[n] for n in TOOLS if n in tools]
        return chat.bind_tools(offered, tool_choice=choice) if offered else chat

    # Nodes -------------------------------------------------------------------------------

    def supervisor(self, state: State) -> State:
        write = get_stream_writer()
        write(StepEvent(name="supervisor", status="started"))
        rounds = state.get("rounds", 0)
        choice = "auto" if rounds < MAX_ROUNDS else "none"  # the last round must answer
        model = self._model(state.get("allowed_tools", []), choice)
        reply = model.invoke(model_view(state))
        write(StepEvent(name="supervisor", status="finished"))
        return {"messages": [reply], "rounds": rounds + 1}

    def run_tools(self, state: State) -> State:
        """Every call gets a result, or the provider refuses the history from then on."""
        write = get_stream_writer()
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        allowed = set(state.get("allowed_tools", []))
        known = {s["source_id"] for s in state.get("sources", [])}
        messages: list[BaseMessage] = []
        sources: list[SourceRecord] = []
        for call in last.tool_calls:
            write(StepEvent(name=call["name"], status="started"))
            message, found, status = self._execute(call, allowed)
            write(StepEvent(name=call["name"], status=status))
            messages.append(message)
            for source in found:
                write(CitationEvent(**source))
                if source["source_id"] not in known:
                    known.add(source["source_id"])
                    sources.append(source)
        for broken in last.invalid_tool_calls:  # arguments that are not JSON
            name = broken.get("name") or "unknown"
            write(StepEvent(name=name, status="failed"))
            messages.append(_error_result(MALFORMED, broken.get("id"), name))
        return {"messages": messages, "sources": sources}

    def finalize(self, state: State) -> State:
        """The reply as the user will see it: a sentence citing an id that no tool verified in
        this thread is removed. Only bracketed ids count, since a deadline's premises name
        articles in prose. The model's own message stays as it was in the history."""
        reply = state["messages"][-1]
        allowed = [s["source_id"] for s in state.get("sources", [])]
        with tracer.start_as_current_span(
            "guardrail citation_verifier",
            attributes={
                "gen_ai.operation.name": "guardrail",
                "ementa.guardrail.name": "citations",
                "ementa.guardrail.stage": "output",
            },
        ) as span:
            checked = verify_citations(reply.text, allowed, check_prose=False)
            span.set_attribute(
                "ementa.guardrail.decision", "modified" if checked.removed else "pass"
            )
            span.set_attribute("ementa.guardrail.removed", len(checked.removed))
        turn = TurnRecord(
            user=state.get("question", ""),
            reply=checked.text or NO_REPLY,
            citations=checked.valid,
            removed=[
                RemovedRecord(reference=r.reference, reason=r.reason) for r in checked.removed
            ],
        )
        return {"turns": [turn]}

    # Edges -------------------------------------------------------------------------------

    @staticmethod
    def _after_supervisor(state: State) -> Literal["tools", "finalize"]:
        last = state["messages"][-1]
        wants_tools = isinstance(last, AIMessage) and bool(
            last.tool_calls or last.invalid_tool_calls
        )
        # Past the last round the calls stay open; the next turn closes them before it starts.
        return "tools" if wants_tools and state.get("rounds", 0) <= MAX_ROUNDS else "finalize"

    def _build(
        self, checkpointer: BaseCheckpointSaver[Any]
    ) -> CompiledStateGraph[Any, Any, Any, Any]:
        graph = StateGraph(State)
        graph.add_node("supervisor", self.supervisor)
        graph.add_node("tools", self.run_tools)
        graph.add_node("finalize", self.finalize)
        graph.add_edge(START, "supervisor")
        graph.add_conditional_edges("supervisor", self._after_supervisor)
        graph.add_edge("tools", "supervisor")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=checkpointer, name=AGENT_ID)

    # Entry points ------------------------------------------------------------------------

    def chat(
        self,
        message: str,
        *,
        user_id: str,
        thread_id: str | None = None,
        case_id: str | None = None,
        allowed_tools: Sequence[str] = TOOLS,
    ) -> Iterator[ChatEvent]:
        """Start a turn of `user_id`'s thread, or of a new one without `thread_id`.

        What can fail before the first event fails here, while an HTTP error can still be sent:
        a thread that is not the caller's raises ThreadNotFoundError, a model without a key
        raises ModelUnavailableError. The turn runs as the iterator is consumed; `done` is the
        last event, and the memory writer runs after it, before the iterator ends. Only the
        tools in `allowed_tools` are offered to the model and run. With `case_id` the turn also
        reads and writes the card of that case: pass only a case the user may see, since its
        memories follow its permission. An id the memory store cannot take, such as one with a
        dot, raises ValueError here too.
        """
        values = self._owned(thread_id, user_id) if thread_id else State()
        self.models.chat(ModelRole.FAST)
        chat_targets(user_id, case_id)  # the namespaces the turn writes to after `done`
        tools = [name for name in TOOLS if name in allowed_tools]
        return self._run(message, thread_id or _new_id(), user_id, case_id, tools, values)

    def history(self, thread_id: str, *, user_id: str) -> ThreadHistory:
        """The finished turns of `user_id`'s thread, with the real values back."""
        from ementa_kit.guardrails.pii import rehydrate

        values = self._owned(thread_id, user_id)
        mapping = values.get("pii_mapping", {})
        sources = {s["source_id"]: s for s in values.get("sources", [])}
        messages: list[HistoryMessage] = []
        for turn in values.get("turns", []):
            cited = [CitationOut(**sources[i]) for i in turn["citations"] if i in sources]
            messages += [
                HistoryMessage(role="user", content=rehydrate(turn["user"], mapping)),
                HistoryMessage(
                    role="assistant", content=rehydrate(turn["reply"], mapping), citations=cited
                ),
            ]
        return ThreadHistory(thread_id=thread_id, messages=messages)

    def _owned(self, thread_id: str, user_id: str) -> State:
        values = cast(State, self.graph.get_state(_config(thread_id)).values)
        if not values or values.get("owner_id") != user_id:
            raise ThreadNotFoundError(thread_id)
        return values

    def _run(
        self,
        message: str,
        thread_id: str,
        user_id: str,
        case_id: str | None,
        allowed_tools: list[str],
        values: State,
    ) -> Iterator[ChatEvent]:
        spec = self.models.spec(ModelRole.FAST)
        prompt_id, _, version = values.get("prompt", self.prompt_label).partition("@")
        span = tracer.start_span(
            f"invoke_agent {AGENT_ID}",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": AGENT_ID,
                "gen_ai.conversation.id": thread_id,
                "gen_ai.provider.name": spec.provider,
                "gen_ai.request.model": spec.model,
                "ementa.prompt.id": prompt_id,
                "ementa.prompt.version": int(version),
                "ementa.assistant.tools": allowed_tools,
            },
        )
        try:
            turn = self._turn(message, thread_id, user_id, case_id, allowed_tools, values, span)
            yield from _with_span(span, turn)
        finally:
            span.end()

    def _turn(
        self,
        message: str,
        thread_id: str,
        user_id: str,
        case_id: str | None,
        allowed_tools: list[str],
        values: State,
        span: Span,
    ) -> Iterator[ChatEvent]:
        from ementa_kit.guardrails.pii import MarkerStream, rehydrate

        guard = self._guard()
        masked = guard.pseudonymize(message, values.get("pii_mapping"), stage="input")
        mapping = masked.mapping
        sections: list[Section] = []
        recalled = self._recall(message, user_id, case_id)
        if recalled is not None:  # the memories hold values as said: masked like the message
            hidden = guard.pseudonymize(recalled.content, mapping, stage="input")
            mapping = hidden.mapping
            sections.append(Section(recalled.name, hidden.text))
        inputs = self._inputs(values, masked.text, sections, mapping, user_id, allowed_tools)
        tokens = MarkerStream(mapping)
        cited: set[str] = set()
        turn: TurnRecord | None = None
        modes: list[StreamMode] = ["messages", "custom", "updates"]  # tokens, node events, outputs
        stream = self.graph.stream(inputs, _config(thread_id), stream_mode=modes)
        for mode, payload in cast(Iterator[tuple[str, Any]], stream):
            if mode == "messages":
                chunk, meta = payload
                ours = meta.get("langgraph_node") == "supervisor" and isinstance(chunk, AIMessage)
                if ours and (text := tokens.feed(chunk.text)):
                    yield TokenEvent(text=text)
            elif mode == "custom":
                # A model call ended: its last word goes out before the step says so.
                if payload == _MODEL_DONE and (text := tokens.flush()):
                    yield TokenEvent(text=text)
                if isinstance(payload, CitationEvent):
                    if payload.source_id in cited:
                        continue
                    cited.add(payload.source_id)
                yield payload
            elif mode == "updates" and "finalize" in payload:
                turn = payload["finalize"]["turns"][-1]
        if turn is None:
            raise RuntimeError("the turn ended without a reply")
        span.set_attribute("ementa.answer.citations", turn["citations"])
        span.set_attribute("ementa.answer.removed", len(turn["removed"]))
        reply = rehydrate(turn["reply"], mapping)
        yield DoneEvent(
            thread_id=thread_id,
            message=reply,
            removed=[RemovedOut(**r) for r in turn["removed"]],
        )
        self._learn(
            [HumanMessage(message), AIMessage(reply)], chat_targets(user_id, case_id), mapping
        )

    def _inputs(
        self,
        values: State,
        text: str,
        sections: Sequence[Section],
        mapping: dict[str, str],
        user_id: str,
        allowed_tools: list[str],
    ) -> State:
        """The turn's input. A new thread opens with the system prompt, frozen from then on:
        later turns only append, even after the prompt file gets a new version. The turn's own
        context (`sections`, the memories) enters in the new message, ahead of the question.
        Past the context budget, a summary goes in first and the view starts again from it."""
        history = values.get("messages", [])
        opening: list[BaseMessage] = (
            [] if history else [SystemMessage(self.prompt.text, id=_new_id())]
        )
        closing = _close_dangling(history)
        summary = self._summary([*model_view(values), *closing], values.get("allowed_tools", []))
        start = len(history) + len(closing) if summary else values.get("view_start", 0)
        content = build_user_turn(sections, text).text if sections else text
        return {
            "messages": [*opening, *closing, *summary, HumanMessage(content, id=_new_id())],
            "view_start": start,
            "pii_mapping": mapping,
            "owner_id": user_id,
            "prompt": values.get("prompt", self.prompt_label),
            "allowed_tools": allowed_tools,
            "rounds": 0,
            "question": text,
        }

    def _summary(self, view: list[BaseMessage], tools: Sequence[str]) -> list[BaseMessage]:
        """Past the context budget, the summary of `view` as a message to append, where the
        view starts again. `compact` asks for it by appending its prompt to `view`, with the
        tools of the last turn, so the request extends the last one and its cached prefix; the
        `ementa.context.compacted` event goes to the turn's span. Nothing while the view fits,
        or when the summary fails: the view stays whole, and the next turn tries again."""
        if self.context_budget is None or not view:
            return []
        try:
            compaction = compact(
                view, self._model(tools, "none"), self.summary_prompt, budget=self.context_budget
            )
        except Exception as exc:
            log.warning("compaction_failed", error=type(exc).__name__)
            return []
        if compaction is None:
            return []
        return [  # the system prompt opening the new conversation is already the first message
            m.model_copy(update={"id": _new_id()})
            for m in compaction.messages
            if not isinstance(m, SystemMessage)
        ]

    def _recall(self, message: str, user_id: str, case_id: str | None) -> Section | None:
        """The `memorias` section of the turn, with the values in clear: the user's preferences,
        the card of the case and the firm's theses closest to the message as typed, since the
        memories keep the values as said. The search runs here, on the local embedder. A store
        that fails costs the turn its memories, not its reply."""
        if self._memories is None:
            return None
        try:
            found = relevant_memories(self._memories(), message, user_id=user_id, case_id=case_id)
        except Exception as exc:
            log.warning("memory_read_failed", error=type(exc).__name__)
            return None
        return memory_section(found)

    def _learn(
        self, turn: Sequence[BaseMessage], targets: Sequence[Namespace], mapping: dict[str, str]
    ) -> None:
        """What the finished turn taught, into the long-term memory. The writer masks the turn
        with the thread's mapping, so its model sees the markers of the conversation. The user
        already has the reply: a failure is logged and the turn ends as usual."""
        if self._writer is None:
            return
        try:
            self._writer().write(turn, targets, mapping)
        except Exception as exc:
            log.warning("memory_write_failed", error=type(exc).__name__)


def _close_dangling(history: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Results for the tool calls the last turn left open (the process stopped between the
    model's call and the tool's result, or the rounds ran out). Providers refuse a history with
    an unanswered tool call, and the history is never edited, so the results are appended."""
    last = history[-1] if history else None
    if not isinstance(last, AIMessage):
        return []
    calls = [*last.tool_calls, *last.invalid_tool_calls]
    return [
        _error_result(INTERRUPTED, call.get("id"), call.get("name") or "unknown") for call in calls
    ]


def _error_result(text: str, call_id: str | None, name: str) -> ToolMessage:
    return ToolMessage(text, tool_call_id=call_id or "", name=name, id=_new_id(), status="error")


def _with_span[T](span: Span, steps: Iterator[T]) -> Iterator[T]:
    """Advance `steps` with `span` current, one step at a time, never across a yield.

    The API pulls each event in a worker thread with its own copy of the request's context, so
    a span attached in one step and detached in another would break the trace.
    """
    while True:
        with trace.use_span(span, end_on_exit=False):
            try:
                step = next(steps)
            except StopIteration:
                return
        yield step


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _new_id() -> str:
    return str(uuid.uuid4())


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_TOOL_CHARS else f"{text[:MAX_TOOL_CHARS]} [truncado]"


def _field_errors(exc: ValidationError) -> str:
    """Where the arguments are wrong, without echoing their values."""
    return "; ".join(
        f"{'.'.join(str(part) for part in e['loc']) or 'args'}: {e['msg']}"
        for e in exc.errors(include_url=False, include_input=False)
    )
