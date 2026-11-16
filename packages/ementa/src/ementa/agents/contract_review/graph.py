"""`contract_review`: the firm's playbook over a consumer contract, clause by clause, with the
lawyer's approval before the report goes out.

parse_document -> split_clauses -> analyze_clause, one per clause and in parallel (`Send`) ->
aggregate_findings -> verify_citations -> draft_redlines -> human_review -> publish_report.

Each clause gets the playbook's deterministic rules first; then the criteria that need judgment
go to the `fast` model, which sees that clause only, pseudonymized. The reduce step builds the
risk matrix (clause, rule, severity, legal basis); the verifier keeps only the basis that exists
in the corpus; the redlines rewrite the flagged sentences with the firm's wording, or with the
model's for a criterion. The run stops at `human_review` until a lawyer approves, edits or
rejects the report. The graph is compiled with a checkpointer, so the decision may come from
another process: `Command(resume=ApprovalDecision(...).model_dump())` on the run's thread.
"""

import operator
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict, cast

import yaml
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send
from opentelemetry.trace import Span
from pydantic import BaseModel, Field, ValidationError

from ementa.agents.contract_review.report import (
    SourceText,
    corpus_texts,
    render_report,
    severity_counts,
    worst_by_clause,
)
from ementa.agents.contract_review.schemas import (
    Change,
    ClauseOut,
    ContractReport,
    Redline,
    ReviewRun,
    ReviewStatus,
    RiskRow,
)
from ementa.agents.legal_research import NOTICE
from ementa.documents import DocumentRejectedError, MediaType, Upload, extract_text
from ementa.domain.citations import verify_citations
from ementa.domain.contracts import (
    Clause,
    Edit,
    Playbook,
    RuleHit,
    Severity,
    apply_edits,
    check_clause,
    fold,
    split_contract,
)
from ementa.prompts import get_prompt
from ementa.tools.research import RemovedOut
from ementa_kit.context import Section, build_context
from ementa_kit.hitl import ApprovalDecision, ApprovalRequest, ask_for_approval
from ementa_kit.llm import BudgetExceededError, ModelRegistry, ModelRole
from ementa_kit.policy import DataClass
from ementa_kit.telemetry import get_logger, get_tracer

if TYPE_CHECKING:  # Presidio and spaCy take seconds to import; they load on the first review
    from ementa_kit.guardrails.pii import PiiGuard

AGENT_ID = "contract_review"
REPORT_KIND = "contract_report"
CLAUSE_PROMPT = ("contract_review.clause", 1)
QUESTION = "Quais critérios do playbook esta cláusula viola? Responda só com o JSON."
OUTSIDE_CORPUS = "fonte fora do corpus"
NO_BASIS = "achado sem fundamento conferido no corpus"
_FIX = (
    "A resposta anterior não é um JSON no formato pedido. "
    'Responda de novo só com o JSON: {"findings": [...]}.'
)

tracer = get_tracer(__name__)
log = get_logger(__name__)

Source = Literal["rule", "model"]


class ReviewNotFoundError(LookupError):
    """No review in this thread reached the lawyer's approval."""


class ReviewNotPendingError(RuntimeError):
    """The review is not waiting for a decision: a lawyer decided it already."""


class ReviewExistsError(ValueError):
    """The thread holds a review already; a new review needs a new thread."""


# State: plain records only, so any checkpointer stores them without custom types -----------


class ClauseRecord(TypedDict):
    number: str
    title: str
    text: str


class ClauseTask(TypedDict):
    """What one `analyze_clause` gets through `Send`."""

    clause: ClauseRecord
    contract: str  # the contract's title, pseudonymized
    masked: str  # the clause as the model sees it, pseudonymized
    mapping: dict[str, str]  # marker -> value for the whole contract
    rules_only: bool


class FindingRecord(TypedDict):
    clause: str
    rule: str  # rule or criterion id
    source: Source
    sentences: list[str]  # the clause's sentences it points at; empty: the clause as a whole
    note: str  # the model's reason; empty for a rule
    suggestion: str  # the wording for those sentences


