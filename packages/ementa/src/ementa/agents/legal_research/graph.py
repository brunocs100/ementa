"""`legal_research`: agentic RAG over the legal corpus, with a citation check and one retry.

parse_question -> direct_lookup (explicit article or súmula) or rewrite_queries -> retrieve
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
from ementa_kit.context import Section, build_context
from ementa_kit.llm import BudgetExceededError, ModelRegistry, ModelRole
from ementa_kit.rag import Hit
from ementa_kit.telemetry import get_logger, get_tracer

AGENT_ID = "legal_research"
ANSWER_PROMPT = ("legal_research.answer", 2)
REWRITE_PROMPT = ("legal_research.rewrite", 2)
MAX_ATTEMPTS = 2  # first answer plus one retry
CONTEXT_LIMIT = 5  # sources in the context without the rerank (F1); with it, rerank_top_k
MAX_REWRITES = 3

NOTICE = "Rascunho para conferência do advogado responsável. Não é aconselhamento jurídico."
NO_BASIS = (
    "Não encontrei base nas fontes recuperadas para responder com segurança. "
    "A pergunta precisa de pesquisa do advogado."
)

_NUMBER = r"\d{1,3}(?:\.\d{3})+|\d+"  # 1.003 or 219
_ONE_ARTICLE = rf"(?:{_NUMBER})\s*[º°]?(?:-[A-Za-z]\b)?"
_LIST = r"\s*(?:,|\be\b|\bou\b)\s*"
_ARTICLES = re.compile(
    rf"\bart(?:igo)?s?\.?\s*({_ONE_ARTICLE}(?:{_LIST}{_ONE_ARTICLE})*)", re.IGNORECASE
)
_ARTICLE_NUMBER = re.compile(rf"({_NUMBER})\s*[º°]?(?:-([A-Za-z])\b)?")
_SUMULAS = re.compile(
    rf"\bs[úu]mulas?\s+(vinculantes?\s+)?(?:n[º°o.]*\s*)?(\d+(?:{_LIST}\d+)*)", re.IGNORECASE
)
_SV = re.compile(rf"\bSVs?\s*(?:n[º°o.]*\s*)?(\d+(?:{_LIST}\d+)*)")
_LAW_NUMBER = r"\blei\s+(?:n[º°o.]*\s*)?"
_LAWS = {
    "cdc": rf"\bcdc\b|c[óo]digo de defesa do consumidor|{_LAW_NUMBER}8\.?078\b",
    "lgpd": rf"\blgpd\b|lei (?:geral )?de prote[çc][ãa]o de dados|{_LAW_NUMBER}13\.?709\b",
    "cpc": rf"\bn?cpc\b|c[óo]digo de processo civil|{_LAW_NUMBER}13\.?105\b",
    # Laws outside the corpus: an article of one of them is not looked up.
    "other": (
        r"c[óo]digo (?:civil|penal|de processo penal|tribut[áa]rio|eleitoral)|\bcc\b|\bcpp?\b"
        r"|\bclt\b|\bctn\b|\beca\b|\bcf\b|\bcrfb\b|constitui[çc][ãa]o|estatuto"
        rf"|decreto|resolu[çc][ãa]o|medida provis[óo]ria|lei complementar|{_LAW_NUMBER}[\d.]+"
    ),
}
_COURTS = {"stj": r"\bstj\b|superior tribunal de justi[çc]a", "stf": r"\bstf\b|supremo"}
_STOP = re.compile(r"[.;!?](?!\d)|\bart(?:igo)?s?\.?\s*\d|\bs[úu]mula|\bSV\s*\d", re.IGNORECASE)
_WINDOW = 40  # how far after a reference its law or court may be named
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


Mention = tuple[int, int, str]  # start, end, name


def _mentions(text: str, patterns: dict[str, str]) -> list[Mention]:
    """Where each law or court is named. A corpus law wins over "other" on the same words."""
    found = [
        (m.start(), m.end(), name)
        for name, pattern in patterns.items()
        for m in re.finditer(pattern, text, re.IGNORECASE)
    ]
    named = [f for f in found if f[2] != "other"]
    return sorted(
        f for f in found if f[2] != "other" or not any(s < f[1] and f[0] < e for s, e, _ in named)
    )


def _owner(text: str, match: re.Match[str], mentions: list[Mention]) -> str | None:
    """The law or court named right after a reference ("art. 49 do CDC"), else the last one
    named before it ("No CPC, o art. 219"), else None."""
    stop = _STOP.search(text, match.end())
    limit = min(stop.start() if stop else len(text), match.end() + _WINDOW)
    after = [name for start, _, name in mentions if match.end() <= start < limit]
    if after:
        return after[0]
    before = [name for _, end, name in mentions if end <= match.start()]
    return before[-1] if before else None


def explicit_ids(question: str) -> list[str]:
    """Articles and súmulas named in the question, as source ids, in the order they appear.

    "art. 49 do CDC" is `cdc:art49`, "arts. 219 e 224 do CPC" are `cpc:art219` and `cpc:art224`,
    "Súmula 385 do STJ" is `stj:sumula:385`, "Súmula Vinculante 13" or "SV 13" is `stf:sv:13`.
    An article with no law named is a CDC article, as in F1. Articles of laws outside the corpus
    (Código Civil, Constituição) and súmulas of the STF that are not vinculantes are left out; a
    súmula with no court named is an STJ súmula only when the question names the STJ.
    """
    laws, courts = _mentions(question, _LAWS), _mentions(question, _COURTS)
    found: list[tuple[int, str]] = []
    for m in _ARTICLES.finditer(question):
        law = _owner(question, m, laws) or "cdc"
        if law == "other":
            continue
        for n in _ARTICLE_NUMBER.finditer(m.group(1)):
            letter = f"-{n.group(2).upper()}" if n.group(2) else ""
            found.append((m.start(), f"{law}:art{n.group(1).replace('.', '')}{letter}"))
    for m in _SUMULAS.finditer(question):
        court = _owner(question, m, courts)
        if m.group(1):
            prefix = "stf:sv"
        elif court == "stj" or (court is None and any(c[2] == "stj" for c in courts)):
            prefix = "stj:sumula"
        else:
            continue
        found.extend((m.start(), f"{prefix}:{n}") for n in re.findall(r"\d+", m.group(2)))
    for m in _SV.finditer(question):
        found.extend((m.start(), f"stf:sv:{n}") for n in re.findall(r"\d+", m.group(1)))
    return list(dict.fromkeys(i for _, i in sorted(found, key=lambda f: f[0])))


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
        except BudgetExceededError:  # a spent budget stops the run, it is not a failed rewrite
            raise
        except Exception as error:  # a failed rewrite only costs recall
            log.warning("rewrite_failed", error=str(error))
            lines = []
        rewrites = [line for line in lines if line][:MAX_REWRITES]
        return {"queries": list(dict.fromkeys([question, *rewrites]))}

    def retrieve(self, state: State) -> State:
        return {"hits": self.retriever.search(state["queries"], limit=self.context_limit)}

    def assemble_context(self, state: State) -> State:
        sources = "\n\n".join(render_source(h) for h in state["hits"]) or "(nenhuma fonte)"
        # The size of each section goes to the invoke_agent span as an event.
        messages = build_context(
            self.answer_prompt.text, [Section("fontes", sources)], state["question"]
        )
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
        # Called from another agent's node (the assistant's tools), a graph would inherit that
        # agent's checkpointer and store its sources in the caller's thread. Each run stands alone.
        return graph.compile(name=AGENT_ID, checkpointer=False)

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
