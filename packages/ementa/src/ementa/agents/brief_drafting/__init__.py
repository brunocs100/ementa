"""The `brief_drafting` agent: an initial petition in consumer law, planned thesis by thesis,
researched in parallel, drafted on a firm template, criticized, checked for citations and
finalized only with the lawyer's approval."""

from ementa.agents.brief_drafting.export import DRAFT_NOTICE, approval_notice, export_docx
from ementa.agents.brief_drafting.graph import (
    AGENT_ID,
    KIND,
    BriefDraftingAgent,
    BriefDraftingError,
    NoPendingApprovalError,
)
from ementa.agents.brief_drafting.library import (
    BriefLibrary,
    BriefTemplate,
    LibraryError,
    Opinion,
    UnknownTemplateError,
)
from ementa.agents.brief_drafting.schemas import BriefExample, BriefOutcome, BriefPending

__all__ = [
    "AGENT_ID",
    "DRAFT_NOTICE",
    "KIND",
    "BriefDraftingAgent",
    "BriefDraftingError",
    "BriefExample",
    "BriefLibrary",
    "BriefOutcome",
    "BriefPending",
    "BriefTemplate",
    "LibraryError",
    "NoPendingApprovalError",
    "Opinion",
    "UnknownTemplateError",
    "approval_notice",
    "export_docx",
]