class RowRecord(TypedDict):
    """A line of the risk matrix, as stored."""

    clause: str
    rule: str
    rule_title: str
    severity: str
    source: Source
    basis: list[str]
    why: str
    note: str
    sentences: list[str]
    suggestion: str


class ChangeRecord(TypedDict):
    before: str
    after: str


class RedlineRecord(TypedDict):
    clause: str
    rules: list[str]
    suggested: str
    changes: list[ChangeRecord]


class RemovedRecord(TypedDict):
    reference: str
    reason: str


class State(TypedDict, total=False):
    filename: str
    media_type: str
    content: bytes
    document_id: str | None
    requested_by: str | None
    rules_only: bool
    playbook: str  # id@version, fixed when the review starts
    prompt: str | None
    model: str | None
    text: str
    title: str
    clauses: list[ClauseRecord]
    contract: str
    masked: list[str]
    pii_mapping: dict[str, str]
    findings: Annotated[list[FindingRecord], operator.add]  # one batch per clause
    unreviewed: Annotated[list[str], operator.add]  # clauses the model did not judge
    matrix: list[RowRecord]
    removed: list[RemovedRecord]
    redlines: list[RedlineRecord]
    draft: str
    decision: dict[str, Any]
    status: str
    final: str | None


class _Reply(BaseModel):
    findings: list[dict[str, Any]]


class ModelFinding(BaseModel):
    """A criterion the model says the clause breaks, with the passage and a rewrite of it."""

    criterion: str
    excerpt: str = Field(default="", max_length=4000)
    rationale: str = Field(default="", max_length=2000)
    suggestion: str = Field(default="", max_length=4000)


def load_playbook(path: Path) -> Playbook:
    """The playbook in a YAML file; PlaybookError says what is wrong with it."""
    return Playbook.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


