"""The `brief_drafting` agent with scripted models and a scripted research agent: the parallel
theses, the critic, the citation check, the lawyer's approval and the examples.

Every person, company, CPF and CNPJ here is fictitious. The Postgres test needs
EMENTA_TEST_DATABASE_URL.
"""

import json
import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import psycopg
import pytest
from docx import Document
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import sql
from pydantic import Field

from ementa.agents.brief_drafting import (
    DRAFT_NOTICE,
    BriefDraftingAgent,
    BriefDraftingError,
    BriefExample,
    BriefLibrary,
    BriefPending,
    NoPendingApprovalError,
    UnknownTemplateError,
    approval_notice,
    export_docx,
)
from ementa.agents.brief_drafting import graph as brief_graph
from ementa.agents.brief_drafting.graph import (
    CRITIQUE_PROMPT,
    DRAFT_PROMPT,
    EXTRACT_PROMPT,
    FIX_JSON,
    PLAN_PROMPT,
)
from ementa.agents.legal_research import Citation, ResearchAnswer
from ementa.pii import pii_guard
from ementa.prompts import get_prompt
from ementa.services import FIRM_DIR
from ementa.tools.research import RemovedOut
from ementa_kit.config import Settings
from ementa_kit.context import assert_append_only
from ementa_kit.hitl import Action, ApprovalDecision
from ementa_kit.llm import ModelRegistry
from ementa_kit.llm.budget import BudgetCap, BudgetExceededError, BudgetWindow
from ementa_kit.memory import postgres_checkpointer
from ementa_kit.policy import DataClass

EXTRACT, PLAN, DRAFT, CRITIQUE = (
    p[0] for p in (EXTRACT_PROMPT, PLAN_PROMPT, DRAFT_PROMPT, CRITIQUE_PROMPT)
)
PROMPTS = {
    get_prompt(*p).text: p[0] for p in (EXTRACT_PROMPT, PLAN_PROMPT, DRAFT_PROMPT, CRITIQUE_PROMPT)
}
NOW = datetime(2026, 11, 15, 15, 0, tzinfo=UTC)  # noon in Brasília
TEMPLATE = "initial-petition-credit-listing"

DESCRIPTION = (
    "Ana Paula Ribeiro, brasileira, solteira, professora, CPF 123.456.789-09, mora em Porto "
    "Alegre/RS. Em 02/10/2026 ela teve um financiamento negado porque o nome dela estava num "
    "cadastro de inadimplentes, por uma dívida de R$ 1.234,56 de um cartão de crédito que ela "
    "nunca pediu ao Banco Horizonte S.A., CNPJ 11.222.333/0001-81. Ela reclamou ao banco em "
    "05/10/2026 e nada mudou. Quer o nome fora do cadastro e uma indenização. A ação vai para o "
    "Juizado Especial Cível da Comarca de Porto Alegre/RS."
)
REAL_VALUES = ("Ana Paula", "123.456.789-09", "11.222.333/0001-81")

FACTS = json.dumps(
    {
        "plaintiff": {
            "name": "[PESSOA_1]",
            "qualification": "brasileira, solteira, professora, inscrita no CPF sob o nº "
            "[CPF_1], residente em Porto Alegre/RS",
        },
        "defendants": [
            {
                "name": "Banco Horizonte S.A.",
                "qualification": "pessoa jurídica de direito privado, inscrita no CNPJ sob o nº "
                "[CNPJ_1]",
            }
        ],
        "court": "Juizado Especial Cível da Comarca de Porto Alegre/RS",
        "city": "Porto Alegre",
        "facts": [
            "Em 02/10/2026, a autora teve um financiamento negado por inscrição em cadastro de "
            "inadimplentes.",
            "A dívida de R$ 1.234,56 vem de um cartão de crédito que a autora nunca pediu.",
            "A autora reclamou ao banco em 05/10/2026, sem solução.",
        ],
        "evidence": ["comprovante da negativa de financiamento"],
        "wants": ["retirar o nome do cadastro", "indenização"],
        "missing": ["extrato do cadastro de inadimplentes"],
    },
    ensure_ascii=False,
)

