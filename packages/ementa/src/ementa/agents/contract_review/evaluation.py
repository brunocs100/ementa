"""The planted clauses of `evals/contracts_planted.jsonl`: precision, recall and F1 of a review
against the gold, by (clause, rule) pair and by flagged clause.

Each gold line names a fictitious contract in `data/firm/contracts/`, the clauses planted with a
problem (each with the rules and criteria it breaks) and the clean ones. Pairs count only the
items in `scope`, say the deterministic rules; clauses count every planted clause, so a review
by the rules alone shows what only the criteria could find.
"""

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ementa.agents.contract_review.graph import ContractReviewAgent

REPO_ROOT = Path(__file__).resolve().parents[6]
GOLD_FILE = REPO_ROOT / "evals" / "contracts_planted.jsonl"


@dataclass(frozen=True)
class GoldContract:
    id: str
    path: Path
    planted: Mapping[str, frozenset[str]]  # clause -> the rules and criteria it breaks
    clean: frozenset[str]

    def pairs(self, scope: Collection[str] | None = None) -> set[tuple[str, str]]:
        return {
            (clause, rule)
            for clause, rules in self.planted.items()
            for rule in rules
            if scope is None or rule in scope
        }


def load_gold(path: Path = GOLD_FILE, root: Path = REPO_ROOT) -> list[GoldContract]:
    gold: list[GoldContract] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        planted = {p["clause"]: frozenset(p["rules"]) for p in case["planted"]}
        gold.append(
            GoldContract(case["id"], root / case["file"], planted, frozenset(case["clean"]))
        )
    return gold


@dataclass(frozen=True)
class Scores:
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def score(found: Iterable[object], expected: Iterable[object]) -> Scores:
    got, want = set(found), set(expected)
    return Scores(len(got & want), len(got - want), len(want - got))


@dataclass(frozen=True)
class Evaluation:
    pairs: Scores  # (contract, clause, rule), for the rules in scope
    clauses: Scores  # (contract, clause): flagged by anything against planted with anything
    by_rule: Mapping[str, Scores]
    errors: tuple[str, ...]  # "FP academia-01 cláusula 5 alteracao-unilateral"


def evaluate(
    found: Mapping[str, Iterable[tuple[str, str]]],
    gold: Sequence[GoldContract],
    scope: Collection[str],
) -> Evaluation:
    """`found` maps a contract id to the (clause, rule) pairs a review flagged."""
    got: set[tuple[str, str, str]] = set()
    want: set[tuple[str, str, str]] = set()
    flagged: set[tuple[str, str]] = set()
    planted: set[tuple[str, str]] = set()
    for case in gold:
        pairs = set(found.get(case.id, ()))
        got |= {(case.id, clause, rule) for clause, rule in pairs if rule in scope}
        want |= {(case.id, clause, rule) for clause, rule in case.pairs(scope)}
        flagged |= {(case.id, clause) for clause, _ in pairs}
        planted |= {(case.id, clause) for clause in case.planted}
    by_rule = {
        rule: score({p for p in got if p[2] == rule}, {p for p in want if p[2] == rule})
        for rule in sorted(scope)
        if any(p[2] == rule for p in got | want)
    }
    errors = [f"FP {c} cláusula {n} {r}" for c, n, r in sorted(got - want)]
    errors += [f"FN {c} cláusula {n} {r}" for c, n, r in sorted(want - got)]
    return Evaluation(score(got, want), score(flagged, planted), by_rule, tuple(errors))


def review_gold(
    agent: "ContractReviewAgent", gold: Sequence[GoldContract], *, rules_only: bool
) -> dict[str, set[tuple[str, str]]]:
    """Run the agent on each gold contract, up to the approval, and collect what it flagged."""
    found: dict[str, set[tuple[str, str]]] = {}
    for case in gold:
        run = agent.review(case.path.read_bytes(), case.path.name, rules_only=rules_only)
        found[case.id] = {(row.clause, row.rule) for row in run.report.matrix}
    return found


def scores_table(evaluation: Evaluation) -> str:
    """The scores as a Markdown table, for the docs and the eval output."""
    lines = [
        "| Medida | VP | FP | FN | Precisão | Recall | F1 |",
        "|---|---|---|---|---|---|---|",
    ]
    rows = [("Pares (cláusula, regra)", evaluation.pairs), ("Cláusulas", evaluation.clauses)]
    rows += [(f"`{rule}`", scores) for rule, scores in evaluation.by_rule.items()]
    for name, s in rows:
        lines.append(
            f"| {name} | {s.tp} | {s.fp} | {s.fn} | {s.precision:.2f} | {s.recall:.2f} "
            f"| {s.f1:.2f} |"
        )
    return "\n".join(lines) + "\n"
