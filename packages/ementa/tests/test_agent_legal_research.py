from collections.abc import Iterable, Sequence
from functools import cache
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import Field

from ementa.agents.legal_research import LegalResearchAgent, graph
from ementa.agents.legal_research.graph import NO_BASIS, explicit_ids, render_source
from ementa.ingest.corpus import load_statute
from ementa.ingest.index import statute_chunks
from ementa.knowledge import article_of
from ementa_kit.config import Settings
from ementa_kit.context import PrefixChangedError, approx_tokens, assert_append_only
from ementa_kit.llm import ModelRegistry, ModelRole, ModelSpec
from ementa_kit.rag import Hit


@cache
def cdc_hits() -> dict[str, Hit]:
    return {
        c.id: Hit(c.id, c.source_id, c.title, c.path, c.text, c.provision_ids, c.url)
        for c in statute_chunks(load_statute("cdc"))
    }


class FakeRetriever:
    def __init__(self, results: Sequence[str]) -> None:
        self.results = list(results)
        self.queries: list[str] = []

    def search(self, queries: Sequence[str], limit: int = 5) -> list[Hit]:
        self.queries.extend(queries)
        return [cdc_hits()[i] for i in self.results[:limit]]

    def get(self, source_ids: Iterable[str]) -> list[Hit]:
        return [cdc_hits()[article_of(i)] for i in source_ids if article_of(i) in cdc_hits()]


class ScriptedChat(GenericFakeChatModel):
    """Replies in order and records a copy of what it was sent, so a later in-place edit of a
    message shows up as a changed prefix."""

    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.calls.append([m.model_copy(deep=True) for m in messages])
        return super()._generate(messages, *args, **kwargs)


def make_agent(
    retriever: FakeRetriever, answers: list[str], rewrites: str = "consulta"
) -> tuple[LegalResearchAgent, dict[ModelRole, ScriptedChat]]:
    chats = {
        ModelRole.FAST: ScriptedChat(messages=iter([AIMessage(rewrites)])),
        ModelRole.DEFAULT: ScriptedChat(messages=iter([AIMessage(a) for a in answers])),
    }

    def factory(spec: ModelSpec) -> ScriptedChat:
        return chats[spec.role]

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    return LegalResearchAgent(retriever, ModelRegistry(settings, factory=factory)), chats


GOOD = "O consumidor pode desistir em 7 dias da compra fora do estabelecimento [cdc:art49]."
QUESTION = "Posso desistir da compra online?"


def test_answer_with_verified_citation_in_one_attempt() -> None:
    retriever = FakeRetriever(["cdc:art49", "cdc:art35"])
    research, chats = make_agent(
        retriever, [GOOD], rewrites="1. direito de arrependimento\n- prazo"
    )

    answer = research.ask(QUESTION)

    assert answer.answer == GOOD
    assert [c.source_id for c in answer.citations] == ["cdc:art49"]
    assert answer.attempts == 1
    assert answer.removed == []
    assert answer.prompt == "legal_research.answer@1"
    assert retriever.queries == [QUESTION, "direito de arrependimento", "prazo"]
    context = chats[ModelRole.DEFAULT].calls[0][1].text
    assert "[cdc:art49:pu] Parágrafo único." in context
    assert context.endswith(f"<pergunta>\n{QUESTION}\n</pergunta>")


BAD = "Cabe dano moral pela Súmula 385 [stj:sumula:385]. " + GOOD


def test_bad_citation_triggers_one_retry_appending_to_the_conversation() -> None:
    research, chats = make_agent(FakeRetriever(["cdc:art49"]), [BAD, GOOD])

    answer = research.ask(QUESTION)

    assert answer.attempts == 2
    assert answer.answer == GOOD
    assert_append_only(chats[ModelRole.DEFAULT].calls)  # nothing the model saw was edited
    first, second = chats[ModelRole.DEFAULT].calls
    assert len(second) == len(first) + 2  # its own reply, then the verifier's feedback
    assert "stj:sumula:385: fonte fora do contexto recuperado" in second[-1].text


