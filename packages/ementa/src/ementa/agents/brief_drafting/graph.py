"""`brief_drafting`: plan and execute an initial petition in consumer law, with a critic, a
citation check that blocks, and the lawyer's approval before anything is final.

protect -> extract_facts -> plan_theses -> research_thesis (one per thesis, in parallel with
`Send`) -> write_draft -> critique, with one revision when it fails -> verify_citations, with one
revision when a citation fails, after which the sentence is removed and flagged ->
prepare_review -> human_review, paused on `ask_for_approval` -> finalize.

Only pseudonymized text reaches a model. `protect` masks the case description with one marker
mapping per run, kept in the state, and the examples with a copy of it that is thrown away: a
marker from a past case never turns back into a value in this brief. The draft the lawyer
approves has the real values back and its source ids written as references.
"""

import difflib
import operator
import re
import string
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, TypedDict, cast

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import BaseModel, ValidationError

from ementa.agents.brief_drafting.export import BRASILIA
from ementa.agents.brief_drafting.library import BriefLibrary, BriefTemplate, Opinion
from ementa.agents.brief_drafting.schemas import (
    BriefExample,
    BriefOutcome,
    BriefPending,
    Critique,
    Facts,
    Party,
    Plan,
)
from ementa.agents.legal_research import ResearchAnswer
from ementa.domain.citations import citation_label, label_citations, verify_citations
from ementa.prompts import get_prompt
from ementa.tools.research import CitationOut, RemovedOut
from ementa_kit.context import Section, build_context
from ementa_kit.hitl import ApprovalDecision, ApprovalRequest, ask_for_approval
from ementa_kit.llm import BudgetExceededError, ModelRegistry, ModelRole
from ementa_kit.policy import DataClass
from ementa_kit.telemetry import get_logger, get_tracer

if TYPE_CHECKING:  # Presidio and spaCy take seconds to import; they load on the first run
    from ementa_kit.guardrails.pii import PiiGuard

AGENT_ID = "brief_drafting"
KIND = "brief"
LEGAL_RESEARCH = "legal_research"
EXTRACT_PROMPT = ("brief_drafting.extract", 1)
PLAN_PROMPT = ("brief_drafting.plan", 1)
DRAFT_PROMPT = ("brief_drafting.draft", 1)
CRITIQUE_PROMPT = ("brief_drafting.critique", 1)
MAX_THESES = 4  # researched in parallel, one legal_research run each
MAX_DEFENDANTS = 3
MAX_EXAMPLES = 3  # past briefs in the drafting prompt
MAX_EXAMPLE_CHARS = 3000  # each, cut before the model sees it
CRITERIA = {  # the critic's rubric, with the names the lawyer reads
    "completeness": "completude",
    "citations": "citações",
    "requests": "pedidos",
    "tone": "tom",
}

EXTRACT_ASK = "Separe os fatos da descrição acima no formato pedido."
PLAN_ASK = "Planeje as teses da petição no formato pedido."
DRAFT_ASK = "Redija as três seções da petição no formato pedido."
CRITIQUE_ASK = "Avalie o rascunho em <peca> pela rubrica, no formato pedido."
CRITIQUE_AGAIN = "A peça foi reescrita. Avalie a nova versão pela mesma rubrica, no mesmo formato."
FIX_JSON = (
    "A resposta anterior não é um JSON no formato pedido. Responda de novo só com o JSON, sem "
    "comentário."
)
FIX_FORMAT = (
    "A resposta anterior não veio no formato pedido. Responda de novo só com as três seções, "
    "<fatos>, <fundamentos> e <pedidos>."
)
REVISE = (
    "A revisão do rascunho apontou:\n{notes}\n"
    "Reescreva as três seções inteiras, no mesmo formato, corrigindo esses pontos."
)
FIX_CITATIONS = (
    "O verificador de citações recusou o rascunho:\n{problems}\n"
    "Reescreva as três seções inteiras, no mesmo formato, citando só os ids das fontes de "
    "<teses>. Afirmação sem fonte verificada sai da peça."
)
BLANK = "[A COMPLETAR: {}]"
NO_THESES = "(nenhuma tese com fonte verificada)"
MONTHS = [
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
]

_SECTION = re.compile(r"<(fatos|fundamentos|pedidos)>(.*?)</\1>", re.DOTALL)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)]|[a-z][.)])\s+")

tracer = get_tracer(__name__)
log = get_logger(__name__)


class Researcher(Protocol):
    def ask(self, question: str) -> ResearchAnswer: ...


class BriefDraftingError(RuntimeError):
    """A model answered out of the format twice: the run stops at that node."""


