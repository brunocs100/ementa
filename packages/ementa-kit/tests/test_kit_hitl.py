"""Drafting runs with a lawyer in the loop, on a fake graph: no model, no database.

The intake records runs and decisions and queues each leg; here the queue runs the leg at once
with the runner, as the worker would. Every name and CPF here is fictitious.
"""

import asyncio
import hashlib
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import interrupt

from ementa_kit.audit import MemoryAuditLog
from ementa_kit.guardrails.pii import PiiGuard, build_pii_guard
from ementa_kit.hitl import ApprovalDecision, ApprovalRequest, ask_for_approval
from ementa_kit.hitl.runner import (
    DECISION_EVENT,
    DRAFTING_JOB,
    AlreadyDecidedError,
    DraftingIntake,
    DraftingRunError,
    DraftingRunner,
    IdempotencyKeyReusedError,
    UnknownAgentError,
    check_decision,
    run_id_for,
)
from ementa_kit.hitl.store import ApprovalStatus, MemoryDraftingStore, RunStatus
from ementa_kit.jobs import Job, JobStatus, QueueUnavailableError
from ementa_kit.memory import Episode, Episodes, LocalEmbeddings, episodes_of, forget_subject
from ementa_kit.memory import subject_id as make_subject_id

AGENT = "brief_drafting"
LAWYER = "u-associado"
FACTS = "Cobrança indevida de tarifa bancária em conta corrente, caso fictício."
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
KEY = b"test-key"


class Draft(TypedDict, total=False):
    facts: str
    examples: list[dict[str, str]]
    rounds: int
    draft: str
    final: str | None


class FakeBriefs:
    """A drafting agent without a model. It drafts from the facts and the examples, asks for
    approval and finalizes what the lawyer let through; with `redraft`, a reject sends it back
    to draft once more. `flaky` writes fail that many times, as a database that comes back."""

    id = AGENT

    def __init__(
        self,
        *,
        redraft: bool = False,
        ask: bool = True,
        named: str = AGENT,
        flaky: int = 0,
        raw_interrupt: bool = False,
    ) -> None:
        self.redraft = redraft
        self.ask = ask
        self.named = named
        self.flaky = flaky
        self.raw_interrupt = raw_interrupt
        self.plans = 0
        self.writes = 0
        graph = StateGraph(Draft)
        graph.add_node("plan", self.plan)
        graph.add_node("write", self.write)
        graph.add_node("review", self.review)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "write")
        graph.add_edge("write", "review")
        graph.add_conditional_edges("review", self.after_review, ["write", END])
        self.graph: CompiledStateGraph[Any, Any, Any, Any] = graph.compile(
            checkpointer=InMemorySaver()
        )

    def plan(self, state: Draft) -> Draft:
        self.plans += 1
        return {}

    def write(self, state: Draft) -> Draft:
        if self.flaky > 0:
            self.flaky -= 1
            raise ConnectionError("database restarting")
        self.writes += 1
        rounds = state.get("rounds", 0) + 1
        examples = [f"Exemplo aprovado: {e['final']}" for e in state.get("examples", [])]
        return {
            "draft": "\n".join([f"Rascunho {rounds}: {state['facts']}", *examples]),
            "rounds": rounds,
        }

    def review(self, state: Draft) -> Draft:
        if self.raw_interrupt:
            interrupt("continuar?")
        if not self.ask:  # a broken agent: it finalizes on its own
            return {"final": state["draft"]}
        request = ApprovalRequest(
            agent=self.named,
            kind="brief",
            title="Petição inicial fictícia",
            draft=state["draft"],
            summary="Confira os fatos e o pedido.",
        )
        decision = ask_for_approval(request)
        if decision.action == "reject":
            return {"final": None}
        return {"final": decision.text if decision.action == "edit" else state["draft"]}

    def after_review(self, state: Draft) -> str:
        again = state.get("final") is None and self.redraft and state.get("rounds", 0) < 2
        return "write" if again else END

    def example_query(self, request: Mapping[str, Any]) -> str:
        return str(request["facts"])

    def build_input(
        self, request: Mapping[str, Any], examples: Sequence[Episode]
    ) -> dict[str, Any]:
        return {
            "facts": request["facts"],
            "examples": [{"final": e.final, "diff": e.diff} for e in examples],
        }

    def result(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {"text": values.get("final")}


class FakeJobs:
    """One job per kind and subject; a failed one goes back to the queue, as in `ai.jobs`."""

    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], Job] = {}

    def submit(self, kind: str, subject: str, requested_by: str) -> Job:
        job = self.jobs.get((kind, subject))
        if job is None or job.status is JobStatus.FAILED:
            job = Job(
                id=job.id if job else str(uuid.uuid4()),
                kind=kind,
                subject=subject,
                requested_by=requested_by,
                status=JobStatus.QUEUED,
                attempts=0,
                error=None,
                result=None,
                created_at=NOW,
                updated_at=NOW,
                started_at=None,
                finished_at=None,
            )
            self.jobs[(kind, subject)] = job
        return job

    def finish(self, job_id: str, status: JobStatus) -> None:
        for key, job in self.jobs.items():
            if job.id == job_id:
                self.jobs[key] = replace(job, status=status)


