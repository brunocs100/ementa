"""Compaction: past the budget, a new conversation opens with a summary; the old is not edited."""

from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import Field

from ementa_kit.context import (
    COMPACTED_EVENT,
    approx_tokens,
    assert_append_only,
    compact,
    conversation_tokens,
)
from ementa_kit.prompts import Prompt

PROMPT = Prompt("context.summarize", 1, "v1 teste", "Resuma a conversa acima.")
SYSTEM = SystemMessage("Você é o assistente do escritório.")
SUMMARY = "- [PESSOA_1] quer acordo; prazo de 15 dias úteis [cpc:art219]."


class ScriptedChat(GenericFakeChatModel):
    """Replies in order and records a copy of each request."""

    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.calls.append([m.model_copy(deep=True) for m in messages])
        return super()._generate(messages, *args, **kwargs)


def conversation(turns: int, system: bool = True) -> list[BaseMessage]:
    """System prompt, then `turns` questions and answers of about 22 tokens each."""
    filler = "x" * 80
    messages: list[BaseMessage] = [SYSTEM] if system else []
    for n in range(turns):
        messages += [HumanMessage(f"Pergunta {n} {filler}"), AIMessage(f"Resposta {n} {filler}")]
    return messages


def test_a_conversation_within_its_budget_is_left_alone() -> None:
    chat = ScriptedChat(messages=iter([]))
    messages = conversation(3)

    assert compact(messages, chat, PROMPT, budget=conversation_tokens(messages)) is None
    assert chat.calls == []


def test_past_the_budget_a_new_conversation_opens_with_the_summary() -> None:
    chat = ScriptedChat(messages=iter([AIMessage(f"  {SUMMARY}\n")]))
    messages = conversation(6)
    before = [m.model_copy(deep=True) for m in messages]

    result = compact(messages, chat, PROMPT, budget=100)

    assert result is not None
    assert messages == before  # nothing the model saw was edited
    [request] = chat.calls
    assert_append_only([messages, request])  # the summary request only appends the prompt
    assert request[len(messages) :] == [HumanMessage(PROMPT.text)]
    assert result.messages == [SYSTEM, HumanMessage(f"<resumo>\n{SUMMARY}\n</resumo>")]
    assert result.summary == SUMMARY
    assert result.tokens_before == conversation_tokens(messages) > 100
    assert result.tokens_after == conversation_tokens(result.messages) < result.tokens_before
    next_turn = [*result.messages, HumanMessage("E o prazo?")]
    assert_append_only([result.messages, next_turn, [*next_turn, AIMessage("15 dias úteis.")]])


def test_a_long_summary_is_cut_at_its_budget() -> None:
    chat = ScriptedChat(messages=iter([AIMessage("palavra " * 500)]))

    result = compact(conversation(6), chat, PROMPT, budget=100, summary_tokens=50)

    assert result is not None
    assert approx_tokens(result.summary) <= 50
    assert result.summary.endswith("palavra…")


def test_without_a_system_prompt_the_new_conversation_is_the_summary_alone() -> None:
    chat = ScriptedChat(messages=iter([AIMessage(SUMMARY)]))

    result = compact(conversation(6, system=False), chat, PROMPT, budget=100)

    assert result is not None
    assert [m.type for m in result.messages] == ["human"]


def test_the_compaction_is_an_event_on_the_current_span() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    chat = ScriptedChat(messages=iter([AIMessage(SUMMARY)]))
    messages = conversation(6)

    with provider.get_tracer("test").start_as_current_span("invoke_agent assistant"):
        result = compact(messages, chat, PROMPT, budget=100)

    assert result is not None
    [span] = exporter.get_finished_spans()
    [event] = span.events
    assert event.name == COMPACTED_EVENT == "ementa.context.compacted"
    assert dict(event.attributes or {}) == {
        "messages.before": 13,
        "tokens.before": result.tokens_before,
        "tokens.after": result.tokens_after,
        "ementa.prompt.id": "context.summarize",
        "ementa.prompt.version": 1,
    }
