"""`legal_research`: agentic RAG over the legal corpus, with a citation check and one retry.

parse_question -> direct_lookup (explicit article) or rewrite_queries -> retrieve
-> assemble_context -> generate_answer -> verify_citations -> finalize, or back to
generate_answer once with the verifier's findings appended as a new message.
"""

import operator
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ementa.domain.citations import RemovedCitation, VerificationResult, verify_citations
from ementa.prompts import get_prompt
from ementa_kit.context import Section, build_user_turn
from ementa_kit.llm import ModelRegistry, ModelRole
from ementa_kit.rag import Hit
from ementa_kit.telemetry import get_logger, get_tracer

AGENT_ID = "legal_research"
ANSWER_PROMPT = ("legal_research.answer", 1)
REWRITE_PROMPT = ("legal_research.rewrite", 1)
MAX_ATTEMPTS = 2  # first answer plus one retry
CONTEXT_LIMIT = 5  # sources in the context without the rerank (F1); with it, rerank_top_k
MAX_REWRITES = 3

NOTICE = "Rascunho para conferência do advogado responsável. Não é aconselhamento jurídico."
NO_BASIS = (
    "Não encontrei base nas fontes recuperadas para responder com segurança. "
    "A pergunta precisa de pesquisa do advogado."
)

_EXPLICIT = re.compile(r"\bart(?:igo)?s?\.?\s*(\d+)(?:\s*-\s*([A-Z]))?", re.IGNORECASE)
_OTHER_LAW = re.compile(
    r"\b(cpc|c[óo]digo civil|c[óo]digo de processo|constitui[çc][ãa]o|cf|clt|lei n)", re.IGNORECASE
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")

tracer = get_tracer(__name__)
log = get_logger(__name__)


class Retriever(Protocol):
    def search(self, queries: Sequence[str], limit: int = CONTEXT_LIMIT) -> list[Hit]: ...

    def get(self, source_ids: Iterable[str]) -> list[Hit]: ...


class State(TypedDict, total=False):
    question: str
    explicit_ids: list[str]
    queries: list[str]
    hits: list[Hit]
    messages: Annotated[list[BaseMessage], operator.add]  # append-only by construction
    attempts: int
    verification: VerificationResult


@dataclass(frozen=True)
class Citation:
    source_id: str
    title: str
    url: str


@dataclass(frozen=True)
class ResearchAnswer:
    question: str
    answer: str
    citations: list[Citation]
    removed: list[RemovedCitation]
    sources: list[str]
    queries: list[str]
    attempts: int
    prompt: str
    model: str
    notice: str = NOTICE

    @property
    def grounded(self) -> bool:
        return bool(self.citations)


def explicit_ids(question: str) -> list[str]:
    """Articles named in the question ("art. 49 do CDC"). Only the CDC is in the corpus in F1."""
    if _OTHER_LAW.search(question):
        return []
    ids = []
    for m in _EXPLICIT.finditer(question):
        number = m.group(1) + (f"-{m.group(2).upper()}" if m.group(2) else "")
        ids.append(f"cdc:art{number}")
    return list(dict.fromkeys(ids))


def render_source(hit: Hit) -> str:
    """One line per provision, each with its own id, so the model can cite the exact one."""
    lines = hit.text.splitlines()
    ids = hit.provision_ids if len(hit.provision_ids) == len(lines) else [hit.id] * len(lines)
    header = f"{hit.title} ({' > '.join(hit.path)})" if hit.path else hit.title
    return "\n".join([header, *(f"[{i}] {line}" for i, line in zip(ids, lines, strict=True))])


def allowed_ids(hits: Iterable[Hit]) -> set[str]:
    return {i for hit in hits for i in (hit.id, *hit.provision_ids)}


def feedback(removed: Sequence[RemovedCitation]) -> str:
    if not removed:
        problems = "- a resposta não trouxe nenhum id de fonte entre colchetes"
    else:
        problems = "\n".join(f"- {r.reference}: {r.reason}" for r in removed)
    return (
        "O verificador de citações recusou a resposta anterior:\n"
        f"{problems}\n"
        "Reescreva a resposta usando só as fontes acima, com o id entre colchetes em toda frase "
        "que afirma uma regra. Se as fontes não bastam, diga isso sem citar nada."
    )


class LegalResearchAgent:
    def __init__(
        self, retriever: Retriever, models: ModelRegistry, context_limit: int = CONTEXT_LIMIT
    ) -> None:
        self.retriever = retriever
        self.models = models
        self.context_limit = context_limit
        self.answer_prompt = get_prompt(*ANSWER_PROMPT)
        self.rewrite_prompt = get_prompt(*REWRITE_PROMPT)
        self.graph = self._build()

    # Nodes -------------------------------------------------------------------------------

    def parse_question(self, state: State) -> State:
        return {"explicit_ids": explicit_ids(state["question"])}

    def direct_lookup(self, state: State) -> State:
        with tracer.start_as_current_span(
            "retrieval direct_lookup",
            attributes={"gen_ai.operation.name": "retrieval", "ementa.retrieval.mode": "direct"},
        ):
            hits = self.retriever.get(state["explicit_ids"])
        return {"hits": hits, "queries": []}

    def rewrite_queries(self, state: State) -> State:
        question = state["question"]
        try:
            reply = self.models.chat(ModelRole.FAST).invoke(
                [SystemMessage(self.rewrite_prompt.text), HumanMessage(question)]
            )
            lines = [_BULLET.sub("", line).strip().strip('"') for line in reply.text.splitlines()]
        except Exception as error:  # a failed rewrite only costs recall
            log.warning("rewrite_failed", error=str(error))
            lines = []
        rewrites = [line for line in lines if line][:MAX_REWRITES]
        return {"queries": list(dict.fromkeys([question, *rewrites]))}

    def retrieve(self, state: State) -> State:
        return {"hits": self.retriever.search(state["queries"], limit=self.context_limit)}

    def assemble_context(self, state: State) -> State:
        sources = "\n\n".join(render_source(h) for h in state["hits"]) or "(nenhuma fonte)"
        messages = [
            SystemMessage(self.answer_prompt.text),
            build_user_turn([Section("fontes", sources)], state["question"]),
        ]
        return {"messages": messages, "attempts": 0}

    def generate_answer(self, state: State) -> State:
        reply = self.models.chat(ModelRole.DEFAULT).invoke(state["messages"])
        return {"messages": [reply], "attempts": state.get("attempts", 0) + 1}

    def check_citations(self, state: State) -> State:
        draft = state["messages"][-1].text
        with tracer.start_as_current_span(
            "guardrail citation_verifier",
            attributes={"gen_ai.operation.name": "guardrail", "ementa.guardrail.stage": "output"},
        ) as span:
            result = verify_citations(draft, allowed_ids(state["hits"]))
            decision = "pass" if not result.removed else "modified"
            if not result.has_citations:
                decision = "no_citations"
            span.set_attribute("ementa.guardrail.decision", decision)
            span.set_attribute("ementa.guardrail.removed", len(result.removed))
        return {"verification": result}

    def ask_again(self, state: State) -> State:
        return {"messages": [HumanMessage(feedback(state["verification"].removed))]}

    # Edges -------------------------------------------------------------------------------

    @staticmethod
    def _after_parse(state: State) -> Literal["direct_lookup", "rewrite_queries"]:
        return "direct_lookup" if state["explicit_ids"] else "rewrite_queries"

    @staticmethod
    def _after_lookup(state: State) -> Literal["assemble_context", "rewrite_queries"]:
        return "assemble_context" if state["hits"] else "rewrite_queries"

    @staticmethod
    def _after_check(state: State) -> Literal["ask_again", "__end__"]:
        result = state["verification"]
        failed = bool(result.removed) or not result.has_citations
        return "ask_again" if failed and state["attempts"] < MAX_ATTEMPTS else "__end__"

    def _build(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        graph = StateGraph(State)
        graph.add_node("parse_question", self.parse_question)
        graph.add_node("direct_lookup", self.direct_lookup)
        graph.add_node("rewrite_queries", self.rewrite_queries)
        graph.add_node("retrieve", self.retrieve)
        graph.add_node("assemble_context", self.assemble_context)
        graph.add_node("generate_answer", self.generate_answer)
        graph.add_node("verify_citations", self.check_citations)
        graph.add_node("ask_again", self.ask_again)
        graph.add_edge(START, "parse_question")
        graph.add_conditional_edges("parse_question", self._after_parse)
        graph.add_conditional_edges("direct_lookup", self._after_lookup)
        graph.add_edge("rewrite_queries", "retrieve")
        graph.add_edge("retrieve", "assemble_context")
        graph.add_edge("assemble_context", "generate_answer")
        graph.add_edge("generate_answer", "verify_citations")
        graph.add_conditional_edges("verify_citations", self._after_check)
        graph.add_edge("ask_again", "generate_answer")
        return graph.compile(name=AGENT_ID)

    # Entry point -------------------------------------------------------------------------

    def ask(self, question: str) -> ResearchAnswer:
        spec = self.models.spec(ModelRole.DEFAULT)
        prompt_id, prompt_version = ANSWER_PROMPT
        with tracer.start_as_current_span(
            f"invoke_agent {AGENT_ID}",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": AGENT_ID,
                "gen_ai.provider.name": spec.provider,
                "gen_ai.request.model": spec.model,
                "ementa.prompt.id": prompt_id,
                "ementa.prompt.version": prompt_version,
            },
        ) as span:
            state: State = self.graph.invoke({"question": question, "messages": []})  # type: ignore[assignment]
            answer = self._finalize(question, state, spec.model)
            span.set_attribute("ementa.answer.attempts", answer.attempts)
            span.set_attribute("ementa.answer.citations", [c.source_id for c in answer.citations])
            span.set_attribute("ementa.answer.removed", len(answer.removed))
            return answer

    def _finalize(self, question: str, state: State, model: str) -> ResearchAnswer:
        result = state["verification"]
        hits = state["hits"]
        by_id = {i: h for h in hits for i in (h.id, *h.provision_ids)}
        citations = [Citation(i, by_id[i].title, by_id[i].url) for i in result.valid if i in by_id]
        prompt_id, prompt_version = ANSWER_PROMPT
        return ResearchAnswer(
            question=question,
            answer=result.text if citations else NO_BASIS,
            citations=citations,
            removed=result.removed,
            sources=[h.id for h in hits],
            queries=state.get("queries", []),
            attempts=state["attempts"],
            prompt=f"{prompt_id}@{prompt_version}",
            model=model,
        )
