"""The planted clauses of evals/contracts_planted.jsonl, scored against the gold.

The rules alone run here on every push and cost nothing. The full agent, with the criteria on
the `fast` model, is the `eval` test: it calls the provider and skips without OPENAI_API_KEY.
When a score here changes, update the table in docs/AVALIACAO.md.
"""

from typing import NoReturn

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ementa.agents.contract_review import ContractReviewAgent, load_playbook
from ementa.agents.contract_review.evaluation import (
    Evaluation,
    Scores,
    evaluate,
    load_gold,
    review_gold,
    scores_table,
)
from ementa.domain.contracts import split_contract
from ementa.pii import pii_guard
from ementa.services import CONTRACT_PLAYBOOK
from ementa_kit.config import Settings, get_settings
from ementa_kit.llm import ModelRegistry, ModelSpec


def test_the_gold_names_every_clause_of_its_contract_once() -> None:
    gold = load_gold()

    assert len(gold) == 10
    for case in gold:
        numbers = [c.number for c in split_contract(case.path.read_text("utf-8")).clauses]
        assert set(case.planted).isdisjoint(case.clean), case.id
        assert sorted(numbers) == sorted([*case.planted, *case.clean]), case.id
        assert "fictício" in case.path.read_text("utf-8"), case.id  # marked as fictitious


def test_the_gold_plants_every_item_of_the_playbook() -> None:
    playbook = load_playbook(CONTRACT_PLAYBOOK)
    planted = {rule for case in load_gold() for rules in case.planted.values() for rule in rules}

    assert planted == {item.id for item in playbook.items}


def test_scores() -> None:
    scores = Scores(tp=8, fp=2, fn=0)

    assert (scores.precision, scores.recall, round(scores.f1, 3)) == (0.8, 1.0, 0.889)
    assert Scores(0, 0, 0).f1 == 0.0


def no_model(spec: ModelSpec) -> NoReturn:
    raise AssertionError("the rules run without a model")


def no_guard() -> NoReturn:
    raise AssertionError("the rules run without the PII guard")


def rules_only_review() -> Evaluation:
    playbook = load_playbook(CONTRACT_PLAYBOOK)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    agent = ContractReviewAgent(
        ModelRegistry(settings, factory=no_model), playbook, InMemorySaver(), no_guard
    )
    gold = load_gold()
    found = review_gold(agent, gold, rules_only=True)
    return evaluate(found, gold, {rule.id for rule in playbook.rules})


def test_the_rules_alone_find_the_planted_clauses(capsys: pytest.CaptureFixture[str]) -> None:
    """Measured on 15/11/2026, before any rule was tuned to these contracts. The three misses
    are written in on purpose: a supplier that may end the contract, fair because the consumer
    may too (another clause); a 12-month lock-in the reciprocity check takes for a consumer's
    right; and a price set "by the table in force", an indirect change no pattern names."""
    result = rules_only_review()
    with capsys.disabled():
        print("\n" + scores_table(result))

    assert result.pairs == Scores(tp=40, fp=1, fn=2)
    assert result.pairs.f1 >= 0.8
    # Every planted clause, the ones only the criteria can find included
    assert result.clauses == Scores(tp=36, fp=1, fn=13)
    assert result.errors == (
        "FP internet-02 cláusula 6 cancelamento-unilateral",
        "FN saude-02 cláusula 9 cancelamento-unilateral",
        "FN servicos-02 cláusula 6 preco-unilateral",
    )


@pytest.mark.eval
def test_the_full_agent_finds_the_planted_clauses(capsys: pytest.CaptureFixture[str]) -> None:
    """The rules and the criteria on the real `fast` model: 97 calls, one per clause, about
    US$ 0.01 on GPT-6 Luna (estimated from the prices, not measured). Not run yet; the phase
    target is an F1 of at least 0.8."""
    settings = get_settings()
    key = settings.openai_api_key
    if key is None or not key.get_secret_value():
        pytest.skip("OPENAI_API_KEY is not set: the criteria need the fast model")
    playbook = load_playbook(CONTRACT_PLAYBOOK)
    agent = ContractReviewAgent(ModelRegistry(settings), playbook, InMemorySaver(), pii_guard)
    gold = load_gold()

    found = review_gold(agent, gold, rules_only=False)
    result = evaluate(found, gold, {item.id for item in playbook.items})
    with capsys.disabled():
        print("\n" + scores_table(result))
        print("\n".join(result.errors))

    assert result.clauses.f1 >= 0.8
    assert result.pairs.f1 >= 0.8