class NoPendingApprovalError(LookupError):
    """No run with this thread id is waiting for a decision."""


class SourceRecord(TypedDict):
    source_id: str
    title: str
    url: str


class ExampleRecord(TypedDict):
    draft: str
    final: str
    comment: str | None


class PartyRecord(TypedDict):
    name: str
    qualification: str


class FactsRecord(TypedDict):
    plaintiff: PartyRecord
    defendants: list[PartyRecord]
    court: str
    city: str
    facts: list[str]
    evidence: list[str]
    wants: list[str]
    missing: list[str]


class ThesisRecord(TypedDict):
    id: str  # t1, t2, ... in the order of the plan
    title: str
    question: str  # what legal_research is asked
    request: str  # the request the thesis supports
    opinion: str | None  # the firm opinion it follows


class ThesisTask(TypedDict):
    """What `Send` hands to each `research_thesis`."""

    thesis: ThesisRecord


class ResearchRecord(TypedDict):
    thesis_id: str
    answer: str  # the verified answer of legal_research, empty without a source
    citations: list[SourceRecord]  # verified by legal_research: what the brief may cite
    failed: bool  # the research raised; the thesis has no source


class CritiqueRecord(TypedDict):
    status: Literal["passed", "failed", "unavailable"]
    notes: dict[str, str]  # failed criterion -> what to fix


class RemovedRecord(TypedDict):
    reference: str
    reason: str


class CheckRecord(TypedDict):
    text: str  # the draft without the sentences whose citations failed
    valid: list[str]
    removed: list[RemovedRecord]


class ReviewRecord(TypedDict):
    """What the lawyer is asked to approve, with the real values and references written out."""

    title: str
    draft: str
    summary: str


class OutcomeRecord(TypedDict):
    status: Literal["approved", "edited", "rejected"]
    text: str | None
    decided_by: str
    decided_at: str  # ISO 8601, UTC
    comment: str | None


class State(TypedDict, total=False):
    template: str
    description: str  # masked by `protect`
    examples: list[ExampleRecord]  # masked by `protect`
    owner_id: str
    pii_mapping: dict[str, str]  # marker -> value of the case description; rehydrates the draft
    facts: FactsRecord
    theses: list[ThesisRecord]
    research: Annotated[list[ResearchRecord], operator.add]  # one per thesis, in parallel
    messages: Annotated[list[BaseMessage], operator.add]  # the drafting conversation; only grows
    critic_messages: Annotated[list[BaseMessage], operator.add]  # the critic's own conversation
    draft: str  # the last draft in the template, masked, with source ids in brackets
    drafts: int
    critiques: Annotated[list[CritiqueRecord], operator.add]
    revision: Literal["critic", "citations"]  # why the last draft was rewritten
    check: CheckRecord
    review: ReviewRecord
    decision: dict[str, Any]  # the ApprovalDecision, as dumped
    outcome: OutcomeRecord


