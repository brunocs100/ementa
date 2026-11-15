"""DOCX export of a brief: the draft notice until the lawyer approves it."""

from datetime import UTC, datetime
from io import BytesIO

import pytest
from docx import Document

from ementa.agents.brief_drafting import DRAFT_NOTICE, export_docx

TEXT = "EXCELENTÍSSIMO(A) SENHOR(A) JUIZ(A)\n\nI. DOS FATOS\n\nA autora teve o nome inscrito."


def header_of(data: bytes) -> str:
    return "\n".join(p.text for p in Document(BytesIO(data)).sections[0].header.paragraphs)


def test_a_draft_carries_the_notice_in_the_header_of_every_page() -> None:
    data = export_docx("Petição inicial por negativação indevida", TEXT)

    document = Document(BytesIO(data))
    assert header_of(data) == f"{DRAFT_NOTICE}."
    assert DRAFT_NOTICE == "Rascunho para conferência do advogado responsável"
    assert all(not s.different_first_page_header_footer for s in document.sections)
    assert [p.text for p in document.paragraphs] == [
        "EXCELENTÍSSIMO(A) SENHOR(A) JUIZ(A)",
        "I. DOS FATOS",
        "A autora teve o nome inscrito.",
    ]
    assert document.core_properties.title == "Petição inicial por negativação indevida"


def test_an_approved_brief_says_who_approved_it_and_when() -> None:
    approved_at = datetime(2026, 11, 16, 17, 5, tzinfo=UTC)

    data = export_docx("Petição", TEXT, approved_by="Helena Duarte", approved_at=approved_at)

    header = header_of(data)
    assert header == "Aprovado por Helena Duarte em 16/11/2026 às 14:05 (horário de Brasília)."
    assert DRAFT_NOTICE not in header


@pytest.mark.parametrize(
    ("approved_by", "approved_at", "problem"),
    [
        ("Helena Duarte", None, "together"),
        (None, datetime(2026, 11, 16, tzinfo=UTC), "together"),
        ("Helena Duarte", datetime(2026, 11, 16, 14, 5), "time zone"),
    ],
)
def test_an_approval_needs_who_and_when(
    approved_by: str | None, approved_at: datetime | None, problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        export_docx("Petição", TEXT, approved_by=approved_by, approved_at=approved_at)
