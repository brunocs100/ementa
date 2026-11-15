"""The `contract_review` agent with scripted models: map-reduce over clauses, the approval pause
and its resumption, the risk matrix, the redlines, the citation check and the traces.

Every contract, name and CPF here is fictitious. No test calls a real model.
"""

import json
import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import Field, PrivateAttr

from ementa.agents.contract_review import (
    ContractReviewAgent,
    ReviewExistsError,
    ReviewNotFoundError,
    ReviewNotPendingError,
    load_playbook,
)
from ementa.agents.contract_review import graph as review_graph
from ementa.agents.contract_review.graph import NO_BASIS, OUTSIDE_CORPUS
from ementa.agents.contract_review.report import corpus_texts, source_label
from ementa.documents import DocumentRejectedError
from ementa.domain.citations import verify_citations
from ementa.domain.contracts import Playbook, Severity
from ementa.pii import pii_guard
from ementa.services import CONTRACT_PLAYBOOK
from ementa_kit.config import Settings
from ementa_kit.context import assert_append_only
from ementa_kit.hitl import ApprovalDecision
from ementa_kit.llm import BudgetExceededError, ModelRegistry, ModelSpec, ModelUnavailableError
from ementa_kit.llm.budget import BudgetCap, BudgetWindow
from ementa_kit.policy import DataClass

CONTRACTS = CONTRACT_PLAYBOOK.parents[1] / "contracts"

CONTRACT = """# Contrato de academia de teste

> Contrato fictício, para os testes.

## Cláusula 1ª - Do objeto

1.1 Acesso à musculação na unidade da CONTRATADA.

## Cláusula 2ª - Do atraso

2.1 As prestações vencem no dia 10.

2.2 Em caso de atraso, incide multa de 10% (dez por cento) sobre a prestação.

2.3 O CONTRATANTE em atraso paga as despesas de cobrança.

## Cláusula 3ª - Do cancelamento

3.1 O CONTRATANTE que cancelar paga multa de 50% (cinquenta por cento) das prestações que faltam.

## Cláusula 4ª - Do foro

4.1 Fica eleito o foro do domicílio do CONTRATANTE.
"""

EXCESSIVE = (
    "3.1 O CONTRATANTE que cancelar paga multa de 50% (cinquenta por cento) das prestações "
    "que faltam."
)
FAIR = "3.1 O CONTRATANTE que cancelar paga multa de 10% das prestações que faltam."


def finding(criterion: str, excerpt: str, rationale: str, suggestion: str = "") -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "criterion": criterion,
                    "excerpt": excerpt,
                    "rationale": rationale,
                    "suggestion": suggestion,
                }
            ]
        },
        ensure_ascii=False,
    )


class ClauseJudge(BaseChatModel):
    """Answers each clause from a script keyed by the clause number, in whatever order the
    parallel `Send` tasks call it, and records every request (a deep copy)."""

    script: dict[str, list[str]] = Field(default_factory=dict)
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    error: str | None = None  # a clause whose call raises
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def _llm_type(self) -> str:
        return "clause-judge"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        match = re.search(r"<clausula>\nCláusula (\S+)", messages[1].text)
        number = match.group(1) if match else ""
        with self._lock:
            self.calls.append([m.model_copy(deep=True) for m in messages])
            if number == self.error:
                raise ConnectionError("provider down")
            replies = self.script.get(number, [])
            reply = replies.pop(0) if replies else '{"findings": []}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(reply))])

    def calls_for(self, number: str) -> list[list[BaseMessage]]:
        return [c for c in self.calls if f"<clausula>\nCláusula {number} " in c[1].text]


def make_agent(
    judge: BaseChatModel | None = None,
    *,
    saver: InMemorySaver | None = None,
    playbook: Playbook | None = None,
    guard: Callable[[], Any] = pii_guard,
) -> ContractReviewAgent:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    def factory(spec: ModelSpec) -> BaseChatModel:
        if judge is None:
            raise AssertionError("a rules-only review calls no model")
        return judge

    return ContractReviewAgent(
        ModelRegistry(settings, factory=factory),
        playbook or load_playbook(CONTRACT_PLAYBOOK),
        saver or InMemorySaver(),
        guard,
    )