class InlineQueue:
    """Runs each leg at once, as the worker would: a failure that may pass is tried again, up
    to `tries` in all, and then fails the job and the run; a `DraftingRunError` fails the job at
    once."""

    def __init__(self, runner: DraftingRunner, jobs: FakeJobs, tries: int = 1) -> None:
        self.runner = runner
        self.jobs = jobs
        self.tries = tries
        self.down = False
        self.sent: list[dict[str, Any]] = []

    async def enqueue(self, kind: str, job_id: str, **kwargs: Any) -> bool:
        if self.down:
            raise QueueUnavailableError("Redis did not take the job")
        assert kind == DRAFTING_JOB
        self.sent.append(kwargs)
        run_id, approval_id = kwargs["run_id"], kwargs.get("approval_id")
        for attempt in range(1, self.tries + 1):
            try:
                await asyncio.to_thread(self.runner.advance, run_id, approval_id)
            except DraftingRunError:
                break
            except Exception as error:
                if attempt == self.tries:
                    self.runner.fail(run_id, f"{type(error).__name__} after {attempt} tries")
            else:
                self.jobs.finish(job_id, JobStatus.SUCCEEDED)
                return True
        self.jobs.finish(job_id, JobStatus.FAILED)
        return True


class HashEmbedder:
    """Bag of words hashed into 64 dimensions: texts sharing words are close."""

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


def new_store() -> InMemoryStore:
    embeddings = LocalEmbeddings(HashEmbedder())
    return InMemoryStore(index={"dims": HashEmbedder.dim, "embed": embeddings, "fields": ["text"]})


@dataclass
class Desk:
    agent: FakeBriefs
    store: MemoryDraftingStore
    jobs: FakeJobs
    queue: InlineQueue
    audit: MemoryAuditLog
    intake: DraftingIntake
    runner: DraftingRunner

    def start(self, facts: str = FACTS, key: str | None = None) -> str:
        started = asyncio.run(self.intake.start(AGENT, {"facts": facts}, LAWYER, key))
        return started.run.id

    def pending(self, run_id: str) -> str:
        [approval] = [
            a for a in self.store.approvals(ApprovalStatus.PENDING) if a.thread_id == run_id
        ]
        return approval.id

    def decide(self, approval_id: str, action: str, text: str | None = None, **kw: Any) -> Any:
        decision = ApprovalDecision.model_validate(
            {"action": action, "text": text, "decided_by": kw.pop("by", LAWYER), **kw}
        )
        return asyncio.run(self.intake.decide(approval_id, decision))


def desk(agent: FakeBriefs | None = None, episodes: Episodes | None = None, tries: int = 1) -> Desk:
    agent = agent or FakeBriefs()
    store, jobs, audit = MemoryDraftingStore(), FakeJobs(), MemoryAuditLog()
    runner = DraftingRunner({AGENT: agent}, store, episodes)
    queue = InlineQueue(runner, jobs, tries)
    intake = DraftingIntake(store, jobs, queue, audit, agents=[AGENT])
    return Desk(agent, store, jobs, queue, audit, intake, runner)


