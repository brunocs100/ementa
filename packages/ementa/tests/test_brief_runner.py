"""`brief_drafting` behind the approvals runner: request in, graph input; final state, result."""

import pytest

from ementa.agents.brief_drafting import BriefLibrary
from ementa.agents.brief_drafting.runner import DEFAULT_TEMPLATE, BriefDraftingRun
from ementa.services import FIRM_DIR
from ementa_kit.memory.episodes import Episode


class Agent:
    graph = object()
    library = BriefLibrary.from_dir(FIRM_DIR)


def run() -> BriefDraftingRun:
    return BriefDraftingRun(Agent())  # type: ignore[arg-type]


def test_the_request_becomes_the_graph_input_with_the_examples() -> None:
    from datetime import UTC, datetime

    example = Episode(
        key="a1",
        agent="brief_drafting",
        kind="brief",
        title="t",
        draft="d",
        final="f",
        diff="",
        action="approve",
        comment="ok",
        updated_at=datetime.now(UTC),
    )

    built = run().build_input({"facts": "Fatos.", "requested_by": "u1"}, [example])

    assert built["template"] == DEFAULT_TEMPLATE
    assert built["description"] == "Fatos."
    assert built["owner_id"] == "u1"
    assert built["examples"] == [{"draft": "d", "final": "f", "comment": "ok"}]
    assert run().example_query({"facts": "Fatos."}) == "Fatos."
    assert run().id == "brief_drafting"


def test_an_unknown_template_fails_the_run_before_any_model() -> None:
    with pytest.raises(Exception, match="nao-existe"):
        run().build_input({"facts": "Fatos.", "template": "nao-existe"}, [])


def test_the_final_state_becomes_the_run_result() -> None:
    values = {
        "outcome": {
            "status": "approved",
            "text": "Peça final.",
            "decided_by": "u2",
            "decided_at": "2026-11-15T12:00:00+00:00",
            "comment": None,
        },
        "review": {"title": "Ação de indenização", "draft": "Rascunho."},
        "check": {"valid": ["cdc:art6", "cdc:art14"], "removed": []},
    }

    result = run().result(values)

    assert result["status"] == "approved"
    assert result["text"] == "Peça final."
    assert result["citations"] == ["cdc:art14", "cdc:art6"]