Q_SERVICE = "O fornecedor de serviços responde sem culpa pelos danos de defeito no serviço?"
Q_DAMAGE = "O que diz a Súmula 385 do STJ sobre o dano moral por inscrição indevida?"
Q_URGENCY = "Quais são os requisitos da tutela de urgência no CPC?"
THESES = [
    ("Inexistência do débito e falha do serviço", Q_SERVICE, "opinion-2026-014", "declaração"),
    ("Dano moral pela inscrição indevida", Q_DAMAGE, "opinion-2026-014", "indenização"),
    ("Tutela de urgência", Q_URGENCY, "opinion-2026-033", "exclusão do nome"),
]
PLANNED = json.dumps(
    {
        "theses": [
            {"title": title, "question": question, "opinion": opinion, "request": request}
            for title, question, opinion, request in THESES
        ]
    },
    ensure_ascii=False,
)

TITLES = {
    "cdc:art14": "Código de Defesa do Consumidor, art. 14",
    "stj:sumula:385": "Súmula 385 do STJ",
    "cpc:art300": "Código de Processo Civil, art. 300",
}
FOUND = {
    Q_SERVICE: (
        "O fornecedor responde sem culpa pelo defeito do serviço [cdc:art14].",
        "cdc:art14",
    ),
    Q_DAMAGE: (
        "Não cabe dano moral quando há inscrição legítima anterior [stj:sumula:385].",
        "stj:sumula:385",
    ),
    Q_URGENCY: (
        "A tutela de urgência exige probabilidade do direito e perigo de dano [cpc:art300].",
        "cpc:art300",
    ),
}

FATOS = (
    "Em 02/10/2026, a autora [PESSOA_1] teve um financiamento negado porque seu nome estava "
    "inscrito em cadastro de inadimplentes por uma dívida de R$ 1.234,56.\n"
    "A dívida decorre de um cartão de crédito que a autora nunca contratou com a ré."
)
FUNDAMENTOS = (
    "DA INEXISTÊNCIA DO DÉBITO\n"
    "A ré responde, independentemente de culpa, pela falha na prestação do serviço [cdc:art14].\n"
    "DO DANO MORAL\n"
    "A inscrição indevida gera dano moral, pois não havia inscrição anterior legítima "
    "[stj:sumula:385].\n"
    "DA TUTELA DE URGÊNCIA\n"
    "Estão presentes a probabilidade do direito e o perigo de dano [cpc:art300]."
)
PEDIDOS = (
    "a concessão da tutela de urgência para excluir o nome da autora dos cadastros;\n"
    "- a declaração de inexistência do débito de R$ 1.234,56;\n"
    "c) a condenação da ré ao pagamento de indenização por danos morais de [VALOR A DEFINIR];"
)
INVENTED = "A Súmula 999 do STJ garante a indenização em dobro [stj:sumula:999]."


def drafted(fatos: str = FATOS, fundamentos: str = FUNDAMENTOS, pedidos: str = PEDIDOS) -> str:
    return (
        f"<fatos>\n{fatos}\n</fatos>\n<fundamentos>\n{fundamentos}\n</fundamentos>\n"
        f"<pedidos>\n{pedidos}\n</pedidos>"
    )


DRAFTED = drafted()
WITH_INVENTED = drafted(fundamentos=f"{FUNDAMENTOS}\n{INVENTED}")


def verdict(**failed: str) -> str:
    """The critic's JSON: every criterion passes but the ones given, with their notes."""
    return json.dumps(
        {
            name: {"ok": name not in failed, "note": failed.get(name, "")}
            for name in ("completeness", "citations", "requests", "tone")
        },
        ensure_ascii=False,
    )


PASSED = verdict()
TONE = "Tire o adjetivo 'abusiva' dirigido à ré."


class ByPrompt(BaseChatModel):
    """Answers each request with the next reply scripted for its prompt, told by the system
    message, and keeps a deep copy of every request per prompt."""

    replies: dict[str, list[str]]
    calls: dict[str, list[list[BaseMessage]]] = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "by-prompt"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        prompt = PROMPTS[messages[0].text]
        self.calls.setdefault(prompt, []).append([m.model_copy(deep=True) for m in messages])
        reply = AIMessage(self.replies[prompt].pop(0))
        return ChatResult(generations=[ChatGeneration(message=reply)])