class ContractReviewAgent:
    """`review` runs a contract up to the lawyer's approval; `resume` applies the decision;
    `get` reads a review back.

    `guard` (the PII guard) and `sources` (the corpus texts) are factories, called on first use:
    spaCy and the corpus parse take seconds, and a rules-only review needs no guard.
    """

    def __init__(
        self,
        models: ModelRegistry,
        playbook: Playbook,
        checkpointer: BaseCheckpointSaver[Any],
        guard: Callable[[], "PiiGuard"],
        *,
        sources: Callable[[], Mapping[str, SourceText]] = corpus_texts,
    ) -> None:
        self.models = models
        self.playbook = playbook
        self._guard = guard
        self._sources = sources
        self.prompt = get_prompt(*CLAUSE_PROMPT)
        self.prompt_label = f"{self.prompt.id}@{self.prompt.version}"
        self._criteria = "\n".join(
            f"- {c.id}: {c.title}. {c.description}" for c in playbook.criteria
        )
        self.graph = self._build(checkpointer)

    # Nodes -------------------------------------------------------------------------------

    def parse_document(self, state: State) -> State:
        with tracer.start_as_current_span(
            "execute_tool parse_document",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "parse_document",
                "ementa.data_class": DataClass.RESTRICTED.value,
                "ementa.document.media_type": state["media_type"],
            },
        ) as span:
            text = extract_text(MediaType(state["media_type"]), state["content"])
            span.set_attribute("ementa.document.chars", len(text))
            span.set_attribute("ementa.tool.status", "finished")
        return {"text": text}

    def split_clauses(self, state: State) -> State:
        """The clauses and, unless only the rules run, each one pseudonymized for the model
        with one mapping for the whole contract: a name keeps its marker in every clause."""
        contract = split_contract(state["text"])
        if not contract.clauses:
            raise DocumentRejectedError("empty", "The document has no clause to review.")
        title = contract.title or state["filename"]
        masked: list[str] = []
        mapping: dict[str, str] = {}
        masked_title = ""
        if not state.get("rules_only"):
            guard = self._guard()
            hidden = guard.pseudonymize(title, mapping, stage="input")
            masked_title, mapping = hidden.text, hidden.mapping
            for clause in contract.clauses:
                hidden = guard.pseudonymize(f"{clause.label}\n{clause.text}", mapping)
                masked.append(hidden.text)
                mapping = hidden.mapping
        records = [
            ClauseRecord(number=c.number, title=c.title, text=c.text) for c in contract.clauses
        ]
        return {
            "title": title,
            "clauses": records,
            "contract": masked_title,
            "masked": masked,
            "pii_mapping": mapping,
        }

    def analyze_clause(self, task: ClauseTask) -> State:
        """The map step: the playbook's rules on the clause, then the criteria on the model."""
        record = task["clause"]
        clause = Clause(record["number"], record["title"], record["text"])
        with tracer.start_as_current_span(
            "execute_tool check_playbook_rule",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "check_playbook_rule",
                "ementa.data_class": DataClass.RESTRICTED.value,
                "ementa.contract.clause": clause.number,
            },
        ) as span:
            hits = check_clause(clause, self.playbook.rules)
            span.set_attribute("ementa.contract.rule_hits", len(hits))
            span.set_attribute("ementa.tool.status", "finished")
        findings = [
            FindingRecord(
                clause=clause.number,
                rule=hit.rule,
                source="rule",
                sentences=list(hit.sentences),
                note="",
                suggestion=self.playbook.item(hit.rule).redline,
            )
            for hit in hits
        ]
        if task["rules_only"] or not self.playbook.criteria:
            return {"findings": findings}
        judged = self._judge(clause, task, hits)
        if judged is None:
            return {"findings": findings, "unreviewed": [clause.number]}
        return {"findings": findings + judged}

    def aggregate_findings(self, state: State) -> State:
        """The reduce step: one row per clause and rule, in contract order, the most serious
        first within a clause and the rules before the model."""
        order = {c["number"]: n for n, c in enumerate(state["clauses"])}
        unique: dict[tuple[str, str], FindingRecord] = {}
        for finding in state.get("findings", []):
            unique.setdefault((finding["clause"], finding["rule"]), finding)
        rows: list[RowRecord] = []
        for finding in unique.values():
            item = self.playbook.item(finding["rule"])
            rows.append(
                RowRecord(
                    clause=finding["clause"],
                    rule=item.id,
                    rule_title=item.title,
                    severity=item.severity.value,
                    source=finding["source"],
                    basis=list(item.basis),
                    why=item.why,
                    note=finding["note"],
                    sentences=finding["sentences"],
                    suggestion=finding["suggestion"],
                )
            )
        rows.sort(
            key=lambda r: (
                order[r["clause"]],
                Severity(r["severity"]).rank,
                r["source"] != "rule",
                r["rule"],
            )
        )
        return {"matrix": rows}

    def check_citations(self, state: State) -> State:
        """Every basis must be a provision in the corpus, and the texts may cite nothing else:
        a sentence citing an id outside it, or an article in prose without its id, is removed.
        A row left without any basis is dropped. Both are listed in `removed`."""
        allowed = set(self._sources())
        kept: list[RowRecord] = []
        removed: list[RemovedRecord] = []
        with tracer.start_as_current_span(
            "guardrail citation_verifier",
            attributes={
                "gen_ai.operation.name": "guardrail",
                "ementa.guardrail.name": "citations",
                "ementa.guardrail.stage": "output",
            },
        ) as span:
            for row in state.get("matrix", []):
                outside = [i for i in row["basis"] if i not in allowed]
                removed += [RemovedRecord(reference=i, reason=OUTSIDE_CORPUS) for i in outside]
                why = verify_citations(row["why"], allowed)
                note = verify_citations(row["note"], allowed)
                suggestion = verify_citations(row["suggestion"], allowed)
                for result in (why, note, suggestion):
                    removed += [
                        RemovedRecord(reference=r.reference, reason=r.reason)
                        for r in result.removed
                    ]
                basis = [i for i in row["basis"] if i in allowed]
                if not basis:
                    reference = f"{row['rule']} na cláusula {row['clause']}"
                    removed.append(RemovedRecord(reference=reference, reason=NO_BASIS))
                    continue
                fixed = row.copy()
                fixed["basis"] = basis
                fixed["why"] = why.text
                fixed["note"] = note.text
                fixed["suggestion"] = suggestion.text
                kept.append(fixed)
            span.set_attribute("ementa.guardrail.decision", "modified" if removed else "pass")
            span.set_attribute("ementa.guardrail.removed", len(removed))
        return {"matrix": kept, "removed": removed}

    def draft_redlines(self, state: State) -> State:
        """The suggested rewrite of each flagged clause, then the report for the lawyer."""
        texts = {c["number"]: c["text"] for c in state["clauses"]}
        grouped: dict[str, list[RowRecord]] = {}
        for row in state.get("matrix", []):
            grouped.setdefault(row["clause"], []).append(row)
        redlines: list[RedlineRecord] = []
        for number, rows in grouped.items():
            edits: list[Edit] = []
            for row in rows:
                targets: list[str | None] = [*row["sentences"]] or [None]
                edits += [Edit(sentence, row["suggestion"]) for sentence in targets]
            redraft = apply_edits(texts[number], edits)
            redlines.append(
                RedlineRecord(
                    clause=number,
                    rules=[row["rule"] for row in rows],
                    suggested=redraft.text,
                    changes=[ChangeRecord(before=c.before, after=c.after) for c in redraft.changes],
                )
            )
        report = self._report({**state, "redlines": redlines})
        draft = render_report(
            title=report.title,
            filename=report.filename,
            playbook=report.playbook,
            prompt=report.prompt,
            model=report.model,
            criteria=[c.title for c in self.playbook.criteria],
            clauses=report.clauses,
            matrix=report.matrix,
            redlines=report.redlines,
            removed=report.removed,
            unreviewed=report.unreviewed,
            texts=self._sources(),
            notice=NOTICE,
        )
        return {"redlines": redlines, "draft": draft}

    def human_review(self, state: State) -> State:
        """Stops the run until a lawyer decides. When the run resumes, this node starts over
        and `ask_for_approval` returns the decision; nothing before it has side effects. An
        edit without the edited text decides nothing, so the lawyer is asked again."""
        request = ApprovalRequest(
            agent=AGENT_ID,
            kind=REPORT_KIND,
            title=f"Revisão de contrato: {state['title']}",
            draft=state["draft"],
            summary=_summary(state),
            data_class=DataClass.RESTRICTED,
        )
        decision = ask_for_approval(request)
        while decision.action == "edit" and not (decision.text or "").strip():
            decision = ask_for_approval(request)
        return {"decision": decision.model_dump(mode="json")}

    def publish_report(self, state: State) -> State:
        """The report as the lawyer decided: the draft when approved, the lawyer's text when
        edited, nothing when rejected. Sending it to the client is up to whoever called."""
        decision = ApprovalDecision.model_validate(state["decision"])
        if decision.action == "approve":
            return {"status": "approved", "final": state["draft"]}
        if decision.action == "edit":
            return {"status": "edited", "final": decision.text}
        return {"status": "rejected", "final": None}

    # The model ---------------------------------------------------------------------------

    def _judge(
        self, clause: Clause, task: ClauseTask, hits: Sequence[RuleHit]
    ) -> list[FindingRecord] | None:
        """The criteria the `fast` model finds broken in the pseudonymized clause, with the
        values back in its text; None when it gave no usable answer, after one more try."""
        from ementa_kit.guardrails.pii import rehydrate

        flagged = "\n".join(f"- {self.playbook.item(h.rule).title}" for h in hits) or "nenhum"
        sections = [
            Section("criterios", self._criteria),
            Section("contrato", task["contract"]),
            Section("ja_apontado", flagged),
            Section("clausula", task["masked"]),
        ]
        with tracer.start_as_current_span(
            "execute_tool classify_clause",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "classify_clause",
                "ementa.data_class": DataClass.RESTRICTED.value,
                "ementa.contract.clause": clause.number,
                "ementa.prompt.id": self.prompt.id,
                "ementa.prompt.version": self.prompt.version,
            },
        ) as span:
            request = build_context(self.prompt.text, sections, QUESTION)
            model = self.models.chat(ModelRole.FAST)
            try:
                reply = model.invoke(request)
                items = _parse(reply.text)
                if items is None:  # one more try: the reply stays, the correction goes after it
                    items = _parse(model.invoke([*request, reply, HumanMessage(_FIX)]).text)
            except BudgetExceededError:  # a spent budget stops the review, it is no failure
                raise
            except Exception as exc:  # the clause keeps its rule findings; the report says so
                log.warning("clause_review_failed", clause=clause.number, error=type(exc).__name__)
                span.set_attribute("ementa.tool.status", "failed")
                return None
            if items is None:
                log.warning("clause_review_rejected", clause=clause.number, prompt=self.prompt.id)
                span.set_attribute("ementa.tool.status", "failed")
                return None
            criteria = {c.id: c for c in self.playbook.criteria}
            mapping = task["mapping"]
            findings: list[FindingRecord] = []
            for item in items:
                try:
                    found = ModelFinding.model_validate(item)
                except ValidationError:
                    continue  # one bad item does not cost the others
                criterion = criteria.get(found.criterion)
                if criterion is None or any(f["rule"] == criterion.id for f in findings):
                    continue  # unknown or repeated criterion
                suggestion = rehydrate(found.suggestion, mapping).strip()
                findings.append(
                    FindingRecord(
                        clause=clause.number,
                        rule=criterion.id,
                        source="model",
                        sentences=_sentences_with(clause, rehydrate(found.excerpt, mapping)),
                        note=rehydrate(found.rationale, mapping).strip(),
                        suggestion=suggestion or criterion.redline,
                    )
                )
            span.set_attribute("ementa.contract.model_findings", len(findings))
            span.set_attribute("ementa.tool.status", "finished")
        return findings

    # Edges -------------------------------------------------------------------------------

    @staticmethod
    def _fan_out(state: State) -> list[Send]:
        clauses = state["clauses"]
        masked = state.get("masked") or [""] * len(clauses)
        return [
            Send(
                "analyze_clause",
                ClauseTask(
                    clause=clause,
                    contract=state.get("contract", ""),
                    masked=text,
                    mapping=state.get("pii_mapping", {}),
                    rules_only=state.get("rules_only", False),
                ),
            )
            for clause, text in zip(clauses, masked, strict=True)
        ]

    def _build(
        self, checkpointer: BaseCheckpointSaver[Any]
    ) -> CompiledStateGraph[Any, Any, Any, Any]:
        graph = StateGraph(State)
        graph.add_node("parse_document", self.parse_document)
        graph.add_node("split_clauses", self.split_clauses)
        graph.add_node("analyze_clause", self.analyze_clause)  # type: ignore[arg-type]
        graph.add_node("aggregate_findings", self.aggregate_findings)
        graph.add_node("verify_citations", self.check_citations)
        graph.add_node("draft_redlines", self.draft_redlines)
        graph.add_node("human_review", self.human_review)
        graph.add_node("publish_report", self.publish_report)
        graph.add_edge(START, "parse_document")
        graph.add_edge("parse_document", "split_clauses")
        graph.add_conditional_edges("split_clauses", self._fan_out, ["analyze_clause"])
        graph.add_edge("analyze_clause", "aggregate_findings")
        graph.add_edge("aggregate_findings", "verify_citations")
        graph.add_edge("verify_citations", "draft_redlines")
        graph.add_edge("draft_redlines", "human_review")
        graph.add_edge("human_review", "publish_report")
        graph.add_edge("publish_report", END)
        return graph.compile(checkpointer=checkpointer, name=AGENT_ID)

    # Entry points ------------------------------------------------------------------------

    def inputs(
        self,
        content: bytes | str,
        filename: str,
        *,
        media_type: str | None = None,
        document_id: str | None = None,
        requested_by: str | None = None,
        rules_only: bool = False,
    ) -> State:
        """The graph's input for a review of `content`, checked as `review` checks it: a file
        out of the accepted types raises DocumentRejectedError, and unless `rules_only`, a model
        without a key raises ModelUnavailableError."""
        data = content.encode("utf-8") if isinstance(content, str) else content
        upload = Upload.accept(filename, media_type, data)
        if not rules_only:
            self.models.chat(ModelRole.FAST)
        return {
            "filename": upload.filename,
            "media_type": upload.media_type.value,
            "content": upload.content,
            "document_id": document_id,
            "requested_by": requested_by,
            "rules_only": rules_only,
            "playbook": self.playbook.label,
            "prompt": None if rules_only else self.prompt_label,
            "model": None if rules_only else self.models.spec(ModelRole.FAST).model,
        }

    def report(self, values: Mapping[str, Any]) -> ContractReport:
        """The report in a review's state, as `get` builds it."""
        return self._report(cast(State, values))

    def review(
        self,
        content: bytes | str,
        filename: str,
        *,
        media_type: str | None = None,
        document_id: str | None = None,
        requested_by: str | None = None,
        thread_id: str | None = None,
        rules_only: bool = False,
    ) -> ReviewRun:
        """Review a contract up to the lawyer's approval; the run comes back `pending`.

        `content` is the file as sent: Markdown, text or a PDF with a text layer, up to 2 MiB;
        anything else raises DocumentRejectedError. Unless `rules_only`, a model without a key
        raises ModelUnavailableError here, before anything runs. `thread_id` names the run for
        `resume`; a thread that holds a review already raises ReviewExistsError.
        """
        thread = thread_id or str(uuid.uuid4())
        inputs = self.inputs(
            content,
            filename,
            media_type=media_type,
            document_id=document_id,
            requested_by=requested_by,
            rules_only=rules_only,
        )
        config = _config(thread)
        if self.graph.get_state(config).values:
            raise ReviewExistsError(thread)
        with self._span(thread, rules_only) as span:
            self.graph.invoke(inputs, config)
            run = self.get(thread)
            _record(span, run)
        return run

    def resume(self, thread_id: str, decision: ApprovalDecision) -> ReviewRun:
        """Apply the lawyer's decision to a pending review. An edit needs the edited text."""
        config = _config(thread_id)
        snapshot = self.graph.get_state(config)
        if not snapshot.values:
            raise ReviewNotFoundError(thread_id)
        if not snapshot.interrupts:
            raise ReviewNotPendingError(thread_id)
        if decision.action == "edit" and not (decision.text or "").strip():
            raise ValueError("an edit carries the edited text")
        rules_only = bool(snapshot.values.get("rules_only"))
        with self._span(thread_id, rules_only) as span:
            span.set_attribute("ementa.hitl.action", decision.action)
            self.graph.invoke(Command(resume=decision.model_dump(mode="json")), config)
            run = self.get(thread_id)
            _record(span, run)
        return run

    def get(self, thread_id: str) -> ReviewRun:
        """The review in `thread_id` as it stands; ReviewNotFoundError when there is none that
        reached the lawyer's approval."""
        snapshot = self.graph.get_state(_config(thread_id))
        values = cast(State, snapshot.values)
        if not values or "draft" not in values:
            raise ReviewNotFoundError(thread_id)
        request = next((ApprovalRequest.model_validate(i.value) for i in snapshot.interrupts), None)
        raw = values.get("decision")
        return ReviewRun(
            thread_id=thread_id,
            status=cast(ReviewStatus, values.get("status") or "pending"),
            requested_by=values.get("requested_by"),
            report=self._report(values),
            request=request,
            decision=ApprovalDecision.model_validate(raw) if raw else None,
            final=values.get("final"),
        )

    def _report(self, values: State) -> ContractReport:
        clauses = values["clauses"]
        titles = {c["number"]: c["title"] for c in clauses}
        texts = {c["number"]: c["text"] for c in clauses}
        order = {c["number"]: n for n, c in enumerate(clauses)}
        unreviewed = sorted(set(values.get("unreviewed", [])), key=lambda n: order.get(n, 0))
        return ContractReport(
            title=values["title"],
            filename=values["filename"],
            document_id=values.get("document_id"),
            playbook=values.get("playbook", self.playbook.label),
            prompt=values.get("prompt"),
            model=values.get("model"),
            rules_only=values.get("rules_only", False),
            clauses=[ClauseOut(number=c["number"], title=c["title"]) for c in clauses],
            matrix=[
                RiskRow(
                    clause=row["clause"],
                    clause_title=titles.get(row["clause"], ""),
                    rule=row["rule"],
                    rule_title=row["rule_title"],
                    severity=Severity(row["severity"]),
                    basis=row["basis"],
                    source=row["source"],
                    why=row["why"],
                    note=row["note"],
                    excerpt=row["sentences"],
                )
                for row in values.get("matrix", [])
            ],
            redlines=[
                Redline(
                    clause=redline["clause"],
                    clause_title=titles.get(redline["clause"], ""),
                    rules=redline["rules"],
                    original=texts.get(redline["clause"], ""),
                    suggested=redline["suggested"],
                    changes=[Change(**change) for change in redline["changes"]],
                )
                for redline in values.get("redlines", [])
            ],
            removed=[RemovedOut(**r) for r in values.get("removed", [])],
            unreviewed=unreviewed,
            draft=values.get("draft", ""),
        )

    @contextmanager
    def _span(self, thread_id: str, rules_only: bool) -> Iterator[Span]:
        attributes: dict[str, str | int | bool] = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": AGENT_ID,
            "gen_ai.conversation.id": thread_id,
            "ementa.data_class": DataClass.RESTRICTED.value,
            "ementa.playbook.id": self.playbook.id,
            "ementa.playbook.version": self.playbook.version,
            "ementa.contract.rules_only": rules_only,
        }
        if not rules_only:
            spec = self.models.spec(ModelRole.FAST)
            attributes |= {
                "gen_ai.provider.name": spec.provider,
                "gen_ai.request.model": spec.model,
                "ementa.prompt.id": self.prompt.id,
                "ementa.prompt.version": self.prompt.version,
            }
        with tracer.start_as_current_span(
            f"invoke_agent {AGENT_ID}", attributes=attributes
        ) as span:
            yield span