def no_guard() -> Any:
    raise AssertionError("a rules-only review needs no guard")


def approve(**fields: Any) -> ApprovalDecision:
    return ApprovalDecision(action="approve", decided_by="socia", **fields)


# The playbook and the corpus ---------------------------------------------------------------


def test_every_basis_of_the_playbook_is_a_provision_of_the_corpus() -> None:
    playbook = load_playbook(CONTRACT_PLAYBOOK)
    texts = corpus_texts()

    assert playbook.basis_ids() <= set(texts)
    for item in playbook.items:
        cited = set(re.findall(r"\[([a-z]+(?::[A-Za-z0-9-]+)+)\]", item.why))
        assert cited == set(item.basis), item.id  # the reason cites exactly its basis
        assert not verify_citations(item.why, texts).removed, item.id
        assert not verify_citations(item.redline, texts).removed, item.id  # the firm's wording


def test_corpus_texts_quote_the_law_with_the_lead_in_of_an_inciso() -> None:
    texts = corpus_texts()

    assert texts["cdc:art51:IV"].label == "CDC, art. 51, IV"
    assert texts["cdc:art51:IV"].text.startswith("São nulas de pleno direito")
    assert "IV - estabeleçam obrigações consideradas iníquas" in texts["cdc:art51:IV"].text
    assert texts["stj:sumula:302"].label == "Súmula 302 do STJ"
    assert "cdc:art51:V" not in texts  # vetoed: nothing to cite


@pytest.mark.parametrize(
    ("source_id", "label"),
    [
        ("cdc:art52:p1", "CDC, art. 52, § 1º"),
        ("cdc:art49:pu", "CDC, art. 49, parágrafo único"),
        ("cdc:art6:VIII", "CDC, art. 6º, VIII"),
        ("cdc:art51:p1:III", "CDC, art. 51, § 1º, III"),
    ],
)
def test_source_labels(source_id: str, label: str) -> None:
    assert source_label(source_id) == label


# Map-reduce --------------------------------------------------------------------------------


def test_each_clause_gets_the_rules_and_one_model_call_of_its_own() -> None:
    judge = ClauseJudge(
        script={
            "3": [
                finding(
                    "desvantagem-exagerada",
                    "multa de 50% (cinquenta por cento) das prestações que faltam",
                    "A multa cobra metade de tudo o que falta.",
                    FAIR,
                )
            ]
        }
    )
    agent = make_agent(judge)

    run = agent.review(CONTRACT, "academia.md")

    assert len(judge.calls) == 4  # one call per clause, in parallel
    for call in judge.calls:
        assert call[1].text.count("<clausula>") == 1  # each call sees one clause only
    assert [(r.clause, r.rule, r.source) for r in run.report.matrix] == [
        ("2", "custos-de-cobranca", "rule"),
        ("2", "multa-de-mora", "rule"),
        ("3", "desvantagem-exagerada", "model"),
    ]
    [clause_two] = judge.calls_for("2")
    assert "- Põe no consumidor os custos da cobrança" in clause_two[1].text  # ja_apontado
    excessive = run.report.matrix[2]
    assert excessive.severity is Severity.HIGH
    assert excessive.basis == ["cdc:art51:IV", "cdc:art51:p1:III"]
    assert excessive.note == "A multa cobra metade de tudo o que falta."
    assert excessive.excerpt == [EXCESSIVE]
    assert run.report.prompt == "contract_review.clause@1"
    assert run.report.unreviewed == []


