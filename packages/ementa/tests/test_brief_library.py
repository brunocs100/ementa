"""The firm's brief templates and opinions in data/firm/, all fictitious."""

from datetime import date
from pathlib import Path

import pytest

from ementa.agents.brief_drafting import BriefLibrary, LibraryError, UnknownTemplateError
from ementa.agents.brief_drafting.library import FIELDS
from ementa.domain.citations import verify_citations
from ementa.services import FIRM_DIR

TEMPLATE = """---
id: {id}
title: Petição de teste
description: Só para o teste.
area: consumidor
approved_by: Helena Duarte, sócia
approved_at: 2026-10-05
guidance: Fatos e fundamentos.
fixed_requests:
  - a citação da ré.
---
{body}
"""
BODY = "\n".join(f"{{{{{field}}}}}" for field in FIELDS)


def test_the_firm_templates_and_opinions_load() -> None:
    library = BriefLibrary.from_dir(FIRM_DIR)

    assert [t.id for t in library.templates()] == [
        "initial-petition-credit-listing",
        "initial-petition-product-defect",
    ]
    assert [o.id for o in library.opinions()] == [
        "opinion-2026-014",
        "opinion-2026-021",
        "opinion-2026-033",
    ]
    for template in library.templates():
        assert template.title and template.guidance and len(template.fixed_requests) == 3
        assert isinstance(template.approved_at, date)
    assert library.opinion(None) is None and library.opinion("opinion-1999-001") is None
    with pytest.raises(UnknownTemplateError):
        library.template("contestacao")


def test_the_fixed_text_of_every_template_passes_the_citation_check() -> None:
    """Only the model's sections cite; a template naming an article would block every brief."""
    for template in BriefLibrary.from_dir(FIRM_DIR).templates():
        text = template.render({field: "texto" for field in FIELDS})
        requests = "\n".join(template.fixed_requests)

        assert verify_citations(f"{text}\n{requests}", allowed=[]).removed == []


def test_a_value_is_never_read_as_a_placeholder(tmp_path: Path) -> None:
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "peca.md").write_text(
        TEMPLATE.format(id="peca", body=BODY), encoding="utf-8"
    )
    template = BriefLibrary.from_dir(tmp_path).template("peca")

    rendered = template.render(
        {field: "{{fatos}}" if field == "autor" else "x" for field in FIELDS}
    )

    assert rendered.splitlines()[1] == "{{fatos}}"


@pytest.mark.parametrize(
    ("name", "content", "problem"),
    [
        ("peca", TEMPLATE.format(id="outra", body=BODY), "id must be the file name"),
        ("peca", TEMPLATE.format(id="peca", body=BODY + "\n{{valor}}"), "unknown placeholders"),
        ("peca", TEMPLATE.format(id="peca", body="{{fatos}}"), "missing placeholders"),
        ("peca", TEMPLATE.format(id="peca", body=BODY).replace("guidance", "guide"), "fields"),
        ("peca", BODY, "front matter"),
    ],
)
def test_a_malformed_template_fails_naming_the_file(
    tmp_path: Path, name: str, content: str, problem: str
) -> None:
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / f"{name}.md").write_text(content, encoding="utf-8")

    with pytest.raises(LibraryError, match=problem) as raised:
        BriefLibrary.from_dir(tmp_path)
    assert f"{name}.md" in str(raised.value)