def test_a_run_stops_for_approval_and_nothing_is_final_before_a_decision() -> None:
    d = desk()

    run_id = d.start()

    run = d.store.run(run_id)
    assert run is not None
    assert (run.status, run.result, run.agent, run.requested_by) == (
        RunStatus.AWAITING_APPROVAL,
        None,
        AGENT,
        LAWYER,
    )
    [approval] = d.store.approvals(ApprovalStatus.PENDING)
    assert approval.thread_id == run_id
    assert approval.request.draft == f"Rascunho 1: {FACTS}"
    assert (approval.request.kind, approval.request.summary) == (
        "brief",
        "Confira os fatos e o pedido.",
    )
    assert (approval.decision, approval.diff, approval.final_text) == (None, None, None)
    assert "final" not in d.agent.graph.get_state({"configurable": {"thread_id": run_id}}).values


def test_approve_finalizes_the_draft_as_written() -> None:
    d = desk()
    approval_id = d.pending(d.start())

    decided = d.decide(approval_id, "approve")

    approval = d.store.approval(approval_id)
    run = d.store.run(decided.approval.thread_id)
    assert approval is not None and run is not None
    assert (approval.status, approval.diff) == (ApprovalStatus.APPROVED, "")
    assert (run.status, run.result) == (RunStatus.FINALIZED, {"text": f"Rascunho 1: {FACTS}"})
    assert run.finished_at is not None
    assert d.agent.writes == 1  # the resume did not draft again


def test_edit_finalizes_the_lawyer_text_and_keeps_the_diff() -> None:
    d = desk()
    approval_id = d.pending(d.start())
    edited = f"Rascunho revisto: {FACTS}\nPede a devolução em dobro."

    d.decide(approval_id, "edit", edited, comment="Faltava o pedido.")

    approval = d.store.approval(approval_id)
    assert approval is not None and approval.decision is not None
    assert approval.status is ApprovalStatus.EDITED
    assert approval.decision.comment == "Faltava o pedido."
    assert "-Rascunho 1:" in (approval.diff or "") and "+Pede a devolução em dobro." in (
        approval.diff or ""
    )
    run = d.store.run(approval.thread_id)
    assert run is not None and (run.status, run.result) == (RunStatus.FINALIZED, {"text": edited})


def test_reject_ends_the_run_with_nothing_final() -> None:
    d = desk()
    approval_id = d.pending(d.start())

    d.decide(approval_id, "reject", comment="Fatos incompletos.")

    approval = d.store.approval(approval_id)
    assert approval is not None
    run = d.store.run(approval.thread_id)
    assert run is not None and (run.status, run.result) == (RunStatus.REJECTED, {"text": None})
    assert (approval.status, approval.diff, approval.final_text) == (
        ApprovalStatus.REJECTED,
        None,
        None,
    )


def test_after_a_reject_the_agent_may_draft_again_and_ask_again() -> None:
    d = desk(FakeBriefs(redraft=True))
    run_id = d.start()
    first = d.pending(run_id)

    d.decide(first, "reject", comment="Refaça.")
    second = d.pending(run_id)

    assert second != first
    run = d.store.run(run_id)
    assert run is not None and run.status is RunStatus.AWAITING_APPROVAL
    approval = d.store.approval(second)
    assert approval is not None and approval.request.draft.startswith("Rascunho 2:")

    d.decide(second, "approve")
    run = d.store.run(run_id)
    assert run is not None and run.status is RunStatus.FINALIZED
    assert run.result == {"text": f"Rascunho 2: {FACTS}"}


def test_a_decision_is_taken_once_and_the_same_one_again_only_queues_the_resume() -> None:
    d = desk()
    approval_id = d.pending(d.start())
    d.decide(approval_id, "approve")

    with pytest.raises(AlreadyDecidedError):
        d.decide(approval_id, "reject")
    with pytest.raises(AlreadyDecidedError):  # the same action by someone else is another one
        d.decide(approval_id, "approve", by="u-socia")
    again = d.decide(approval_id, "approve")

    assert again.repeated is True
    assert [e.action for e in (r.event for r in d.audit.records)] == [DECISION_EVENT]
    approval = d.store.approval(approval_id)
    assert approval is not None and approval.status is ApprovalStatus.APPROVED