class BriefDraftingAgent:
    """`draft` runs a brief until it waits for the lawyer; `resume` finishes it with the
    lawyer's decision; `pending` and `outcome` read a run back.

    The graph is compiled with `checkpointer`, so a run waits for days and survives a restart.
    The approvals API may also resume it straight on the graph, with
    `Command(resume=decision.model_dump())` and the run's thread id: `resume` adds the span and
    checks the decision first. `researcher` and `guard` are factories called on the first run:
    the research agent loads the embedding model and the guard loads spaCy.
    """

    def __init__(
        self,
        models: ModelRegistry,
        researcher: Callable[[], Researcher],
        checkpointer: BaseCheckpointSaver[Any],
        guard: Callable[[], "PiiGuard"],
        library: BriefLibrary,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.models = models
        self._researcher = researcher
        self._guard = guard
        self.library = library
        self.clock = clock
        self.extract_prompt = get_prompt(*EXTRACT_PROMPT)
        self.plan_prompt = get_prompt(*PLAN_PROMPT)
        self.draft_prompt = get_prompt(*DRAFT_PROMPT)
        self.critique_prompt = get_prompt(*CRITIQUE_PROMPT)
        self.graph = self._build(checkpointer)

    # Nodes -------------------------------------------------------------------------------

    def protect(self, state: State) -> State:
        """Mask the case description and the examples before any model sees them. The mapping
        of the description stays for the run; the examples are masked with a copy of it, so a
        value both mention keeps its marker and a value of a past case gets one of its own,
        which is never put back."""
        guard = self._guard()
        masked = guard.pseudonymize(state["description"], stage="input")
        seen = masked.mapping
        examples: list[ExampleRecord] = []
        for example in state.get("examples", [])[:MAX_EXAMPLES]:
            texts: list[str] = []
            for text in (example["draft"], example["final"], example["comment"] or ""):
                hidden = guard.pseudonymize(text, seen, stage="input") if text else None
                seen = hidden.mapping if hidden else seen
                texts.append(hidden.text if hidden else "")
            examples.append(ExampleRecord(draft=texts[0], final=texts[1], comment=texts[2] or None))
        return {"description": masked.text, "examples": examples, "pii_mapping": masked.mapping}

    def extract_facts(self, state: State) -> State:
        request = build_context(
            self.extract_prompt.text, [Section("descricao", state["description"])], EXTRACT_ASK
        )
        facts, _ = self._ask_json(ModelRole.FAST, request, Facts)
        if facts is None:
            raise BriefDraftingError("the facts came back out of the JSON format twice")
        return {"facts": _facts_record(facts)}

    def plan_theses(self, state: State) -> State:
        template = self.library.template(state["template"])
        opinions = self.library.opinions()
        sections = [
            Section("modelo", _template_section(template)),
            Section("caso", _facts_section(state["facts"])),
            Section("pareceres", "\n\n".join(_opinion_section(o) for o in opinions) or "(nenhum)"),
        ]
        request = build_context(self.plan_prompt.text, sections, PLAN_ASK)
        plan, _ = self._ask_json(ModelRole.REASONING, request, Plan)
        if plan is None:
            raise BriefDraftingError("the plan came back out of the JSON format twice")
        known = {o.id for o in opinions}
        theses = [
            ThesisRecord(
                id=f"t{n}",
                title=t.title.strip(),
                question=t.question.strip(),
                request=t.request.strip(),
                opinion=t.opinion if t.opinion in known else None,
            )
            for n, t in enumerate(plan.theses[:MAX_THESES], start=1)
        ]
        # Built here, once: the theses call it from parallel threads, and a first call from
        # each would load the embedding model once per thread.
        self._researcher()
        return {"theses": theses}

    def research_thesis(self, state: ThesisTask) -> State:
        """One thesis through legal_research, which retrieves, answers and verifies. The brief
        may cite only what it verified. A research that fails leaves the thesis without a
        source, out of the brief; a spent budget stops the run."""
        thesis = state["thesis"]
        with tracer.start_as_current_span(
            f"execute_tool {LEGAL_RESEARCH}",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": LEGAL_RESEARCH,
                "ementa.data_class": DataClass.PUBLIC.value,
                "ementa.brief.thesis": thesis["id"],
            },
        ) as span:
            try:
                answer = self._researcher().ask(thesis["question"])
            except BudgetExceededError:
                raise
            except Exception as exc:
                log.warning("thesis_research_failed", thesis=thesis["id"], error=type(exc).__name__)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("ementa.tool.status", "failed")
                failed = ResearchRecord(
                    thesis_id=thesis["id"], answer="", citations=[], failed=True
                )
                return {"research": [failed]}
            cited = [
                SourceRecord(source_id=c.source_id, title=c.title, url=c.url)
                for c in answer.citations
            ]
            span.set_attribute("ementa.tool.status", "finished")
            span.set_attribute("ementa.answer.citations", [c["source_id"] for c in cited])
        record = ResearchRecord(
            thesis_id=thesis["id"],
            answer=answer.answer if cited else "",
            citations=cited,
            failed=False,
        )
        return {"research": [record]}

    def write_draft(self, state: State) -> State:
        """The three sections from the `reasoning` model, put into the template. A revision goes
        on the same conversation: its feedback is already the last message."""
        history = state.get("messages", [])
        opening = [] if history else self._opening(state)
        request = [*history, *opening]
        model = self.models.chat(ModelRole.REASONING)
        reply = model.invoke(request)
        exchanged: list[BaseMessage] = [reply]
        sections = _sections(reply.text)
        if sections is None:  # one more try: the reply stays and the correction goes after it
            fix = HumanMessage(FIX_FORMAT)
            again = model.invoke([*request, reply, fix])
            exchanged += [fix, again]
            sections = _sections(again.text)
        if sections is None:
            raise BriefDraftingError("the draft came back without its three sections twice")
        return {
            "messages": [*opening, *exchanged],
            "draft": self._render(state, sections),
            "drafts": state.get("drafts", 0) + 1,
        }

    def critique(self, state: State) -> State:
        """The rubric from the `reasoning` model, on a conversation of its own. The second
        critique appends the rewritten draft to the first one, so the critic sees what it
        asked for."""
        previous = state.get("critic_messages", [])
        if previous:
            new: list[BaseMessage] = [
                HumanMessage(f"{Section('peca', state['draft']).render()}\n\n{CRITIQUE_AGAIN}")
            ]
        else:
            sections = [
                Section("modelo", _template_section(self.library.template(state["template"]))),
                Section("caso", _facts_section(state["facts"])),
                Section("teses", _thesis_titles(state)),
                Section("fontes", _sources_section(state)),
                Section("peca", state["draft"]),
            ]
            new = build_context(self.critique_prompt.text, sections, CRITIQUE_ASK)
        verdict, exchanged = self._ask_json(ModelRole.REASONING, [*previous, *new], Critique)
        record = _critique_record(verdict)
        trace.get_current_span().add_event(
            "ementa.brief.critique",
            {
                "status": record["status"],
                "failed": list(record["notes"]),
                "round": len(state.get("critiques", [])) + 1,
            },
        )
        return {"critic_messages": [*new, *exchanged], "critiques": [record]}

    def revise_draft(self, state: State) -> State:
        notes = state["critiques"][-1]["notes"]
        lines = "\n".join(f"- {CRITERIA[name]}: {note}" for name, note in notes.items())
        return {"messages": [HumanMessage(REVISE.format(notes=lines))], "revision": "critic"}

    def check_citations(self, state: State) -> State:
        """Every citation of the draft against what legal_research verified for its theses. A
        failure sends the draft back once; after that, the failing sentences are removed and
        listed for the lawyer. Either way, no unverified citation reaches the approval."""
        with tracer.start_as_current_span(
            "guardrail citation_verifier",
            attributes={
                "gen_ai.operation.name": "guardrail",
                "ementa.guardrail.name": "citations",
                "ementa.guardrail.stage": "output",
            },
        ) as span:
            result = verify_citations(state["draft"], _allowed(state))
            if not result.removed:
                decision = "pass"
            else:
                decision = "block" if _may_fix_citations(state) else "modified"
            span.set_attribute("ementa.guardrail.decision", decision)
            span.set_attribute("ementa.guardrail.removed", len(result.removed))
        removed = [RemovedRecord(reference=r.reference, reason=r.reason) for r in result.removed]
        return {"check": CheckRecord(text=result.text, valid=result.valid, removed=removed)}

    def fix_citations(self, state: State) -> State:
        problems = "\n".join(
            f"- {r['reference']}: {r['reason']}" for r in state["check"]["removed"]
        )
        return {
            "messages": [HumanMessage(FIX_CITATIONS.format(problems=problems))],
            "revision": "citations",
        }

    def prepare_review(self, state: State) -> State:
        """The draft as the lawyer reads it: real values back, ids written as references."""
        from ementa_kit.guardrails.pii import rehydrate

        mapping = state.get("pii_mapping", {})
        template = self.library.template(state["template"])
        draft = rehydrate(label_citations(state["check"]["text"]), mapping)
        summary = rehydrate(_summary(state, template), mapping)
        return {"review": ReviewRecord(title=template.title, draft=draft, summary=summary)}

    def human_review(self, state: State) -> State:
        """Pause until the lawyer decides. A decision that cannot be applied (not a decision,
        or an edit without its text) is asked for again: raised here, it would stay stored as
        the answer to this pause, and every later decision would replay it."""
        review = state["review"]
        request = ApprovalRequest(
            agent=AGENT_ID,
            kind=KIND,
            title=review["title"],
            draft=review["draft"],
            summary=review["summary"],
            data_class=DataClass.RESTRICTED,
        )
        while True:
            try:
                decision = ask_for_approval(request)
            except ValidationError:
                log.warning("approval_decision_invalid", agent=AGENT_ID)
                continue
            if decision.action != "edit" or (decision.text or "").strip():
                return {"decision": decision.model_dump(mode="json")}
            log.warning("approval_edit_without_text", agent=AGENT_ID)

    def finalize(self, state: State) -> State:
        decision = ApprovalDecision.model_validate(state["decision"])
        status: Literal["approved", "edited", "rejected"]
        text: str | None
        if decision.action == "approve":
            status, text = "approved", state["review"]["draft"]
        elif decision.action == "edit":
            status, text = "edited", decision.text
        else:
            status, text = "rejected", None
        outcome = OutcomeRecord(
            status=status,
            text=text,
            decided_by=decision.decided_by,
            decided_at=self.clock().astimezone(UTC).isoformat(),
            comment=decision.comment,
        )
        return {"outcome": outcome}

    # Edges -------------------------------------------------------------------------------

    @staticmethod
    def _fan_out(state: State) -> list[Send]:
        return [Send("research_thesis", ThesisTask(thesis=t)) for t in state["theses"]]

    @staticmethod
    def _after_draft(state: State) -> Literal["critique", "verify_citations"]:
        # A rewrite for the citations changes no argument: it goes straight back to the check.
        return "verify_citations" if state.get("revision") == "citations" else "critique"

    @staticmethod
    def _after_critique(state: State) -> Literal["revise_draft", "verify_citations"]:
        failed = state["critiques"][-1]["status"] == "failed"
        return "revise_draft" if failed and "revision" not in state else "verify_citations"

    @staticmethod
    def _after_check(state: State) -> Literal["fix_citations", "prepare_review"]:
        blocked = bool(state["check"]["removed"]) and _may_fix_citations(state)
        return "fix_citations" if blocked else "prepare_review"

    def _build(
        self, checkpointer: BaseCheckpointSaver[Any]
    ) -> CompiledStateGraph[Any, Any, Any, Any]:
        graph = StateGraph(State)
        graph.add_node("protect", self.protect)
        graph.add_node("extract_facts", self.extract_facts)
        graph.add_node("plan_theses", self.plan_theses)
        graph.add_node("research_thesis", self.research_thesis)
        graph.add_node("write_draft", self.write_draft)
        graph.add_node("critique", self.critique)
        graph.add_node("revise_draft", self.revise_draft)
        graph.add_node("verify_citations", self.check_citations)
        graph.add_node("fix_citations", self.fix_citations)
        graph.add_node("prepare_review", self.prepare_review)
        graph.add_node("human_review", self.human_review)
        graph.add_node("finalize", self.finalize)
        graph.add_edge(START, "protect")
        graph.add_edge("protect", "extract_facts")
        graph.add_edge("extract_facts", "plan_theses")
        graph.add_conditional_edges("plan_theses", self._fan_out, ["research_thesis"])
        graph.add_edge("research_thesis", "write_draft")  # once every thesis is back
        graph.add_conditional_edges("write_draft", self._after_draft)
        graph.add_conditional_edges("critique", self._after_critique)
        graph.add_edge("revise_draft", "write_draft")
        graph.add_conditional_edges("verify_citations", self._after_check)
        graph.add_edge("fix_citations", "write_draft")
        graph.add_edge("prepare_review", "human_review")
        graph.add_edge("human_review", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=checkpointer, name=AGENT_ID)

    # Entry points ------------------------------------------------------------------------

    def draft(
        self,
        description: str,
        *,
        template: str,
        user_id: str,
        examples: Sequence[BriefExample] = (),
    ) -> BriefPending:
        """Draft a brief from the lawyer's description of the case, on the firm's `template`,
        and stop before finalizing. `examples` are briefs the firm approved before, the most
        relevant first; the drafting prompt takes the first three.

        An unknown template raises UnknownTemplateError and a model without a key
        ModelUnavailableError, both before any model call.
        """
        self.library.template(template)
        if not description.strip():
            raise ValueError("the case description is empty")
        self.models.chat(ModelRole.REASONING)
        self.models.chat(ModelRole.FAST)
        thread_id = str(uuid.uuid4())
        inputs = State(
            template=template,
            description=description,
            examples=[
                ExampleRecord(draft=e.draft, final=e.final, comment=e.comment) for e in examples
            ],
            owner_id=user_id,
        )
        with tracer.start_as_current_span(
            f"invoke_agent {AGENT_ID}", attributes=self._attributes(thread_id)
        ) as span:
            span.set_attribute("ementa.brief.template", template)
            self.graph.invoke(inputs, _config(thread_id))
            pending = self.pending(thread_id)
            if pending is None:
                raise RuntimeError("the run ended without asking for approval")
            _record_run(span, self._values(thread_id))
            span.set_attribute("ementa.approval.status", "pending")
            return pending

    def pending(self, thread_id: str) -> BriefPending | None:
        """What a paused run waits on, or None if no run with this id waits."""
        snapshot = self.graph.get_state(_config(thread_id))
        values = cast(State, snapshot.values)
        for paused in snapshot.interrupts:
            request = ApprovalRequest.model_validate(paused.value)
            return BriefPending(
                thread_id=thread_id, owner_id=values.get("owner_id", ""), request=request
            )
        return None

    def resume(self, thread_id: str, decision: ApprovalDecision) -> BriefOutcome:
        """Finish a paused run with the lawyer's decision: approve finalizes the draft as the
        lawyer saw it, edit finalizes the lawyer's text and reject ends with no brief.

        Raises NoPendingApprovalError when no run with this id waits, and ValueError for an edit
        without its text, both before resuming anything.
        """
        if decision.action == "edit" and not (decision.text or "").strip():
            raise ValueError("an edit needs the edited text")
        if self.pending(thread_id) is None:
            raise NoPendingApprovalError(thread_id)
        with tracer.start_as_current_span(
            f"invoke_agent {AGENT_ID}", attributes=self._attributes(thread_id)
        ) as span:
            span.set_attribute("ementa.approval.action", decision.action)
            self.graph.invoke(Command(resume=decision.model_dump(mode="json")), _config(thread_id))
            outcome = self.outcome(thread_id)
            if outcome is None:
                raise RuntimeError("the run did not finish after the decision")
            if outcome.text is not None and outcome.status == "edited":
                span.set_attribute("ementa.brief.changed_lines", _changed_lines(outcome))
            span.set_attribute("ementa.approval.status", outcome.status)
            return outcome

    def outcome(self, thread_id: str) -> BriefOutcome | None:
        """The result of a finished run, or None while it waits or if it does not exist."""
        values = self._values(thread_id)
        outcome = values.get("outcome")
        if outcome is None:
            return None
        sources = {s["source_id"]: s for r in values.get("research", []) for s in r["citations"]}
        return BriefOutcome(
            thread_id=thread_id,
            status=outcome["status"],
            title=values["review"]["title"],
            text=outcome["text"],
            draft=values["review"]["draft"],
            decided_by=outcome["decided_by"],
            decided_at=datetime.fromisoformat(outcome["decided_at"]),
            comment=outcome["comment"],
            citations=[CitationOut(**sources[i]) for i in values["check"]["valid"] if i in sources],
            removed=[RemovedOut(**r) for r in values["check"]["removed"]],
        )

    # Helpers -----------------------------------------------------------------------------

    def _values(self, thread_id: str) -> State:
        return cast(State, self.graph.get_state(_config(thread_id)).values)

    def _attributes(self, thread_id: str) -> dict[str, Any]:
        spec = self.models.spec(ModelRole.REASONING)
        prompt_id, version = DRAFT_PROMPT
        return {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": AGENT_ID,
            "gen_ai.conversation.id": thread_id,
            "gen_ai.provider.name": spec.provider,
            "gen_ai.request.model": spec.model,
            "ementa.prompt.id": prompt_id,
            "ementa.prompt.version": version,
        }

    def _ask_json[T: BaseModel](
        self, role: ModelRole, request: Sequence[BaseMessage], schema: type[T]
    ) -> tuple[T | None, list[BaseMessage]]:
        """The model's JSON reply as `schema`, with one more try appended to the conversation
        when it does not parse. Returns None after the second miss, and the messages the
        exchange added after `request`."""
        model = self.models.chat(role)
        reply = model.invoke(list(request))
        parsed = _parse(reply.text, schema)
        if parsed is not None:
            return parsed, [reply]
        fix = HumanMessage(FIX_JSON)
        again = model.invoke([*request, reply, fix])
        parsed = _parse(again.text, schema)
        if parsed is None:
            log.warning("model_reply_rejected", agent=AGENT_ID, schema=schema.__name__)
        return parsed, [reply, fix, again]

    def _opening(self, state: State) -> list[BaseMessage]:
        """The first drafting request: system prompt, then the template, the case, the
        researched theses, the opinions they follow and the examples, the task last."""
        template = self.library.template(state["template"])
        grounded = _grounded(state)
        sections = [
            Section("modelo", _template_section(template)),
            Section("caso", _facts_section(state["facts"])),
            Section("teses", _theses_section(grounded)),
        ]
        cited = dict.fromkeys(t["opinion"] for t, _ in grounded if t["opinion"])
        opinions = [o for i in cited if (o := self.library.opinion(i)) is not None]
        if opinions:
            sections.append(
                Section("pareceres", "\n\n".join(_opinion_section(o) for o in opinions))
            )
        if state.get("examples"):
            sections.append(Section("exemplos", _examples_section(state["examples"])))
        return build_context(self.draft_prompt.text, sections, DRAFT_ASK)

    def _render(self, state: State, sections: dict[str, str]) -> str:
        template = self.library.template(state["template"])
        facts = state["facts"]
        requests = [*_lines(sections["pedidos"]), *template.fixed_requests]
        defendants = [_party(p, "réu") for p in facts["defendants"]] or [BLANK.format("réu")]
        today = self.clock().astimezone(BRASILIA).date()
        return template.render(
            {
                "juizo": facts["court"].strip().upper() or BLANK.format("JUÍZO"),
                "autor": _party(facts["plaintiff"], "autor"),
                "reu": "; e ".join(defendants),
                "fatos": sections["fatos"],
                "fundamentos": sections["fundamentos"],
                "pedidos": "\n".join(f"{_letter(n)}) {r}" for n, r in enumerate(requests)),
                "local_data": f"{facts['city'].strip() or BLANK.format('cidade')}, {_long(today)}.",
            }
        )


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _parse[T: BaseModel](text: str, schema: type[T]) -> T | None:
    raw = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        return schema.model_validate_json(raw)
    except ValidationError:
        return None


