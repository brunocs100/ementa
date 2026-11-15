"""Drafting runs on a real Postgres: `ai.runs` and `ai.approvals`, one decision per approval,
one worker per thread, and a paused run that survives a process restart (ADR 0002).

Set EMENTA_TEST_DATABASE_URL to run. Every name here is fictitious.
"""

import asyncio
import os
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, TypedDict

import psycopg
import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg import sql

from ementa_kit.audit import MemoryAuditLog
from ementa_kit.hitl import ApprovalDecision, ApprovalRequest, ask_for_approval
from ementa_kit.hitl.runner import DraftingIntake, DraftingRunner
from ementa_kit.hitl.store import ApprovalStatus, PgDraftingStore, RunStatus
from ementa_kit.jobs import Job, JobStatus, PgJobStore
from ementa_kit.memory import Episode, postgres_checkpointer
from ementa_kit.policy import DataClass

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL", "")
AGENT = "brief_drafting"
LAWYER = "u-associado"
REQUEST = ApprovalRequest(
    agent=AGENT,
    kind="brief",
    title="Petição inicial fictícia",
    draft="Rascunho fictício.",
    summary="Confira o pedido.",
    data_class=DataClass.SECRET,
)


class Draft(TypedDict, total=False):
    facts: str
    draft: str
    final: str | None


class FakeBriefs:
    """Drafts from the facts, asks for approval and finalizes what the lawyer let through."""

    id = AGENT

    def __init__(self, checkpointer: BaseCheckpointSaver[Any]) -> None:
        self.writes = 0
        graph = StateGraph(Draft)
        graph.add_node("write", self.write)
        graph.add_node("review", self.review)
        graph.add_edge(START, "write")
        graph.add_edge("write", "review")
        graph.add_edge("review", END)
        self.graph: CompiledStateGraph[Any, Any, Any, Any] = graph.compile(
            checkpointer=checkpointer
        )

    def write(self, state: Draft) -> Draft:
        self.writes += 1
        return {"draft": f"Rascunho: {state['facts']}"}

    def review(self, state: Draft) -> Draft:
        decision = ask_for_approval(REQUEST.model_copy(update={"draft": state["draft"]}))
        if decision.action == "reject":
            return {"final": None}
        return {"final": decision.text if decision.action == "edit" else state["draft"]}

    def example_query(self, request: Mapping[str, Any]) -> str:
        return str(request["facts"])

    def build_input(
        self, request: Mapping[str, Any], examples: Sequence[Episode]
    ) -> dict[str, Any]:
        return {"facts": request["facts"]}

    def result(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {"text": values.get("final")}


class InlineQueue:
    """Runs each leg at once with whatever runner the test holds now."""

    def __init__(self, jobs: PgJobStore) -> None:
        self.jobs = jobs
        self.runner: DraftingRunner | None = None

    async def enqueue(self, kind: str, job_id: str, **kwargs: Any) -> bool:
        assert self.runner is not None
        progress = await asyncio.to_thread(
            self.runner.advance, kwargs["run_id"], kwargs.get("approval_id")
        )
        self.jobs.succeed(job_id, {"status": str(progress.run.status)})
        return True


@pytest.fixture
def schemas() -> Iterator[tuple[str, str]]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    prefix = f"test_{uuid.uuid4().hex[:8]}"
    names = (f"{prefix}_ai", f"{prefix}_langgraph")
    yield names
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        for name in names:
            conn.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name)))


@pytest.fixture
def store(schemas: tuple[str, str]) -> PgDraftingStore:
    store = PgDraftingStore(DATABASE_URL, schema=schemas[0])
    store.ensure_schema()
    store.ensure_schema()  # runs again harmlessly, as on every start of the API
    return store


def decision(action: str = "approve", by: str = "u-socia", **kw: Any) -> ApprovalDecision:
    return ApprovalDecision.model_validate({"action": action, "decided_by": by, **kw})