def test_the_model_sees_the_clauses_pseudonymized_and_its_text_comes_back_with_the_values() -> None:
    contract = (
        "# Contrato de teste\n\n## Cláusula 1ª - Das comunicações\n\n"
        "1.1 Os avisos vão para Mariana Lopes Teixeira, CPF 123.456.789-09, pelo aplicativo.\n"
        "1.2 A CONTRATADA pode mudar as regras de aviso quando quiser.\n"
    )
    judge = ClauseJudge(
        script={
            "1": [
                finding(
                    "redacao-obscura",
                    "pode mudar as regras de aviso quando quiser",
                    "[PESSOA_1] não sabe que regras valem.",
                    "As regras de aviso a [PESSOA_1] ficam escritas neste contrato.",
                )
            ]
        }
    )

    run = make_agent(judge).review(contract, "teste.md")

    [call] = judge.calls
    seen = "\n".join(m.text for m in call)
    assert "123.456.789-09" not in seen and "Mariana" not in seen
    assert "[CPF_1]" in seen and "[PESSOA_1]" in seen
    [row] = run.report.matrix
    assert row.note == "Mariana Lopes Teixeira não sabe que regras valem."
    [redline] = run.report.redlines
    assert redline.changes[0].after == (
        "1.2 As regras de aviso a Mariana Lopes Teixeira ficam escritas neste contrato."
    )


# Approval ----------------------------------------------------------------------------------


def test_the_review_stops_for_the_lawyer_before_it_finalizes() -> None:
    agent = make_agent(ClauseJudge())

    run = agent.review(CONTRACT, "academia.md", requested_by="associado")

    assert run.status == "pending" and run.final is None and run.decision is None
    assert run.requested_by == "associado"
    request = run.request
    assert request is not None
    assert (request.agent, request.kind) == ("contract_review", "contract_report")
    assert request.data_class is DataClass.RESTRICTED
    assert request.title == "Revisão de contrato: Contrato de academia de teste"
    assert request.draft == run.report.draft
    assert "1 de 4 cláusulas com risco (gravidade: 1 média)" in request.summary
    assert agent.graph.get_state({"configurable": {"thread_id": run.thread_id}}).next == (
        "human_review",
    )


def test_approve_publishes_the_draft() -> None:
    agent = make_agent(ClauseJudge())
    pending = agent.review(CONTRACT, "academia.md")

    run = agent.resume(pending.thread_id, approve(comment="Pode enviar."))

    assert run.status == "approved"
    assert run.final == pending.report.draft
    assert run.decision is not None and run.decision.comment == "Pode enviar."
    assert run.request is None


def test_edit_publishes_the_lawyers_text() -> None:
    agent = make_agent(ClauseJudge())
    pending = agent.review(CONTRACT, "academia.md")
    edited = pending.report.draft.replace("Revisão de contrato", "Parecer sobre o contrato")

    run = agent.resume(
        pending.thread_id, ApprovalDecision(action="edit", text=edited, decided_by="socia")
    )

    assert run.status == "edited"
    assert run.final == edited
    assert run.report.draft == pending.report.draft  # the draft stays as it was


def test_reject_publishes_nothing() -> None:
    agent = make_agent(ClauseJudge())
    pending = agent.review(CONTRACT, "academia.md")

    run = agent.resume(
        pending.thread_id,
        ApprovalDecision(action="reject", comment="Refazer.", decided_by="socia"),
    )

    assert run.status == "rejected"
    assert run.final is None
    assert run.report.matrix == pending.report.matrix


def test_the_approvals_api_resumes_the_graph_with_a_decision_dump() -> None:
    """What the approvals API does: `Command(resume=decision.model_dump())` on the thread, from
    another agent built on the same checkpointer, as after a restart."""
    saver = InMemorySaver()
    pending = make_agent(ClauseJudge(), saver=saver).review(CONTRACT, "academia.md")
    other = make_agent(ClauseJudge(), saver=saver)
    config: RunnableConfig = {"configurable": {"thread_id": pending.thread_id}}

    other.graph.invoke(Command(resume=approve().model_dump()), config)

    run = other.get(pending.thread_id)
    assert run.status == "approved" and run.final == pending.report.draft


def test_an_edit_without_text_decides_nothing_and_the_lawyer_is_asked_again() -> None:
    agent = make_agent(ClauseJudge())
    pending = agent.review(CONTRACT, "academia.md")
    config: RunnableConfig = {"configurable": {"thread_id": pending.thread_id}}
    empty = ApprovalDecision(action="edit", text="  ", decided_by="socia")

    with pytest.raises(ValueError, match="edited text"):
        agent.resume(pending.thread_id, empty)
    agent.graph.invoke(Command(resume=empty.model_dump()), config)  # past the check

    assert agent.get(pending.thread_id).status == "pending"
    assert agent.resume(pending.thread_id, approve()).status == "approved"