def _parse(text: str) -> list[dict[str, Any]] | None:
    raw = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return _Reply.model_validate_json(raw).findings
    except ValidationError:
        return None


def _sentences_with(clause: Clause, excerpt: str) -> list[str]:
    """The clause's sentences the model's excerpt points at, compared without accents, case or
    extra spaces; none when the excerpt is not in the clause, and the rewrite is then added."""
    wanted = " ".join(fold(excerpt).split())
    if not wanted:
        return []
    found: list[str] = []
    for sentence in clause.sentences():
        plain = " ".join(fold(sentence).split())
        if wanted in plain or (len(plain) >= 20 and plain in wanted):
            found.append(sentence)
    return found


def _summary(state: State) -> str:
    """Why the run stopped and what to check, for the approval inbox."""
    worst = worst_by_clause(
        (row["clause"], Severity(row["severity"])) for row in state.get("matrix", [])
    )
    total = len(state["clauses"])
    parts = [
        f"{len(worst)} de {total} cláusulas com risco ({severity_counts(worst)})."
        if worst
        else f"Nenhuma das {total} cláusulas violou o playbook."
    ]
    if state.get("rules_only"):
        parts.append("Só as regras rodaram; os critérios do modelo não foram avaliados.")
    unreviewed = sorted(set(state.get("unreviewed", [])))
    if unreviewed:
        parts.append(f"O modelo não avaliou as cláusulas {', '.join(unreviewed)}.")
    parts.append("Confira a matriz e as redações sugeridas antes de enviar o relatório.")
    return " ".join(parts)


def _record(span: Span, run: ReviewRun) -> None:
    report = run.report
    span.set_attribute("ementa.contract.clauses", len(report.clauses))
    span.set_attribute("ementa.contract.findings", len(report.matrix))
    span.set_attribute("ementa.contract.flagged", len({r.clause for r in report.matrix}))
    for severity in Severity:
        count = sum(r.severity is severity for r in report.matrix)
        span.set_attribute(f"ementa.contract.findings.{severity.value}", count)
    span.set_attribute("ementa.contract.unreviewed", len(report.unreviewed))
    span.set_attribute("ementa.guardrail.removed", len(report.removed))
    span.set_attribute("ementa.hitl.status", run.status)


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}
