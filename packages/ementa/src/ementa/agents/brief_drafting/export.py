"""DOCX export of a brief with python-docx: one paragraph per line of the text, and a notice in
the header of every page. Until the lawyer approves the brief, the notice says it is a draft;
afterwards, it says who approved it and when."""

from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

DRAFT_NOTICE = "Rascunho para conferência do advogado responsável"
BRASILIA = ZoneInfo("America/Sao_Paulo")


def approval_notice(approved_by: str, approved_at: datetime) -> str:
    """The approval line: "Aprovado por Helena Duarte em 16/11/2026 às 14:05 (horário de
    Brasília)"."""
    when = approved_at.astimezone(BRASILIA)
    return f"Aprovado por {approved_by} em {when:%d/%m/%Y} às {when:%H:%M} (horário de Brasília)"


def export_docx(
    title: str,
    text: str,
    *,
    approved_by: str | None = None,
    approved_at: datetime | None = None,
) -> bytes:
    """The brief as a .docx file. `title` goes to the document properties.

    Without `approved_by` and `approved_at` the header carries the draft notice; with them, the
    approval: pass the approver's name, as the lawyer reads it, and the decision time with its
    time zone.
    """
    if (approved_by is None) != (approved_at is None):
        raise ValueError("approved_by and approved_at go together")
    if approved_by is None or approved_at is None:
        notice = DRAFT_NOTICE
    elif approved_at.tzinfo is None:
        raise ValueError("approved_at needs a time zone")
    else:
        notice = approval_notice(approved_by, approved_at)

    document = Document()
    document.core_properties.title = title
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    header = document.sections[0].header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.CENTER
    header.add_run(f"{notice}.").bold = True
    for line in text.splitlines():
        if line.strip():  # a blank line only separates paragraphs
            paragraph = document.add_paragraph(line.strip())
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()