def test_resume_needs_a_pending_review() -> None:
    agent = make_agent(ClauseJudge())
    pending = agent.review(CONTRACT, "academia.md")
    agent.resume(pending.thread_id, approve())

    with pytest.raises(ReviewNotPendingError):
        agent.resume(pending.thread_id, approve())
    with pytest.raises(ReviewNotFoundError):
        agent.resume("no-such-thread", approve())
    with pytest.raises(ReviewNotFoundError):
        agent.get("no-such-thread")
    with pytest.raises(ReviewExistsError):
        agent.review(CONTRACT, "academia.md", thread_id=pending.thread_id)


# Risk matrix and redlines -----------------------------------------------------------------


def test_the_risk_matrix_carries_severity_basis_and_origin() -> None:
    run = make_agent(guard=no_guard).review(CONTRACT, "academia.md", rules_only=True)

    rows = {r.rule: r for r in run.report.matrix}
    assert set(rows) == {"custos-de-cobranca", "multa-de-mora"}
    late = rows["multa-de-mora"]
    assert (late.clause, late.clause_title, late.severity) == ("2", "Do atraso", Severity.MEDIUM)
    assert late.basis == ["cdc:art52:p1", "cdc:art52"]
    assert late.source == "rule"
    assert late.excerpt == [
        "2.2 Em caso de atraso, incide multa de 10% (dez por cento) sobre a prestação."
    ]
    assert "[cdc:art52:p1]" in late.why
    draft = run.report.draft
    assert "| 2 \u2013 Do atraso | Multa de mora acima de 2% | média |" in draft
    assert "CDC, art. 52, § 1º [cdc:art52:p1]" in draft
    assert "- [cdc:art52:p1] CDC, art. 52, § 1º: As multas de mora decorrentes" in draft


def test_redlines_rewrite_the_flagged_sentences_with_the_firms_wording() -> None:
    run = make_agent(guard=no_guard).review(CONTRACT, "academia.md", rules_only=True)

    [redline] = run.report.redlines
    assert redline.clause == "2"
    assert redline.rules == ["custos-de-cobranca", "multa-de-mora"]
    assert redline.suggested == (
        "2.1 As prestações vencem no dia 10.\n\n"
        "2.2 Em caso de atraso, incide multa de mora de 2% (dois por cento) do valor da "
        "prestação."
    )
    assert [(c.before[:3], c.after[:3]) for c in redline.changes] == [
        ("2.2", "2.2"),
        ("2.3", ""),  # the costs of collection go: the playbook's wording is to remove them
    ]
    assert "Trecho a suprimir:" in run.report.draft


def test_the_models_suggestion_is_the_redline_of_a_criterion_or_the_firms_wording() -> None:
    judge = ClauseJudge(
        script={
            "3": [finding("desvantagem-exagerada", "multa de 50%", "Excessiva.", FAIR)],
            "1": [finding("venda-casada", "trecho que não está na cláusula", "Casada.")],
        }
    )

    run = make_agent(judge).review(CONTRACT, "academia.md")

    redlines = {r.clause: r for r in run.report.redlines}
    assert redlines["3"].suggested == FAIR
    playbook = load_playbook(CONTRACT_PLAYBOOK)
    tying = playbook.item("venda-casada").redline
    assert redlines["1"].suggested.endswith(f"\n\n{tying}")  # no excerpt found: it is added
    assert redlines["1"].changes[0].before == ""


# Citations ---------------------------------------------------------------------------------


