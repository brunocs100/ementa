"""`contract_review` behind the approvals runner: the contract the route named, read from the
firm's documents, reviewed up to the lawyer's approval and finished by the decision.

Every contract here is fictitious. No test calls a real model.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from ementa.agents.contract_review import ContractReviewAgent, load_playbook
from ementa.agents.contract_review.runner import ContractReviewRun
from ementa.documents import Document, MediaType
from ementa.pii import pii_guard
from ementa.services import CONTRACT_PLAYBOOK
from ementa_kit.config import Settings
from ementa_kit.hitl import ApprovalDecision
from ementa_kit.hitl.runner import DraftingRunError, DraftingRunner
from ementa_kit.hitl.store import MemoryDraftingStore, RunStatus
from ementa_kit.llm import ModelRegistry, ModelSpec
from ementa_kit.policy import DataClass

CONTRACT = """# Contrato de academia de teste

> Contrato fictício, para os testes.

## Cláusula 1ª - Do atraso

1.1 Em caso de atraso, incide multa de 10% (dez por cento) sobre a prestação.

## Cláusula 2ª - Do foro

2.1 Fica eleito o foro da Comarca de São Paulo.
"""


class NoFindings(BaseChatModel):
    """A model that finds nothing: the playbook's rules alone fill the matrix."""

    @property
    def _llm_type(self) -> str:
        return "no-findings"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage('{"findings": []}'))])


def make_agent() -> ContractReviewAgent:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    def factory(spec: ModelSpec) -> BaseChatModel:
        return NoFindings()

    return ContractReviewAgent(
        ModelRegistry(settings, factory=factory),
        load_playbook(CONTRACT_PLAYBOOK),
        InMemorySaver(),
        pii_guard,
    )


class Documents:
    def __init__(self) -> None:
        self.files: dict[str, tuple[Document, bytes]] = {}

    def add(self, filename: str, content: bytes, media_type: MediaType) -> str:
        document = Document(
            id=str(uuid.uuid4()),
            uploaded_by="socia",
            owner_id="socia",
            acl=["socia"],
            secrecy_level=DataClass.RESTRICTED,
            sha256="0" * 64,
            filename=filename,
            media_type=media_type,
            size_bytes=len(content),
            chunks=None,
            created_at=datetime.now(UTC),
            indexed_at=None,
        )
        self.files[document.id] = (document, content)
        return document.id

    def get(self, document_id: str) -> Document | None:
        found = self.files.get(document_id)
        return found[0] if found else None

    def content(self, document_id: str) -> bytes | None:
        found = self.files.get(document_id)
        return found[1] if found else None


def start(documents: Documents, document_id: str) -> tuple[DraftingRunner, MemoryDraftingStore]:
    store = MemoryDraftingStore()
    agent = ContractReviewRun(make_agent(), documents)
    store.create_run("r1", agent.id, "socia", {"document_id": document_id})
    return DraftingRunner({agent.id: agent}, store), store


def test_a_contract_review_stops_for_the_lawyer_and_ends_with_the_decision() -> None:
    documents = Documents()
    document_id = documents.add("academia.md", CONTRACT.encode(), MediaType.MARKDOWN)
    runner, store = start(documents, document_id)

    waiting = runner.advance("r1")

    assert waiting.run.status is RunStatus.AWAITING_APPROVAL
    [approval] = waiting.pending
    assert approval.request.agent == "contract_review"
    assert approval.request.kind == "contract_report"
    draft = approval.request.draft
    assert "| 2 \N{EN DASH} Do foro |" in draft

    decision = ApprovalDecision(action="approve", decided_by="socia")
    assert store.decide(approval.id, decision, "") is not None
    done = runner.advance("r1", approval.id)

    assert done.run.status is RunStatus.FINALIZED
    result = done.run.result
    assert result is not None
    assert result["status"] == "approved"
    assert result["text"] == draft
    assert result["decided_by"] == "socia"
    assert result["playbook"] == "consumer-services@1"
    assert [row["clause"] for row in result["matrix"]] == ["1", "2"]
    assert result["matrix"][1]["rule"] == "foro-de-eleicao"


def test_a_document_that_is_gone_fails_the_run_before_any_model() -> None:
    runner, store = start(Documents(), str(uuid.uuid4()))

    with pytest.raises(DraftingRunError, match="no document"):
        runner.advance("r1")

    run = store.run("r1")
    assert run is not None and run.status is RunStatus.FAILED


def test_a_document_that_cannot_be_reviewed_fails_the_run() -> None:
    documents = Documents()
    document_id = documents.add("vazio.md", b"", MediaType.MARKDOWN)
    runner, store = start(documents, document_id)

    with pytest.raises(DraftingRunError, match="cannot be reviewed"):
        runner.advance("r1")

    run = store.run("r1")
    assert run is not None and run.status is RunStatus.FAILED
