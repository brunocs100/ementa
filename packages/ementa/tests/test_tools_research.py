import pytest
from pydantic import ValidationError

from ementa.agents.legal_research import Citation, ResearchAnswer
from ementa.domain.citations import RemovedCitation
from ementa.tools.research import (
    AnswerLegalQuestionInput,
    AnswerLegalQuestionOutput,
    SearchLegalCorpusInput,
)


@pytest.mark.parametrize(
    "payload", [{"query": "ab"}, {"query": "prazo", "k": 11}, {"query": "prazo", "x": 1}]
)
def test_search_input_is_strict(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SearchLegalCorpusInput.model_validate(payload)


def test_answer_input_rejects_a_too_short_question() -> None:
    with pytest.raises(ValidationError):
        AnswerLegalQuestionInput(question="oi?")


def test_answer_output_maps_the_agent_answer() -> None:
    answer = ResearchAnswer(
        question="q",
        answer="a [cdc:art49]",
        citations=[Citation("cdc:art49", "CDC, art. 49", "https://x")],
        removed=[RemovedCitation("stj:sumula:1", "s", "fonte fora do contexto recuperado")],
        sources=["cdc:art49"],
        queries=["q"],
        attempts=2,
        prompt="legal_research.answer@1",
        model="m",
    )

    out = AnswerLegalQuestionOutput.from_answer(answer)

    assert out.grounded is True
    assert out.citations[0].source_id == "cdc:art49"
    assert out.removed[0].reference == "stj:sumula:1"
    assert out.data_class == "public"
    assert out.notice.startswith("Rascunho")