def test_the_verifier_takes_out_citations_and_findings_without_a_source_in_the_corpus() -> None:
    playbook = load_playbook(CONTRACT_PLAYBOOK)
    data = {
        "id": "teste",
        "version": 1,
        "title": "Teste",
        "scope": "Teste",
        "rules": [
            {
                "id": "foro-inventado",
                "title": "Regra com fonte que não existe",
                "severity": "alta",
                "basis": ["cdc:art999"],
                "why": "Regra inventada [cdc:art999].",
                "redline": "",
                "match": {"any": ["foro"]},
            }
        ],
        "criteria": [
            {
                "id": c.id,
                "title": c.title,
                "severity": c.severity.value,
                "basis": list(c.basis),
                "why": c.why,
                "description": c.description,
                "redline": c.redline,
            }
            for c in playbook.criteria
        ],
    }
    judge = ClauseJudge(
        script={
            "3": [
                finding(
                    "desvantagem-exagerada",
                    "multa de 50%",
                    "Viola o art. 51 do CDC. A multa é alta demais.",
                    f"{FAIR} Conforme a Súmula 999 [stj:sumula:999].",
                )
            ]
        }
    )

    run = make_agent(judge, playbook=Playbook.from_dict(data)).review(CONTRACT, "academia.md")

    assert [(r.clause, r.rule) for r in run.report.matrix] == [("3", "desvantagem-exagerada")]
    row = run.report.matrix[0]
    assert row.note == "A multa é alta demais."  # the sentence citing art. 51 in prose went
    assert run.report.redlines[0].suggested == FAIR
    removed = [(r.reference, r.reason) for r in run.report.removed]
    assert ("cdc:art999", OUTSIDE_CORPUS) in removed
    assert ("foro-inventado na cláusula 4", NO_BASIS) in removed
    assert ("art. 51", "menção sem id de fonte verificado") in removed
    assert ("stj:sumula:999", "fonte fora do contexto recuperado") in removed
    assert "## Removido pelo verificador" in run.report.draft


# Failures ----------------------------------------------------------------------------------


def test_a_clause_the_model_cannot_judge_keeps_its_rules_and_is_reported() -> None:
    judge = ClauseJudge(script={"2": ["não sei", "ainda não sei"]}, error="3")

    run = make_agent(judge).review(CONTRACT, "academia.md")

    assert run.report.unreviewed == ["2", "3"]
    assert {r.rule for r in run.report.matrix} == {"custos-de-cobranca", "multa-de-mora"}
    first, retry = judge.calls_for("2")
    assert_append_only([first, retry])  # the correction goes after the bad reply
    assert "não é um JSON no formato pedido" in retry[-1].text
    assert "O modelo não avaliou as cláusulas 2, 3" in run.report.draft
    assert run.request is not None and "O modelo não avaliou as cláusulas 2, 3." in (
        run.request.summary
    )


def test_a_spent_budget_stops_the_review() -> None:
    spent = BudgetExceededError(
        BudgetCap(BudgetWindow.DAY, 2.0), "dev", 2.1, datetime(2026, 11, 16, tzinfo=UTC)
    )

    class SpentJudge(ClauseJudge):
        def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
            raise spent

    with pytest.raises(BudgetExceededError):
        make_agent(SpentJudge()).review(CONTRACT, "academia.md")


def test_rules_only_calls_no_model_and_says_what_was_left_out() -> None:
    run = make_agent(guard=no_guard).review(CONTRACT, "academia.md", rules_only=True)

    assert run.report.rules_only and run.report.prompt is None and run.report.model is None
    assert "Só as regras do playbook rodaram" in run.report.draft
    assert "Obrigação desproporcional para o consumidor" in run.report.draft
    assert run.request is not None and "Só as regras rodaram" in run.request.summary


def test_without_a_key_the_review_fails_before_it_starts() -> None:
    def keyless(spec: ModelSpec) -> BaseChatModel:
        raise ModelUnavailableError("OPENAI_API_KEY is not set")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    agent = ContractReviewAgent(
        ModelRegistry(settings, factory=keyless),
        load_playbook(CONTRACT_PLAYBOOK),
        InMemorySaver(),
        no_guard,
    )

    with pytest.raises(ModelUnavailableError):
        agent.review(CONTRACT, "academia.md")


@pytest.mark.parametrize(
    ("filename", "content", "reason"),
    [
        ("contrato.docx", b"PK\x03\x04", "unsupported"),
        ("contrato.md", b"", "empty"),
        ("contrato.pdf", b"%PDF-1.4\nlixo", "unreadable"),
    ],
)
def test_a_file_that_is_not_a_contract_is_refused(
    filename: str, content: bytes, reason: str
) -> None:
    with pytest.raises(DocumentRejectedError) as error:
        make_agent(guard=no_guard).review(content, filename, rules_only=True)

    assert error.value.reason == reason


