"""Every evaluation case points to units that exist in the corpus snapshots."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from ementa.ingest.corpus import SOURCE_IDS, load_statute
from ementa.ingest.index import statute_chunks

EVALS = Path(__file__).resolve().parents[3] / "evals"


def cases(name: str) -> list[dict[str, Any]]:
    lines = (EVALS / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


@pytest.fixture(scope="module")
def chunk_ids() -> set[str]:
    return {c.id for source in SOURCE_IDS for c in statute_chunks(load_statute(source))}


@pytest.mark.parametrize("name", ["smoke_cdc.jsonl", "legal_research.jsonl"])
def test_eval_cases_point_to_units_of_the_corpus(name: str, chunk_ids: set[str]) -> None:
    rows = cases(name)
    assert len({row["id"] for row in rows}) == len(rows)
    for row in rows:
        assert row["expected"], row["id"]
        assert set(row["expected"]) <= chunk_ids, row["id"]


def test_legal_research_eval_mixes_the_sources() -> None:
    rows = cases("legal_research.jsonl")
    assert len(rows) == 30
    first = Counter(row["expected"][0].split(":")[0] for row in rows)
    assert first == {"cdc": 10, "lgpd": 7, "cpc": 7, "stj": 4, "stf": 2}