def _sections(text: str) -> dict[str, str] | None:
    """The three tagged sections of a draft, or None if one is missing or empty."""
    found = {m.group(1): m.group(2).strip() for m in _SECTION.finditer(text)}
    if len(found) < 3 or not all(found.values()):
        return None
    return found


def _lines(text: str) -> list[str]:
    """One request per line, without the bullets or letters the model may have added."""
    return [_BULLET.sub("", line).strip() for line in text.splitlines() if line.strip()]


def _letter(n: int) -> str:
    """a, b, ..., z, aa, ab: the requests of a brief are lettered."""
    letters = string.ascii_lowercase
    return letters[n] if n < len(letters) else _letter(n // len(letters) - 1) + letters[n % 26]


def _long(day: date) -> str:
    return f"{day.day} de {MONTHS[day.month - 1]} de {day.year}"


def _party(party: PartyRecord, role: str) -> str:
    name = party["name"].strip() or BLANK.format(role)
    qualification = party["qualification"].strip() or BLANK.format(f"qualificação do {role}")
    return f"{name}, {qualification}"


def _facts_record(facts: Facts) -> FactsRecord:
    def party(p: Party) -> PartyRecord:
        return PartyRecord(name=p.name.strip(), qualification=p.qualification.strip())

    return FactsRecord(
        plaintiff=party(facts.plaintiff),
        defendants=[party(p) for p in facts.defendants[:MAX_DEFENDANTS]],
        court=facts.court.strip(),
        city=facts.city.strip(),
        facts=facts.facts,
        evidence=facts.evidence,
        wants=facts.wants,
        missing=facts.missing,
    )


def _critique_record(verdict: Critique | None) -> CritiqueRecord:
    """A critic that does not answer in the format costs the brief its review, not the brief:
    the draft goes on to the citation check and the lawyer is told."""
    if verdict is None:
        return CritiqueRecord(status="unavailable", notes={})
    notes = {
        name: getattr(verdict, name).note.strip() or "sem detalhe"
        for name in CRITERIA
        if not getattr(verdict, name).ok
    }
    return CritiqueRecord(status="failed" if notes else "passed", notes=notes)


def _grounded(state: State) -> list[tuple[ThesisRecord, ResearchRecord]]:
    """The theses legal_research found a verified source for, in the order of the plan."""
    found = {r["thesis_id"]: r for r in state.get("research", []) if r["citations"]}
    return [(t, found[t["id"]]) for t in state.get("theses", []) if t["id"] in found]


def _allowed(state: State) -> set[str]:
    return {c["source_id"] for _, r in _grounded(state) for c in r["citations"]}


def _may_fix_citations(state: State) -> bool:
    return state.get("revision") != "citations"


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "- (nada informado)"


def _template_section(template: BriefTemplate) -> str:
    return (
        f"Peça: {template.title}\n{template.description}\n\n"
        f"O que cada seção precisa cobrir:\n{template.guidance}\n\n"
        f"Pedidos fixos do modelo, que o sistema acrescenta:\n{_bullets(template.fixed_requests)}"
    )


def _facts_section(facts: FactsRecord) -> str:
    defendants = [f"{p['name']}, {p['qualification']}" for p in facts["defendants"]]
    return "\n".join(
        [
            f"Autor: {facts['plaintiff']['name']}, {facts['plaintiff']['qualification']}",
            f"Réus:\n{_bullets(defendants)}",
            f"Juízo: {facts['court'] or '(não informado)'}",
            f"Cidade: {facts['city'] or '(não informada)'}",
            f"Fatos:\n{_bullets(facts['facts'])}",
            f"Provas:\n{_bullets(facts['evidence'])}",
            f"O que o cliente quer:\n{_bullets(facts['wants'])}",
            f"Falta na descrição:\n{_bullets(facts['missing'])}",
        ]
    )


def _opinion_section(opinion: Opinion) -> str:
    return f"Parecer {opinion.id}: {opinion.title}\n{opinion.body.strip()}"


def _theses_section(grounded: Sequence[tuple[ThesisRecord, ResearchRecord]]) -> str:
    blocks = []
    for thesis, research in grounded:
        sources = "\n".join(f"- [{c['source_id']}] {c['title']}" for c in research["citations"])
        blocks.append(
            f"Tese {thesis['id']}: {thesis['title']}\n"
            f"Pedido que sustenta: {thesis['request'] or '(não indicado)'}\n"
            f"Pesquisa verificada: {research['answer']}\n"
            f"Fontes que pode citar:\n{sources}"
        )
    return "\n\n".join(blocks) or NO_THESES


def _thesis_titles(state: State) -> str:
    lines = [
        f"- {t['title']}: {t['request'] or '(pedido não indicado)'}" for t, _ in _grounded(state)
    ]
    return "\n".join(lines) or NO_THESES


def _sources_section(state: State) -> str:
    seen = {c["source_id"]: c for _, r in _grounded(state) for c in r["citations"]}
    return "\n".join(f"[{i}] {c['title']}" for i, c in seen.items()) or "(nenhuma)"


def _examples_section(examples: Sequence[ExampleRecord]) -> str:
    """Each past brief as the lawyer's edits to the agent's draft, as a unified diff; one
    approved as drafted shows the approved text. Cut to MAX_EXAMPLE_CHARS each."""
    blocks = []
    for n, example in enumerate(examples, start=1):
        draft, final = example["draft"].strip(), example["final"].strip()
        if draft == final:
            body = f"Aprovado sem edição:\n{final}"
        else:
            diff = difflib.unified_diff(
                draft.splitlines(), final.splitlines(), "rascunho", "aprovado", n=1, lineterm=""
            )
            body = "Edições do advogado (- saiu do rascunho, + entrou na versão aprovada):\n"
            body += "\n".join(diff)
        if len(body) > MAX_EXAMPLE_CHARS:
            body = f"{body[:MAX_EXAMPLE_CHARS]} [truncado]"
        comment = f"\nComentário do advogado: {example['comment']}" if example["comment"] else ""
        blocks.append(f"Exemplo {n}\n{body}{comment}")
    return "\n\n".join(blocks)


def _summary(state: State, template: BriefTemplate) -> str:
    """What the lawyer should check, in Portuguese, from the state: no model writes it."""
    theses = state["theses"]
    grounded = _grounded(state)
    check = state["check"]
    lines = [
        f"Rascunho de {template.title.lower()} para conferência do advogado responsável.",
        f"Teses com fonte verificada: {len(grounded)} de {len(theses)}.",
    ]
    if check["valid"]:
        lines.append(f"Fontes citadas: {'; '.join(citation_label(i) for i in check['valid'])}.")
    kept = {t["id"] for t, _ in grounded}
    left_out = [t["title"] for t in theses if t["id"] not in kept]
    if left_out:
        lines.append(f"Teses sem fonte verificada, fora da peça: {'; '.join(left_out)}.")
    if check["removed"]:
        removed = "; ".join(
            f"{citation_label(r['reference'])} ({r['reason']})" for r in check["removed"]
        )
        lines.append(f"Trechos tirados por citação não verificada: {removed}.")
    critiques = state.get("critiques", [])
    if critiques and critiques[-1]["status"] == "failed":
        notes = "; ".join(f"{CRITERIA[k]}: {v}" for k, v in critiques[-1]["notes"].items())
        lines.append(f"Pontos da revisão automática a conferir: {notes}")
    elif critiques and critiques[-1]["status"] == "unavailable":
        lines.append("A revisão automática não respondeu no formato: revise o texto inteiro.")
    if state["facts"]["missing"]:
        lines.append(f"Faltou na descrição do caso: {'; '.join(state['facts']['missing'])}.")
    lines.append(
        "Complete os campos entre colchetes, como o valor da causa e a assinatura, antes de "
        "protocolar."
    )
    return "\n".join(lines)


def _record_run(span: Span, values: State) -> None:
    critiques = values.get("critiques", [])
    check = values.get("check")
    span.set_attribute("ementa.brief.theses", len(values.get("theses", [])))
    span.set_attribute("ementa.brief.grounded", len(_grounded(values)))
    span.set_attribute("ementa.brief.drafts", values.get("drafts", 0))
    span.set_attribute("ementa.brief.verdict", critiques[-1]["status"] if critiques else "none")
    if check is not None:
        span.set_attribute("ementa.brief.citations", check["valid"])
        span.set_attribute("ementa.brief.removed", len(check["removed"]))


def _changed_lines(outcome: BriefOutcome) -> int:
    """Lines the lawyer's edit took out of the draft or put in."""
    diff = difflib.ndiff(outcome.draft.splitlines(), (outcome.text or "").splitlines())
    return sum(1 for line in diff if line[:1] in "+-")
