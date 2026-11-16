"""What a contract review returns: the risk matrix, the redlines and the report the lawyer
approves. Every text carries the real values back; only the model saw markers."""

from typing import Literal

from pydantic import BaseModel, Field

from ementa.agents.legal_research import NOTICE
from ementa.domain.contracts import Severity
from ementa.tools.research import RemovedOut
from ementa_kit.hitl import ApprovalDecision, ApprovalRequest

ReviewStatus = Literal["pending", "approved", "edited", "rejected"]
FindingSource = Literal["rule", "model"]


class ClauseOut(BaseModel):
    number: str
    title: str


class RiskRow(BaseModel):
    """A line of the risk matrix: a clause, the playbook item it breaks and the law behind it."""

    clause: str = Field(description="Número da cláusula")
    clause_title: str
    rule: str = Field(description="Id da regra ou do critério do playbook")
    rule_title: str
    severity: Severity
    basis: list[str] = Field(description="Ids de fonte do corpus, conferidos pelo verificador")
    source: FindingSource = Field(description="rule: regra do playbook; model: critério do modelo")
    why: str = Field(description="A regra jurídica, citada pelos ids de `basis`")
    note: str = Field(default="", description="O motivo que o modelo deu, nos critérios")
    excerpt: list[str] = Field(default_factory=list, description="Frases da cláusula apontadas")


class Change(BaseModel):
    before: str = Field(description="Frase atual; vazia quando o texto é acrescentado")
    after: str = Field(description="Redação sugerida; vazia quando a frase sai")


class Redline(BaseModel):
    """The suggested rewrite of one flagged clause."""

    clause: str
    clause_title: str
    rules: list[str]
    original: str
    suggested: str
    changes: list[Change]


class ContractReport(BaseModel):
    title: str
    filename: str
    document_id: str | None = None
    playbook: str = Field(description="id@versão do playbook")
    prompt: str | None = Field(description="id@versão do prompt dos critérios; None só regras")
    model: str | None = Field(description="Modelo dos critérios; None quando só as regras rodaram")
    rules_only: bool
    clauses: list[ClauseOut]
    matrix: list[RiskRow]
    redlines: list[Redline]
    removed: list[RemovedOut] = Field(description="Citações e achados que o verificador tirou")
    unreviewed: list[str] = Field(description="Cláusulas que o modelo não avaliou")
    draft: str = Field(description="O relatório em Markdown, como vai para a aprovação")
    notice: str = NOTICE


class ReviewRun(BaseModel):
    """A review as it stands: waiting for the lawyer (`pending`, with the `request`) or decided,
    with the text that was approved or edited in `final`. A rejected report has no `final`."""

    thread_id: str
    status: ReviewStatus
    requested_by: str | None = None
    report: ContractReport
    request: ApprovalRequest | None = None
    decision: ApprovalDecision | None = None
    final: str | None = None