class FakeResearcher:
    """legal_research by question. With `parties`, every call waits until that many run at
    once: the theses only get through if they are researched in parallel."""

    def __init__(
        self,
        found: dict[str, tuple[str, str]] | None = None,
        *,
        parties: int = 0,
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self.found = FOUND if found is None else found
        self.errors = errors or {}
        self.barrier = threading.Barrier(parties, timeout=10) if parties else None
        self.questions: list[str] = []
        self.threads: set[int] = set()

    def ask(self, question: str) -> ResearchAnswer:
        self.questions.append(question)
        self.threads.add(threading.get_ident())
        if self.barrier is not None:
            self.barrier.wait()
        if question in self.errors:
            raise self.errors[question]
        text, source = self.found.get(question, ("Não encontrei base nas fontes.", ""))
        cited = [Citation(source, TITLES[source], f"https://fonte/{source}")] if source else []
        return ResearchAnswer(
            question=question,
            answer=text,
            citations=cited,
            removed=[],
            sources=[source] if source else [],
            queries=[question],
            attempts=1,
            prompt="legal_research.answer@2",
            model="fake",
        )


def make_agent(
    *,
    drafts: list[str] | None = None,
    critiques: list[str] | None = None,
    facts: list[str] | None = None,
    plans: list[str] | None = None,
    researcher: FakeResearcher | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> tuple[BriefDraftingAgent, ByPrompt, FakeResearcher]:
    model = ByPrompt(
        replies={
            EXTRACT: facts if facts is not None else [FACTS],
            PLAN: plans if plans is not None else [PLANNED],
            DRAFT: drafts if drafts is not None else [DRAFTED],
            CRITIQUE: critiques if critiques is not None else [PASSED],
        }
    )
    research = researcher or FakeResearcher()
    registry = ModelRegistry(Settings(_env_file=None), factory=lambda spec: model)  # type: ignore[call-arg]
    agent = BriefDraftingAgent(
        registry,
        lambda: research,
        checkpointer or InMemorySaver(),
        pii_guard,
        BriefLibrary.from_dir(FIRM_DIR),
        clock=lambda: NOW,
    )
    return agent, model, research


def run(agent: BriefDraftingAgent, **kwargs: Any) -> BriefPending:
    return agent.draft(DESCRIPTION, template=TEMPLATE, user_id="rafael", **kwargs)


def config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def decide(action: Action, **fields: Any) -> dict[str, Any]:
    """The resume value the approvals API sends: an ApprovalDecision dump."""
    return ApprovalDecision(action=action, decided_by="helena", **fields).model_dump()


def every_request(model: ByPrompt) -> list[str]:
    return [m.text for calls in model.calls.values() for request in calls for m in request]


def test_the_theses_are_researched_in_parallel_and_ground_the_draft() -> None:
    agent, model, research = make_agent(researcher=FakeResearcher(parties=3))

    pending = run(agent)

    assert sorted(research.questions) == sorted([Q_SERVICE, Q_DAMAGE, Q_URGENCY])
    assert len(research.threads) == 3  # the barrier let them through together
    [opening] = model.calls[DRAFT]
    context = opening[1].text
    assert context.index("Tese t1: Inexistência") < context.index("Tese t2") < context.index("t3")
    assert "- [cdc:art14] Código de Defesa do Consumidor, art. 14" in context
    assert "Pesquisa verificada: A tutela de urgência exige" in context
    assert "Parecer opinion-2026-014" in context and "Parecer opinion-2026-033" in context
    assert "opinion-2026-021" not in context  # no thesis follows it
    assert "<exemplos>" not in context
    assert context.endswith("Redija as três seções da petição no formato pedido.\n</pergunta>")
    request = pending.request
    assert (request.agent, request.kind, request.data_class) == (
        "brief_drafting",
        "brief",
        DataClass.RESTRICTED,
    )
    assert request.title == "Petição inicial por negativação indevida"
    assert pending.owner_id == "rafael"


def test_the_draft_fills_the_template_and_reads_as_a_brief() -> None:
    agent, _, _ = make_agent()

    draft = run(agent).request.draft

    assert draft.startswith(
        "EXCELENTÍSSIMO(A) SENHOR(A) DOUTOR(A) JUIZ(A) DE DIREITO DO(A) JUIZADO ESPECIAL CÍVEL "
        "DA COMARCA DE PORTO ALEGRE/RS\n"
    )
    assert "Ana Paula Ribeiro, brasileira, solteira, professora, inscrita no CPF sob o nº " in draft
    assert "em face de Banco Horizonte S.A., pessoa jurídica de direito privado" in draft
    assert "prestação do serviço (art. 14 do CDC)." in draft
    assert "anterior legítima (Súmula 385 do STJ)." in draft
    assert "[cdc:" not in draft and "[stj:" not in draft
    assert "a) a concessão da tutela de urgência" in draft
    assert "b) a declaração de inexistência do débito" in draft  # the bullet is gone
    assert "c) a condenação da ré ao pagamento de indenização" in draft
    assert "d) a citação da(s) ré(s) para, querendo, apresentar contestação" in draft
    assert "Porto Alegre, 15 de novembro de 2026." in draft


def test_the_models_see_only_markers_and_the_lawyer_sees_the_values() -> None:
    agent, model, research = make_agent()

    pending = run(agent)

    sent = [*every_request(model), *research.questions]
    assert not any(value in text for text in sent for value in REAL_VALUES)
    [extraction] = model.calls[EXTRACT]
    assert "[PESSOA_1], brasileira, solteira, professora, CPF [CPF_1]" in extraction[1].text
    assert "CNPJ [CNPJ_1]" in extraction[1].text
    assert all(value in pending.request.draft for value in REAL_VALUES)


def test_a_failed_critique_gets_one_revision_on_the_same_conversation() -> None:
    agent, model, _ = make_agent(
        drafts=[DRAFTED, DRAFTED],
        critiques=[verdict(tone=TONE, requests="Ligue o pedido de tutela à tese."), PASSED],
    )

    pending = run(agent)

    first, second = model.calls[DRAFT]
    assert_append_only(model.calls[DRAFT])  # the revision went on, nothing it saw was edited
    assert len(second) == len(first) + 2  # its draft, then the critic's notes
    assert second[-1].text.startswith("A revisão do rascunho apontou:\n")
    assert f"- tom: {TONE}" in second[-1].text
    assert "- pedidos: Ligue o pedido de tutela à tese." in second[-1].text
    assert_append_only(model.calls[CRITIQUE])  # the critic saw its own first verdict
    assert model.calls[CRITIQUE][1][-1].text.startswith("<peca>\n")
    assert "revisão automática" not in pending.request.summary


def test_the_critic_gets_one_revision_only_and_its_last_notes_go_to_the_lawyer() -> None:
    agent, model, _ = make_agent(
        drafts=[DRAFTED, DRAFTED], critiques=[verdict(tone=TONE), verdict(tone=TONE)]
    )

    pending = run(agent)

    assert len(model.calls[DRAFT]) == len(model.calls[CRITIQUE]) == 2
    assert f"Pontos da revisão automática a conferir: tom: {TONE}" in pending.request.summary


def test_an_unverified_citation_sends_the_draft_back_once() -> None:
    agent, model, _ = make_agent(drafts=[WITH_INVENTED, DRAFTED])

    pending = run(agent)

    _, second = model.calls[DRAFT]
    assert_append_only(model.calls[DRAFT])
    feedback = second[-1].text
    assert feedback.startswith("O verificador de citações recusou o rascunho:\n")
    assert "- stj:sumula:999: fonte fora do contexto recuperado" in feedback
    assert len(model.calls[CRITIQUE]) == 1  # a rewrite for the citations goes back to the check
    assert "999" not in pending.request.draft
    assert "Trechos tirados" not in pending.request.summary


def test_an_unverified_citation_that_stays_is_removed_and_flagged() -> None:
    agent, model, _ = make_agent(drafts=[WITH_INVENTED, WITH_INVENTED])

    pending = run(agent)
    outcome = agent.resume(pending.thread_id, ApprovalDecision(action="approve", decided_by="h"))

    assert len(model.calls[DRAFT]) == 2  # one revision for the citations, then no more
    assert "999" not in pending.request.draft
    assert "estão presentes a probabilidade do direito" in pending.request.draft.lower()
    assert (
        "Trechos tirados por citação não verificada: Súmula 999 do STJ "
        "(fonte fora do contexto recuperado)."
    ) in pending.request.summary
    assert outcome.text is not None and "999" not in outcome.text
    assert outcome.removed == [
        RemovedOut(reference="stj:sumula:999", reason="fonte fora do contexto recuperado")
    ]


def test_a_thesis_without_a_verified_source_stays_out_of_the_brief() -> None:
    research = FakeResearcher(
        found={Q_SERVICE: FOUND[Q_SERVICE]}, errors={Q_DAMAGE: RuntimeError("banco fora do ar")}
    )
    only_service = drafted(
        fundamentos="DA INEXISTÊNCIA DO DÉBITO\nA ré responde pela falha do serviço [cdc:art14]."
    )
    agent, model, _ = make_agent(drafts=[only_service], researcher=research)

    pending = run(agent)

    context = model.calls[DRAFT][0][1].text
    assert "Tese t1" in context and "Tese t2" not in context and "Tese t3" not in context
    assert "opinion-2026-033" not in context  # only the opinions of the theses that stayed
    summary = pending.request.summary
    assert "Teses com fonte verificada: 1 de 3." in summary
    assert (
        "Teses sem fonte verificada, fora da peça: Dano moral pela inscrição indevida; "
        "Tutela de urgência."
    ) in summary
    assert "Fontes citadas: art. 14 do CDC." in summary


def test_a_spent_budget_stops_the_run_during_the_research() -> None:
    spent = BudgetExceededError(BudgetCap(BudgetWindow.DAY, 2.0), "dev", 2.0, NOW)
    agent, model, _ = make_agent(researcher=FakeResearcher(errors={Q_DAMAGE: spent}))

    with pytest.raises(BudgetExceededError):
        run(agent)
    assert DRAFT not in model.calls


@pytest.mark.parametrize("via_graph", [True, False])
def test_approve_finalizes_the_draft_the_lawyer_saw(via_graph: bool) -> None:
    agent, _, _ = make_agent()
    pending = run(agent)

    if via_graph:  # as the approvals API resumes a run
        agent.graph.invoke(Command(resume=decide("approve")), config(pending.thread_id))
        outcome = agent.outcome(pending.thread_id)
    else:
        outcome = agent.resume(
            pending.thread_id, ApprovalDecision(action="approve", decided_by="helena")
        )

    assert outcome is not None
    assert (outcome.status, outcome.decided_by, outcome.decided_at) == ("approved", "helena", NOW)
    assert outcome.text == outcome.draft == pending.request.draft
    assert [c.source_id for c in outcome.citations] == ["cdc:art14", "stj:sumula:385", "cpc:art300"]
    assert agent.pending(pending.thread_id) is None


def test_edit_finalizes_the_lawyers_text() -> None:
    agent, _, _ = make_agent()
    pending = run(agent)
    edited = pending.request.draft.replace("[A COMPLETAR PELO ADVOGADO]", "10.000,00")

    agent.graph.invoke(
        Command(resume=decide("edit", text=edited, comment="valor da causa")),
        config(pending.thread_id),
    )
    outcome = agent.outcome(pending.thread_id)

    assert outcome is not None
    assert (outcome.status, outcome.text, outcome.comment) == ("edited", edited, "valor da causa")
    assert outcome.draft == pending.request.draft


def test_reject_ends_with_no_brief() -> None:
    agent, _, _ = make_agent()
    pending = run(agent)

    agent.graph.invoke(
        Command(resume=decide("reject", comment="cliente desistiu")), config(pending.thread_id)
    )
    outcome = agent.outcome(pending.thread_id)

    assert outcome is not None
    assert (outcome.status, outcome.text, outcome.comment) == (
        "rejected",
        None,
        "cliente desistiu",
    )


def test_a_decision_that_cannot_be_applied_keeps_the_run_waiting() -> None:
    agent, _, _ = make_agent()
    pending = run(agent)
    thread = pending.thread_id

    with pytest.raises(ValueError, match="edited text"):  # refused before resuming anything
        agent.resume(thread, ApprovalDecision(action="edit", decided_by="helena"))
    agent.graph.invoke(Command(resume=decide("edit")), config(thread))  # no text to finalize
    agent.graph.invoke(Command(resume={"action": "approve"}), config(thread))  # nobody decided
    waiting = agent.pending(thread)
    outcome = agent.resume(thread, ApprovalDecision(action="approve", decided_by="helena"))

    assert waiting is not None and waiting.request == pending.request
    assert (outcome.status, outcome.text) == ("approved", pending.request.draft)


def test_only_a_run_waiting_for_approval_resumes() -> None:
    agent, _, _ = make_agent()
    approve = ApprovalDecision(action="approve", decided_by="helena")

    with pytest.raises(NoPendingApprovalError):
        agent.resume(str(uuid.uuid4()), approve)
    thread = run(agent).thread_id
    agent.resume(thread, approve)
    with pytest.raises(NoPendingApprovalError):
        agent.resume(thread, approve)
    assert agent.outcome(str(uuid.uuid4())) is None


def test_examples_reach_the_drafting_prompt_as_the_lawyers_edits() -> None:
    example = BriefExample(
        draft="DOS FATOS\nO autor Carlos Mendes teve o nome inscrito pela ré.",
        final="DOS FATOS\nO autor Carlos Mendes teve o nome inscrito indevidamente pela ré.",
        comment="Prefiro a palavra indevidamente.",
    )
    copied = drafted(fatos=f"{FATOS}\nComo no caso de [PESSOA_2], a inscrição foi indevida.")
    agent, model, _ = make_agent(drafts=[copied])

    pending = run(agent, examples=[example])

    context = model.calls[DRAFT][0][1].text
    examples = context[context.index("<exemplos>") : context.index("</exemplos>")]
    assert "Exemplo 1\nEdições do advogado" in examples
    assert "-O autor [PESSOA_2] teve o nome inscrito pela ré." in examples
    assert "+O autor [PESSOA_2] teve o nome inscrito indevidamente pela ré." in examples
    assert "Comentário do advogado: Prefiro a palavra indevidamente." in examples
    assert not any("Carlos Mendes" in text for text in every_request(model))
    assert "<exemplos>" not in model.calls[CRITIQUE][0][1].text
    # A marker of a past case never turns back into its value in this brief.
    assert "Como no caso de [PESSOA_2]" in pending.request.draft
    assert "Carlos Mendes" not in pending.request.draft


def test_an_example_approved_as_drafted_shows_the_approved_text() -> None:
    same = BriefExample(
        draft="DOS PEDIDOS\nRequer a citação.", final="DOS PEDIDOS\nRequer a citação."
    )
    agent, model, _ = make_agent()

    run(agent, examples=[same, same, same, same])

    context = model.calls[DRAFT][0][1].text
    assert "Exemplo 1\nAprovado sem edição:\nDOS PEDIDOS\nRequer a citação." in context
    assert "Exemplo 3" in context and "Exemplo 4" not in context  # three at most


def test_a_reply_out_of_format_gets_one_more_try_appended() -> None:
    agent, model, _ = make_agent(
        facts=["Os fatos são estes: a autora foi negativada.", FACTS],
        drafts=["Segue a peça, sem as seções.", DRAFTED],
    )

    run(agent)

    _, second = model.calls[EXTRACT]
    assert_append_only(model.calls[EXTRACT])
    assert second[-1].text == FIX_JSON
    assert len(model.calls[DRAFT]) == 2
    assert model.calls[DRAFT][1][-1].text.startswith("A resposta anterior não veio no formato")


def test_a_plan_out_of_format_twice_stops_the_run() -> None:
    agent, _, research = make_agent(plans=["sem plano", "ainda sem plano"])

    with pytest.raises(BriefDraftingError, match="plan"):
        run(agent)
    assert research.questions == []


def test_a_critic_out_of_format_leaves_the_review_to_the_lawyer() -> None:
    agent, model, _ = make_agent(critiques=["nada", "nada de novo"])

    pending = run(agent)

    assert len(model.calls[DRAFT]) == 1  # no revision without a verdict
    assert "A revisão automática não respondeu no formato" in pending.request.summary


def test_a_bad_request_fails_before_any_model_call() -> None:
    agent, model, _ = make_agent()

    with pytest.raises(UnknownTemplateError):
        agent.draft(DESCRIPTION, template="contestacao", user_id="rafael")
    with pytest.raises(ValueError, match="empty"):
        agent.draft("  ", template=TEMPLATE, user_id="rafael")
    assert model.calls == {}


def by_name(spans: list[ReadableSpan]) -> dict[str, list[ReadableSpan]]:
    named: dict[str, list[ReadableSpan]] = {}
    for span in spans:
        named.setdefault(span.name, []).append(span)
    return named


def test_a_run_and_its_approval_are_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(brief_graph, "tracer", provider.get_tracer("test"))
    agent, _, _ = make_agent(drafts=[WITH_INVENTED, DRAFTED])

    pending = run(agent)
    agent.resume(pending.thread_id, ApprovalDecision(action="edit", text="Peça.", decided_by="h"))

    spans = by_name(list(exporter.get_finished_spans()))
    run_span, approval_span = spans["invoke_agent brief_drafting"]
    attributes = dict(run_span.attributes or {})
    assert attributes["gen_ai.conversation.id"] == pending.thread_id
    assert attributes["ementa.prompt.id"] == "brief_drafting.draft"
    assert attributes["ementa.brief.template"] == TEMPLATE
    assert (attributes["ementa.brief.theses"], attributes["ementa.brief.grounded"]) == (3, 3)
    assert (attributes["ementa.brief.drafts"], attributes["ementa.brief.verdict"]) == (2, "passed")
    assert attributes["ementa.brief.citations"] == ("cdc:art14", "stj:sumula:385", "cpc:art300")
    assert attributes["ementa.approval.status"] == "pending"
    tools = spans["execute_tool legal_research"]
    assert sorted(str((t.attributes or {})["ementa.brief.thesis"]) for t in tools) == [
        "t1",
        "t2",
        "t3",
    ]
    checks = spans["guardrail citation_verifier"]
    assert [(c.attributes or {})["ementa.guardrail.decision"] for c in checks] == ["block", "pass"]
    for child in [*tools, *checks]:
        assert child.parent is not None and child.parent.span_id == run_span.context.span_id
    events = {e.name for e in run_span.events}
    assert {"ementa.brief.critique", "ementa.context.section_tokens"} <= events
    approval = dict(approval_span.attributes or {})
    assert approval["gen_ai.conversation.id"] == pending.thread_id
    assert (approval["ementa.approval.action"], approval["ementa.approval.status"]) == (
        "edit",
        "edited",
    )
    changed = approval["ementa.brief.changed_lines"]
    assert isinstance(changed, int) and changed > 0


def test_the_approved_brief_exports_with_who_approved_it() -> None:
    agent, _, _ = make_agent()
    pending = run(agent)
    outcome = agent.resume(pending.thread_id, ApprovalDecision(action="approve", decided_by="h"))
    assert outcome.text is not None

    data = export_docx(
        outcome.title, outcome.text, approved_by="Helena Duarte", approved_at=outcome.decided_at
    )

    document = Document(BytesIO(data))
    header = document.sections[0].header.paragraphs[0].text
    assert header == f"{approval_notice('Helena Duarte', NOW)}."
    assert header == "Aprovado por Helena Duarte em 15/11/2026 às 12:00 (horário de Brasília)."
    body = [p.text for p in document.paragraphs]
    assert body[0].startswith("EXCELENTÍSSIMO(A) SENHOR(A)")
    assert any("Ana Paula Ribeiro" in line for line in body)
    assert DRAFT_NOTICE not in header


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
def test_a_run_waits_for_the_lawyer_across_a_restart(schema: str) -> None:
    """ADR 0002: the run stops at the approval, the process ends, a new one resumes it."""
    before = postgres_checkpointer(DATABASE_URL, schema)
    agent, _, _ = make_agent(checkpointer=before)
    pending = run(agent)
    before.conn.close()

    after = postgres_checkpointer(DATABASE_URL, schema)
    try:
        restarted, model, research = make_agent(
            checkpointer=after, drafts=[], critiques=[], facts=[], plans=[]
        )
        waiting = restarted.pending(pending.thread_id)
        outcome = restarted.resume(
            pending.thread_id, ApprovalDecision(action="approve", decided_by="helena")
        )
    finally:
        after.conn.close()

    assert waiting == pending
    assert (outcome.status, outcome.text) == ("approved", pending.request.draft)
    assert model.calls == {} and research.questions == []  # nothing ran again
