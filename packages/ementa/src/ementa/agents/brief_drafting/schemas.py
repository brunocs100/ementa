"""What `brief_drafting` takes, what its models answer and what a run returns.

The models answer in JSON (facts, plan, critique) or in tagged sections (the draft); the parsed
values enter the graph state as plain dicts, so a checkpoint holds no Python objects.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ementa.tools.research import CitationOut, RemovedOut
from ementa_kit.hitl import ApprovalRequest


class BriefExample(BaseModel):
    """A brief the firm already approved, as the episodic memory keeps it: the agent's draft,
    the text the lawyer finalized and the lawyer's comment. The drafting prompt shows the
    lawyer's edits as a few-shot example."""

    draft: str = Field(min_length=1)
    final: str = Field(min_length=1)
    comment: str | None = None


class Party(BaseModel):
    name: str = ""
    qualification: str = ""


class Facts(BaseModel):
    """The case as the `fast` model separated it, with personal data as markers."""

    plaintiff: Party = Field(default_factory=Party)
    defendants: list[Party] = Field(default_factory=list)
    court: str = ""
    city: str = ""
    facts: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    wants: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class PlannedThesis(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    question: str = Field(min_length=5, max_length=2000)  # what legal_research accepts
    request: str = Field(default="", max_length=1000)
    opinion: str | None = None


class Plan(BaseModel):
    theses: list[PlannedThesis] = Field(min_length=1)


class Criterion(BaseModel):
    ok: bool
    note: str = ""


class Critique(BaseModel):
    """The critic's rubric, one verdict per criterion."""

    completeness: Criterion
    citations: Criterion
    requests: Criterion
    tone: Criterion


class BriefPending(BaseModel):
    """A run paused before finalizing: the lawyer's decision resumes it."""

    thread_id: str
    owner_id: str = Field(description="Quem pediu a peça")
    request: ApprovalRequest


class BriefOutcome(BaseModel):
    """A finished run. `approved` finalizes the draft the lawyer saw, `edited` the lawyer's
    text, and `rejected` ends with no brief (`text` is None)."""

    thread_id: str
    status: Literal["approved", "edited", "rejected"]
    title: str
    text: str | None
    draft: str = Field(description="O rascunho que o advogado viu")
    decided_by: str
    decided_at: datetime
    comment: str | None
    citations: list[CitationOut] = Field(description="Fontes verificadas citadas no rascunho")
    removed: list[RemovedOut] = Field(description="Citações não verificadas tiradas do rascunho")