# PDF ---------------------------------------------------------------------------------------


def pdf_of(text: str) -> bytes:
    """A one-page PDF with each line of `text` in Helvetica, WinAnsi encoded, built by hand."""

    def show(line: str) -> bytes:
        raw = line.encode("cp1252")
        raw = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        return b"(" + raw + b") Tj T*"

    lines = b" ".join(show(line) for line in text.split("\n"))
    stream = b"BT /F1 8 Tf 20 820 Td 10 TL " + lines + b" ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%b\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%b\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_a_pdf_contract_gets_the_same_review_as_its_text() -> None:
    text = (CONTRACTS / "curso-02.txt").read_text(encoding="utf-8")
    agent = make_agent(guard=no_guard)

    from_text = agent.review(text, "curso-02.txt", rules_only=True)
    from_pdf = agent.review(pdf_of(text), "curso-02.pdf", rules_only=True)

    assert [c.number for c in from_pdf.report.clauses] == [
        c.number for c in from_text.report.clauses
    ]
    pairs = [(r.clause, r.rule) for r in from_text.report.matrix]
    assert pairs == [
        ("3", "preco-unilateral"),
        ("6", "onus-da-prova"),
        ("8", "acesso-ao-judiciario"),
    ]
    assert [(r.clause, r.rule) for r in from_pdf.report.matrix] == pairs


# Traces ------------------------------------------------------------------------------------


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(review_graph, "tracer", provider.get_tracer("test"))
    yield exporter


def children(spans: list[ReadableSpan], parent: ReadableSpan) -> list[str]:
    parent_id = parent.context.span_id if parent.context else None
    return sorted(s.name for s in spans if s.parent and s.parent.span_id == parent_id)


def test_the_review_and_its_resumption_are_traced(spans: InMemorySpanExporter) -> None:
    judge = ClauseJudge(script={"3": [finding("desvantagem-exagerada", "multa de 50%", "Alta.")]})
    agent = make_agent(judge)
    pending = agent.review(CONTRACT, "academia.md")
    agent.resume(pending.thread_id, approve())

    finished = list(spans.get_finished_spans())
    review, resumed = [s for s in finished if s.name == "invoke_agent contract_review"]
    assert children(finished, review) == [
        "execute_tool check_playbook_rule",
        "execute_tool check_playbook_rule",
        "execute_tool check_playbook_rule",
        "execute_tool check_playbook_rule",
        "execute_tool classify_clause",
        "execute_tool classify_clause",
        "execute_tool classify_clause",
        "execute_tool classify_clause",
        "execute_tool parse_document",
        "guardrail citation_verifier",
    ]
    attributes = dict(review.attributes or {})
    assert attributes["gen_ai.agent.name"] == "contract_review"
    assert attributes["gen_ai.conversation.id"] == pending.thread_id
    assert attributes["ementa.prompt.id"] == "contract_review.clause"
    assert attributes["ementa.playbook.id"] == "consumer-services"
    assert attributes["ementa.contract.clauses"] == 4
    assert attributes["ementa.contract.findings"] == 3
    assert attributes["ementa.contract.findings.alta"] == 1
    assert attributes["ementa.hitl.status"] == "pending"
    resumed_attributes = dict(resumed.attributes or {})
    assert resumed_attributes["ementa.hitl.action"] == "approve"
    assert resumed_attributes["ementa.hitl.status"] == "approved"
    rules = [s for s in finished if s.name == "execute_tool check_playbook_rule"]
    hits = {
        (s.attributes or {})["ementa.contract.clause"]: (s.attributes or {})[
            "ementa.contract.rule_hits"
        ]
        for s in rules
    }
    assert hits == {"1": 0, "2": 2, "3": 0, "4": 0}
    sizes = [
        e
        for s in finished
        if s.name == "execute_tool classify_clause"
        for e in s.events
        if e.name == "ementa.context.section_tokens"
    ]
    assert len(sizes) == 4  # the size of each section, per clause
    texts = [str(v) for s in finished for v in (s.attributes or {}).values()]
    assert not any("multa de 50%" in t for t in texts)  # no contract text in the trace