def test_a_retried_resume_does_not_answer_the_next_interrupt() -> None:
    d = desk(FakeBriefs(redraft=True))
    run_id = d.start()
    first = d.pending(run_id)
    d.decide(first, "reject")
    second = d.pending(run_id)

    progress = d.runner.advance(run_id, first)  # the resume of `first` delivered twice

    assert [a.id for a in progress.pending] == [second]
    approval = d.store.approval(second)
    assert approval is not None and approval.status is ApprovalStatus.PENDING
    assert d.agent.writes == 2


def test_a_retried_start_changes_nothing() -> None:
    d = desk()
    run_id = d.start()

    progress = d.runner.advance(run_id)

    assert progress.run.status is RunStatus.AWAITING_APPROVAL
    assert len(d.store.approvals(ApprovalStatus.PENDING)) == 1
    assert d.agent.writes == 1


def test_a_graph_stopped_by_a_failure_goes_on_from_its_checkpoint() -> None:
    d = desk(FakeBriefs(flaky=1), tries=2)

    run_id = d.start()  # the first try fails in `write`, after `plan`

    run = d.store.run(run_id)
    assert run is not None and run.status is RunStatus.AWAITING_APPROVAL
    assert (d.agent.plans, d.agent.writes) == (1, 1)  # the retry did not start over


def test_an_agent_that_ends_without_asking_fails_the_run() -> None:
    d = desk(FakeBriefs(ask=False))
    run_id = d.start()

    run = d.store.run(run_id)
    assert run is not None
    assert (run.status, run.result) == (RunStatus.FAILED, None)
    assert run.error == "The agent ended without asking for approval, so nothing was finalized."


@pytest.mark.parametrize(
    ("agent", "error"),
    [
        (FakeBriefs(raw_interrupt=True), "an interrupt that is not an approval request"),
        (FakeBriefs(named="contract_review"), "names the agent 'contract_review'"),
    ],
    ids=["raw-interrupt", "other-agent"],
)
def test_an_interrupt_the_runner_cannot_take_fails_the_run(agent: FakeBriefs, error: str) -> None:
    d = desk(agent)
    run_id = d.start()

    run = d.store.run(run_id)
    assert run is not None and run.status is RunStatus.FAILED
    assert error in (run.error or "")
    assert d.store.approvals(ApprovalStatus.PENDING) == []


def test_runs_the_worker_cannot_move() -> None:
    d = desk()
    store = d.store
    run, _ = store.create_run(str(uuid.uuid4()), "case_monitor", LAWYER, {})

    with pytest.raises(DraftingRunError, match="No drafting agent 'case_monitor'"):
        d.runner.advance(run.id)
    with pytest.raises(DraftingRunError, match="There is no run"):
        d.runner.advance(str(uuid.uuid4()))
    failed = store.run(run.id)
    assert failed is not None and failed.status is RunStatus.FAILED


def test_a_resume_without_a_decision_is_refused_and_the_run_keeps_waiting() -> None:
    d = desk()
    run_id = d.start()
    approval_id = d.pending(run_id)

    with pytest.raises(DraftingRunError, match="not decided"):
        d.runner.advance(run_id, approval_id)

    run = d.store.run(run_id)
    assert run is not None and run.status is RunStatus.AWAITING_APPROVAL


def test_the_same_idempotency_key_is_the_same_run() -> None:
    d = desk()

    first = d.start(key="pedido-1")
    again = asyncio.run(d.intake.start(AGENT, {"facts": FACTS}, LAWYER, "pedido-1"))

    assert (again.run.id, again.duplicate) == (first, True)
    assert again.job.status is JobStatus.SUCCEEDED
    assert len(d.queue.sent) == 1  # nothing to queue again
    assert first == run_id_for(LAWYER, AGENT, "pedido-1")
    assert run_id_for("u-socia", AGENT, "pedido-1") != first  # keys are per user
    with pytest.raises(IdempotencyKeyReusedError):
        asyncio.run(d.intake.start(AGENT, {"facts": "Outros fatos."}, LAWYER, "pedido-1"))
    assert d.start() != d.start()  # without a key, each request is a run


