"""Episodic memory with a hashed bag-of-words embedder and the kit's PII guard.

Every name and CPF here is fictitious.
"""

import hashlib
import math
import re
from collections.abc import Sequence

import pytest
from langgraph.store.memory import InMemoryStore

from ementa_kit.guardrails.pii import PiiGuard, build_pii_guard
from ementa_kit.memory import Episodes, LocalEmbeddings, edit_diff, episodes_of

KEY = b"test-key"


class HashEmbedder:
    model_name = "hash"
    dim = 64

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in re.findall(r"\w+", text.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


@pytest.fixture(scope="module")
def guard() -> PiiGuard:
    return build_pii_guard()


@pytest.fixture
def episodes(guard: PiiGuard) -> Episodes:
    embeddings = LocalEmbeddings(HashEmbedder())
    store = InMemoryStore(index={"dims": HashEmbedder.dim, "embed": embeddings, "fields": ["text"]})
    return Episodes(store, lambda: guard, subject_key=KEY)


def record(episodes: Episodes, key: str, final: str, agent: str = "brief_drafting") -> None:
    episodes.record(
        key, agent=agent, kind="brief", title="Peça fictícia", draft="Rascunho.", final=final
    )


def test_examples_come_closest_first_and_at_most_k(episodes: Episodes) -> None:
    record(episodes, "a1", "Devolução em dobro da tarifa bancária cobrada sem contrato.")
    record(episodes, "a2", "Indenização por voo cancelado sem aviso.")
    record(episodes, "a3", "Tarifa bancária cobrada duas vezes no mesmo mês.")

    found = episodes.examples_for("brief_drafting", "tarifa bancária cobrada sem contrato", k=2)

    assert [e.key for e in found] == ["a1", "a3"]
    assert found[0].score is not None and found[1].score is not None
    assert found[0].score >= found[1].score
    assert episodes.examples_for("brief_drafting", "tarifa", k=0) == []


def test_examples_are_kept_per_agent(episodes: Episodes) -> None:
    record(episodes, "a1", "Cláusula de multa revista.", agent="contract_review")

    assert episodes.examples_for("brief_drafting", "multa") == []
    [episode] = episodes.examples_for("contract_review", "multa")
    assert episode.agent == "contract_review"
    assert episodes.store.search(episodes_of("contract_review").path)[0].key == "a1"


def test_recording_an_approval_again_replaces_its_episode(episodes: Episodes) -> None:
    record(episodes, "a1", "Primeira versão.")
    record(episodes, "a1", "Versão final.")

    [episode] = episodes.examples_for("brief_drafting", "versão")
    assert episode.final == "Versão final."


def test_title_draft_final_and_comment_share_the_markers(episodes: Episodes) -> None:
    episode = episodes.record(
        "a1",
        agent="brief_drafting",
        kind="brief",
        title="Peça fictícia",
        draft="Maria Souza pede a devolução.",
        final="Maria Souza, CPF 123.456.789-09, pede a devolução em dobro.",
        action="edit",
        comment="Conferi o CPF 123.456.789-09 com Maria Souza.",
        decided_by="u-socia",
    )

    text = " ".join((episode.title, episode.draft, episode.final, episode.comment or ""))
    assert "Maria Souza" not in text and "123.456.789-09" not in text
    marker = re.search(r"\[PESSOA_\d\]", episode.draft)  # the same person, the same marker
    assert marker is not None and marker.group(0) in episode.final
    assert "[CPF_1]" in (episode.comment or "")
    assert episode.diff == "\n".join(
        [
            "--- rascunho",
            "+++ final",
            "@@ -1 +1 @@",
            f"-{episode.draft}",
            f"+{episode.final}",
        ]
    )
    stored = episodes.store.get(episodes_of("brief_drafting").path, "a1")
    assert stored is not None
    assert len(stored.value["subjects"]) == 2  # the person and the CPF
    assert stored.value["decided_by"] == "u-socia"


def test_the_diff_is_empty_when_the_text_did_not_change() -> None:
    assert edit_diff("Parágrafo um.\nParágrafo dois.", "Parágrafo um.\nParágrafo dois.") == ""
    lines = edit_diff("um\ndois\ntrês\nquatro", "um\n2\ntrês\nquatro").splitlines()
    assert lines[2:] == ["@@ -1,3 +1,3 @@", " um", "-dois", "+2", " três"]  # one line around
