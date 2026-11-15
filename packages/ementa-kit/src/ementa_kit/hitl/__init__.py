"""Human in the loop: an agent stops before it finalizes and waits for a lawyer's decision.

Interface fixed for F4. A LangGraph node calls `ask_for_approval(...)`; the graph pauses (it is
compiled with a checkpointer) and the interrupt carries the `ApprovalRequest`. The approvals
API resumes the run with an `ApprovalDecision` through `Command(resume=decision.model_dump())`,
and `ask_for_approval` returns that decision inside the node.
"""

from typing import Literal

from langgraph.types import interrupt
from pydantic import BaseModel, Field

from ementa_kit.policy import DataClass

Action = Literal["approve", "reject", "edit"]


class ApprovalRequest(BaseModel):
    """What the lawyer sees: the draft to approve, with what it is and why it stopped."""

    agent: str = Field(description="Agent id, e.g. brief_drafting or contract_review")
    kind: str = Field(description="What is being approved, e.g. brief or contract_report")
    title: str
    draft: str = Field(description="The text to approve or edit, as the agent would finalize it")
    summary: str = Field(default="", description="Why it stopped and what to check")
    data_class: DataClass = DataClass.RESTRICTED


class ApprovalDecision(BaseModel):
    action: Action
    text: str | None = Field(default=None, description="The edited text, when action is edit")
    comment: str | None = None
    decided_by: str = Field(description="User id of whoever decided")


def ask_for_approval(request: ApprovalRequest) -> ApprovalDecision:
    """Pause the graph until a lawyer decides; returns the decision when the run resumes."""
    return ApprovalDecision.model_validate(interrupt(request.model_dump(mode="json")))


__all__ = ["Action", "ApprovalDecision", "ApprovalRequest", "ask_for_approval"]