def test_prefix_check_catches_a_retry_that_rewrites_the_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that folded the feedback into the first user turn, instead of appending it,
    would break the provider cache. The prefix check must catch it."""

    def generate_answer(self: LegalResearchAgent, state: graph.State) -> graph.State:
        system, turn, *rest = state["messages"]
        if rest:
            *rest, feedback = rest
            turn = HumanMessage(f"{turn.text}\n\n{feedback.text}")
        reply = self.models.chat(ModelRole.DEFAULT).invoke([system, turn, *rest])
        return {"messages": [reply], "attempts": state.get("attempts", 0) + 1}

    monkeypatch.setattr(LegalResearchAgent, "generate_answer", generate_answer)
    research, chats = make_agent(FakeRetriever(["cdc:art49"]), [BAD, GOOD])

    assert research.ask(QUESTION).attempts == 2
    with pytest.raises(PrefixChangedError, match="request 2 changed message 1 of request 1"):
        assert_append_only(chats[ModelRole.DEFAULT].calls)


def test_context_size_of_each_section_goes_to_the_agent_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(graph, "tracer", provider.get_tracer("test"))
    research, chats = make_agent(FakeRetriever(["cdc:art49", "cdc:art35"]), [GOOD])

    research.ask(QUESTION)

    [span] = [s for s in exporter.get_finished_spans() if s.name == "invoke_agent legal_research"]
    [event] = [e for e in span.events if e.name == "ementa.context.section_tokens"]
    sizes = {k: v for k, v in (event.attributes or {}).items() if isinstance(v, int)}
    system, turn = chats[ModelRole.DEFAULT].calls[0]
    assert len(sizes) == len(event.attributes or {}) == 6  # chars and tokens of 3 parts
    assert sizes["system.chars"] == len(system.text)
    assert sizes["system.tokens"] == approx_tokens(system.text)
    assert sizes["fontes.chars"] + len("\n\n") + sizes["pergunta.chars"] == len(turn.text)
    assert sizes["fontes.tokens"] > sizes["pergunta.tokens"] > 0


def test_answer_without_valid_citation_falls_back_after_the_retry() -> None:
    research, chats = make_agent(
        FakeRetriever(["cdc:art49"]), ["Pelo art. 6 do CPC, sim.", "Acho que sim."]
    )

    answer = research.ask(QUESTION)

    assert answer.answer == NO_BASIS
    assert not answer.grounded
    assert answer.attempts == 2
    assert len(chats[ModelRole.DEFAULT].calls) == 2


def test_explicit_article_is_looked_up_without_rewriting() -> None:
    retriever = FakeRetriever([])
    research, chats = make_agent(retriever, [GOOD])

    answer = research.ask("O que diz o art. 49 do CDC?")

    assert answer.sources == ["cdc:art49"]
    assert retriever.queries == []
    assert chats[ModelRole.FAST].calls == []


def test_failed_rewrite_searches_with_the_question_only() -> None:
    retriever = FakeRetriever(["cdc:art49"])
    research, chats = make_agent(retriever, [GOOD])
    chats[ModelRole.FAST].messages = iter([])  # the fake raises when it runs out

    research.ask(QUESTION)

    assert retriever.queries == [QUESTION]


@pytest.mark.parametrize(
    ("question", "ids"),
    [
        ("O que diz o art. 49 do CDC?", ["cdc:art49"]),
        ("artigo 54-a e art. 42", ["cdc:art54-A", "cdc:art42"]),
        ("O art. 219 do CPC conta dias úteis?", []),
        ("Posso desistir?", []),
    ],
)
def test_explicit_ids(question: str, ids: list[str]) -> None:
    assert explicit_ids(question) == ids


def test_render_source_prefixes_each_provision_with_its_id() -> None:
    lines = render_source(cdc_hits()["cdc:art42"]).splitlines()

    assert lines[0].startswith("Código de Defesa do Consumidor, art. 42 (")
    assert lines[1].startswith("[cdc:art42] Art. 42.")
    assert lines[2].startswith("[cdc:art42:pu] Parágrafo único.")
