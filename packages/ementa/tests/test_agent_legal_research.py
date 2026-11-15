from collections.abc import Iterable, Sequence
from functools import cache
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from pydantic import Field

from ementa.agents.legal_research import LegalResearchAgent
from ementa.agents.legal_research.graph import NO_BASIS, explicit_ids, render_source
from ementa.ingest.corpus import SOURCE_IDS, load_statute
from ementa.ingest.index import statute_chunks
from ementa.knowledge import article_of
from ementa_kit.config import Settings
from ementa_kit.llm import ModelRegistry, ModelRole, ModelSpec
from ementa_kit.rag import Hit


@cache
def corpus_hits() -> dict[str, Hit]:
    return {
        c.id: Hit(c.id, c.source_id, c.title, c.path, c.text, c.provision_ids, c.url)
        for source in SOURCE_IDS
        for c in statute_chunks(load_statute(source))
    }


class FakeRetriever:
    def __init__(self, results: Sequence[str]) -> None:
        self.results = list(results)
        self.queries: list[str] = []

    def search(self, queries: Sequence[str], limit: int = 5) -> list[Hit]:
        self.queries.extend(queries)
        return [corpus_hits()[i] for i in self.results[:limit]]

    def get(self, source_ids: Iterable[str]) -> list[Hit]:
        hits = corpus_hits()
        return [hits[article_of(i)] for i in source_ids if article_of(i) in hits]


class ScriptedChat(GenericFakeChatModel):
    """Replies in order and records what it was sent."""

    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
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


def test_bad_citation_triggers_one_retry_appending_to_the_conversation() -> None:
    bad = "Cabe dano moral pela Súmula 385 [stj:sumula:385]. " + GOOD
    research, chats = make_agent(FakeRetriever(["cdc:art49"]), [bad, GOOD])

    answer = research.ask(QUESTION)

    assert answer.attempts == 2
    assert answer.answer == GOOD
    first, second = chats[ModelRole.DEFAULT].calls
    assert second[: len(first)] == first  # nothing the model saw was edited
    assert len(second) == len(first) + 2  # its own reply, then the verifier's feedback
    assert "stj:sumula:385: fonte fora do contexto recuperado" in second[-1].text


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


@pytest.mark.parametrize(
    ("question", "sources"),
    [
        ("O que diz o art. 219 do CPC?", ["cpc:art219"]),
        ("O art. 7º da LGPD e a Súmula 385 do STJ se aplicam?", ["lgpd:art7", "stj:sumula:385"]),
        ("A Súmula Vinculante 25 ainda vale?", ["stf:sv:25"]),
    ],
)
def test_explicit_reference_to_any_source_is_looked_up(question: str, sources: list[str]) -> None:
    retriever = FakeRetriever([])
    research, chats = make_agent(retriever, [f"Resposta pela fonte pedida [{sources[0]}]."])

    answer = research.ask(question)

    assert answer.sources == sources
    assert [c.source_id for c in answer.citations] == sources[:1]
    assert retriever.queries == []
    assert chats[ModelRole.FAST].calls == []


def test_explicit_reference_outside_the_corpus_goes_to_search() -> None:
    retriever = FakeRetriever(["cdc:art49"])
    research, _ = make_agent(retriever, [GOOD])

    answer = research.ask("O art. 219 do CDC conta dias úteis?")  # the CDC ends at art. 119

    assert answer.sources == ["cdc:art49"]
    assert retriever.queries[0] == "O art. 219 do CDC conta dias úteis?"


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
        ("O art. 219 do CPC conta dias úteis?", ["cpc:art219"]),
        (
            "Os arts. 219, 220 e 224 do Código de Processo Civil",
            ["cpc:art219", "cpc:art220", "cpc:art224"],
        ),
        ("No CPC, o art. 1.003, § 5º, fala de prazo?", ["cpc:art1003"]),
        ("Pela Lei nº 13.709, o que garante o art. 18?", ["lgpd:art18"]),
        ("O art. 18 do CDC e o art. 18 da LGPD", ["cdc:art18", "lgpd:art18"]),
        ("O art. 49 dá 7 dias. A LGPD fala disso?", ["cdc:art49"]),
        ("O que diz a Súmula 385 do STJ?", ["stj:sumula:385"]),
        ("As súmulas 297 e 479/STJ", ["stj:sumula:297", "stj:sumula:479"]),
        ("A Súmula Vinculante nº 13 e a SV 25", ["stf:sv:13", "stf:sv:25"]),
        ("A Súmula 473 do STF se aplica?", []),
        ("A Súmula 385 se aplica?", []),
        ("O art. 186 do Código Civil exige culpa?", []),
        ("O art. 1º da Lei Complementar 110/2001", []),
        ("O art. 46 da lei de proteção de dados", ["lgpd:art46"]),
        ("O art. 5º, XXXII, da Constituição e o art. 6º do CDC", ["cdc:art6"]),
        ("Posso desistir?", []),
    ],
)
def test_explicit_ids(question: str, ids: list[str]) -> None:
    assert explicit_ids(question) == ids


def test_render_source_prefixes_each_provision_with_its_id() -> None:
    lines = render_source(corpus_hits()["cdc:art42"]).splitlines()

    assert lines[0].startswith("Código de Defesa do Consumidor, art. 42 (")
    assert lines[1].startswith("[cdc:art42] Art. 42.")
    assert lines[2].startswith("[cdc:art42:pu] Parágrafo único.")


def test_render_source_of_a_sumula() -> None:
    lines = render_source(corpus_hits()["stj:sumula:385"]).splitlines()

    assert lines == [
        "Súmula 385 do STJ (DIREITO DO CONSUMIDOR > INSCRIÇÃO EM CADASTRO DE INADIMPLENTES)",
        "[stj:sumula:385] Súmula 385 do STJ: Da anotação irregular em cadastro de proteção ao "
        "crédito, não cabe indenização por dano moral, quando preexistente legítima inscrição, "
        "ressalvado o direito ao cancelamento.",
    ]
