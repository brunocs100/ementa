"""The `brief_drafting` agent: an initial petition in consumer law, planned thesis by thesis,
researched in parallel, drafted on a firm template, criticized, checked for citations and
finalized only with the lawyer's approval."""

from ementa.agents.brief_drafting.export import DRAFT_NOTICE, approval_notice, export_docx

__all__ = ["DRAFT_NOTICE", "approval_notice", "export_docx"]