def test_a_run_that_failed_for_good_goes_back_to_the_queue_with_its_key() -> None:
    d = desk(FakeBriefs(flaky=1))
    run_id = d.start(key="pedido-2")
    failed = d.store.run(run_id)
    assert failed is not None and failed.status is RunStatus.FAILED
    assert failed.error == "ConnectionError after 1 tries"

    again = asyncio.run(d.intake.start(AGENT, {"facts": FACTS}, LAWYER, "pedido-2"))

    assert (again.run.id, again.run.status) == (run_id, RunStatus.QUEUED)
    run = d.store.run(run_id)
    assert run is not None and (run.status, run.error) == (RunStatus.AWAITING_APPROVAL, None)
    assert d.agent.plans == 1  # it went on from where it stopped


def test_queue_down_keeps_the_decision_and_the_same_decision_again_resumes() -> None:
    d = desk()
    approval_id = d.pending(d.start())
    d.queue.down = True

    with pytest.raises(QueueUnavailableError):
        d.decide(approval_id, "approve")
    d.queue.down = False
    again = d.decide(approval_id, "approve")

    assert again.repeated is True
    run = d.store.run(again.approval.thread_id)
    assert run is not None and run.status is RunStatus.FINALIZED
    assert len(d.audit.records) == 1  # one decision, one event


def test_unknown_agents_are_refused_before_anything_is_stored() -> None:
    d = desk()

    with pytest.raises(UnknownAgentError):
        asyncio.run(d.intake.start("contract_review", {"document_id": "x"}, LAWYER))

    assert d.store.runs == {}


def test_the_decision_event_names_who_and_what_but_holds_no_text() -> None:
    d = desk()
    run_id = d.start()
    approval_id = d.pending(run_id)

    d.decide(approval_id, "edit", "Texto com o CPF 123.456.789-09.", comment="Nome: Maria Souza")

    [record] = d.audit.records
    event = record.event
    assert (event.actor, event.action, event.resource) == (LAWYER, DECISION_EVENT, AGENT)
    assert (event.data_class, event.outcome) == ("restricted", "ok")
    assert event.details == {
        "approval_id": approval_id,
        "run_id": run_id,
        "kind": "brief",
        "action": "edit",
        "requested_by": LAWYER,
    }


@pytest.mark.parametrize(
    ("action", "text"),
    [("edit", None), ("edit", "   "), ("approve", "texto"), ("reject", "texto")],
)
def test_an_edit_carries_the_text_and_nothing_else_does(action: str, text: str | None) -> None:
    decision = ApprovalDecision.model_validate(
        {"action": action, "text": text, "decided_by": LAWYER}
    )

    with pytest.raises(ValueError):
        check_decision(decision)


def test_the_lawyers_edit_is_an_example_in_the_next_run(guard: PiiGuard) -> None:
    store = new_store()
    episodes = Episodes(store, lambda: guard, subject_key=KEY)
    d = desk(episodes=episodes)
    first = d.pending(d.start())
    edited = (
        "Maria Souza, CPF 123.456.789-09, pede a devolução em dobro da tarifa cobrada sem contrato."
    )
    d.decide(first, "edit", edited)

    second = d.pending(d.start("Tarifa bancária cobrada sem contrato, fictícia."))

    approval = d.store.approval(second)
    assert approval is not None
    draft = approval.request.draft
    example = r"\[PESSOA_\d\], CPF \[CPF_1\], pede a devolução em dobro da tarifa"
    assert re.search(f"Exemplo aprovado: {example}", draft)
    assert "Maria Souza" not in draft and "123.456.789-09" not in draft
    [episode] = episodes.examples_for(AGENT, "tarifa cobrada sem contrato")
    assert (episode.key, episode.action, episode.kind) == (first, "edit", "brief")
    assert episode.diff.startswith("--- rascunho\n+++ final\n")
    assert re.search(rf"\n\+{example}", episode.diff)

    maria = make_subject_id(KEY, "PESSOA", "Maria Souza")
    assert forget_subject(store, maria) == 1  # forgetting a person reaches the episodes
    assert episodes.examples_for(AGENT, "tarifa cobrada sem contrato") == []


def test_a_rejected_draft_is_no_example(guard: PiiGuard) -> None:
    store = new_store()
    d = desk(episodes=Episodes(store, lambda: guard, subject_key=KEY))

    d.decide(d.pending(d.start()), "reject")

    assert store.search(episodes_of(AGENT).path) == []