def test_a_run_and_its_approval_round_trip(store: PgDraftingStore) -> None:
    run_id = str(uuid.uuid4())
    run, created = store.create_run(run_id, AGENT, LAWYER, {"facts": "Fatos fictícios.", "n": 2})
    again, created_again = store.create_run(run_id, AGENT, LAWYER, {"facts": "outros"})

    assert (created, created_again) == (True, False)
    assert again == run and run.input == {"facts": "Fatos fictícios.", "n": 2}
    assert (run.status, run.result, run.finished_at) == (RunStatus.QUEUED, None, None)

    opened = store.open_approval(run_id, "interrupt-1", REQUEST, LAWYER)
    assert (
        store.open_approval(run_id, "interrupt-1", REQUEST, LAWYER) == opened
    )  # one per interrupt
    assert opened.request == REQUEST  # data class included
    assert store.approvals(ApprovalStatus.PENDING) == [opened]
    assert store.approvals(ApprovalStatus.PENDING, requested_by="u-outro") == []

    edit = decision("edit", text="Texto do advogado.", comment="Ajustei o pedido.")
    decided = store.decide(opened.id, edit, "--- rascunho\n+++ final")
    assert decided is not None
    assert (decided.status, decided.decision, decided.diff) == (
        ApprovalStatus.EDITED,
        edit,
        "--- rascunho\n+++ final",
    )
    assert decided.decided_at is not None and decided.final_text == "Texto do advogado."
    assert store.decide(opened.id, decision(), "") is None  # decided once
    assert store.last_decided(run_id) == decided
    queued = store.run(run_id)
    assert queued is not None and queued.status is RunStatus.QUEUED  # the resume is next

    store.update_run(run_id, RunStatus.FINALIZED, result={"text": "Texto do advogado."})
    store.update_run(run_id, RunStatus.FINALIZED, result={"text": "Texto do advogado."})
    final = store.run(run_id)
    assert final is not None and final.result == {"text": "Texto do advogado."}
    assert final.finished_at is not None


def test_the_inbox_pages_oldest_first(store: PgDraftingStore) -> None:
    run_id = str(uuid.uuid4())
    store.create_run(run_id, AGENT, LAWYER, {})
    opened = [store.open_approval(run_id, f"interrupt-{n}", REQUEST, LAWYER) for n in range(3)]

    first = store.approvals(ApprovalStatus.PENDING, limit=2)
    rest = store.approvals(ApprovalStatus.PENDING, after=first[-1].id, limit=2)

    assert [a.id for a in first + rest] == [a.id for a in opened]


def test_one_decision_wins_when_two_arrive_together(store: PgDraftingStore) -> None:
    run_id = str(uuid.uuid4())
    store.create_run(run_id, AGENT, LAWYER, {})
    approval = store.open_approval(run_id, "interrupt-1", REQUEST, LAWYER)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def decide(action: str) -> None:
        barrier.wait()
        outcomes.append(store.decide(approval.id, decision(action), None))

    threads = [threading.Thread(target=decide, args=(a,)) for a in ("approve", "reject")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(o is None for o in outcomes) == [False, True]


def test_two_workers_never_move_one_run_at_once(store: PgDraftingStore) -> None:
    run_id = str(uuid.uuid4())
    events: list[str] = []
    held = threading.Event()

    def first() -> None:
        with store.lock(run_id):
            events.append("first in")
            held.set()
            time.sleep(0.3)
            events.append("first out")

    def second() -> None:
        held.wait()
        with store.lock(run_id):
            events.append("second in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert events == ["first in", "first out", "second in"]
    with store.lock(str(uuid.uuid4())), store.lock(run_id):  # another run is not held up
        pass


def test_ids_that_are_not_uuids_find_nothing(store: PgDraftingStore) -> None:
    assert store.run("x") is None
    assert store.approval("123.456.789-09") is None
    assert store.decide("x", decision(), None) is None
    assert store.last_decided("x") is None
    assert store.approvals(ApprovalStatus.PENDING, after="x") == []


def test_a_paused_run_survives_a_restart_and_resumes_where_it_stopped(
    schemas: tuple[str, str], store: PgDraftingStore
) -> None:
    jobs = PgJobStore(DATABASE_URL, schema=schemas[0])
    jobs.ensure_schema()
    queue = InlineQueue(jobs)
    intake = DraftingIntake(store, jobs, queue, MemoryAuditLog(), agents=[AGENT])

    before = postgres_checkpointer(DATABASE_URL, schemas[1])
    try:
        queue.runner = DraftingRunner({AGENT: FakeBriefs(before)}, store)
        started = asyncio.run(intake.start(AGENT, {"facts": "Fatos fictícios."}, LAWYER))
    finally:
        before.conn.close()  # the process ends with the run waiting for the lawyer

    [pending] = store.approvals(ApprovalStatus.PENDING)
    job = jobs.get(started.job.id)
    assert job is not None and (job.status, job.result) == (
        JobStatus.SUCCEEDED,
        {"status": "awaiting_approval"},
    )

    after = postgres_checkpointer(DATABASE_URL, schemas[1])
    try:
        agent = FakeBriefs(after)
        queue.runner = DraftingRunner({AGENT: agent}, store)
        decided = asyncio.run(intake.decide(pending.id, decision(by=LAWYER)))
        state = agent.graph.get_state({"configurable": {"thread_id": started.run.id}})
    finally:
        after.conn.close()

    assert isinstance(decided.job, Job)
    run = store.run(started.run.id)
    assert run is not None
    assert (run.status, run.result) == (RunStatus.FINALIZED, {"text": "Rascunho: Fatos fictícios."})
    assert state.values["final"] == "Rascunho: Fatos fictícios."
    assert agent.writes == 0  # the new process resumed at the approval, it did not draft again
