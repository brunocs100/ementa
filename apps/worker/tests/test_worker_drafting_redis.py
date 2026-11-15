"""A drafting run through a real arq worker, Redis and Postgres, on a fake graph: it starts,
stops for approval, and resumes with the lawyer's decision, each leg a job on the queue.

Set EMENTA_TEST_DATABASE_URL and EMENTA_TEST_REDIS_URL to run; without them the tests skip.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

import psycopg
import pytest
from arq.worker import Worker, func
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg import sql

from ementa_kit.audit import MemoryAuditLog
from ementa_kit.hitl import ApprovalDecision, ApprovalRequest, ask_for_approval
from ementa_kit.hitl.runner import DRAFTING_JOB, DraftingIntake, DraftingRunner
from ementa_kit.hitl.store import ApprovalStatus, PgDraftingStore, RunStatus
from ementa_kit.jobs import JobQueue, JobStatus, PgJobStore, worker_options
from ementa_kit.memory import Episode, postgres_checkpointer
from ementa_worker.worker import Resources, run_drafting

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("EMENTA_TEST_REDIS_URL")
AGENT = "brief_drafting"
LAWYER = "u-associado"


class Draft(TypedDict, total=False):
    facts: str
    draft: str
    final: str | None


class FakeBriefs:
    id = AGENT

    def __init__(self, checkpointer: BaseCheckpointSaver[Any]) -> None:
        graph = StateGraph(Draft)
        graph.add_node("write", lambda state: {"draft": f"Rascunho: {state['facts']}"})
        graph.add_node("review", self.review)
        graph.add_edge(START, "write")
        graph.add_edge("write", "review")
        graph.add_edge("review", END)
        self.graph: CompiledStateGraph[Any, Any, Any, Any] = graph.compile(
            checkpointer=checkpointer
        )

    def review(self, state: Draft) -> Draft:
        request = ApprovalRequest(agent=AGENT, kind="brief", title="Peça", draft=state["draft"])
        decision = ask_for_approval(request)
        return {"final": None if decision.action == "reject" else state["draft"]}

    def example_query(self, request: Mapping[str, Any]) -> str:
        return str(request["facts"])

    def build_input(
        self, request: Mapping[str, Any], examples: Sequence[Episode]
    ) -> dict[str, Any]:
        return {"facts": request["facts"]}

    def result(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {"text": values.get("final")}


class NoIngestor:
    def ingest(self, document_id: str) -> int:
        raise AssertionError("not used here")


@dataclass
class Env:
    queue: JobQueue
    store: PgDraftingStore
    jobs: PgJobStore
    runner: DraftingRunner
    intake: DraftingIntake
    checkpointer: PostgresSaver


@pytest.fixture
def env() -> Iterator[Env]:
    if not (DATABASE_URL and REDIS_URL):
        pytest.skip("EMENTA_TEST_DATABASE_URL and EMENTA_TEST_REDIS_URL are needed")
    prefix = f"test_{uuid.uuid4().hex[:8]}"
    store = PgDraftingStore(DATABASE_URL, schema=f"{prefix}_ai")
    jobs = PgJobStore(DATABASE_URL, schema=f"{prefix}_ai")
    store.ensure_schema()
    jobs.ensure_schema()
    checkpointer = postgres_checkpointer(DATABASE_URL, f"{prefix}_langgraph")
    runner = DraftingRunner({AGENT: FakeBriefs(checkpointer)}, store)
    queue = JobQueue(REDIS_URL, queue_name=f"{prefix}:queue")
    intake = DraftingIntake(store, jobs, queue, MemoryAuditLog(), agents=[AGENT])
    try:
        yield Env(queue, store, jobs, runner, intake, checkpointer)
    finally:
        checkpointer.conn.close()
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            for schema in (f"{prefix}_ai", f"{prefix}_langgraph"):
                conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


async def drain(env: Env) -> Worker:
    """Run an arq worker until the queue is empty, as `ementa-worker --burst` does."""
    assert REDIS_URL
    worker = Worker(
        functions=[func(run_drafting, name=DRAFTING_JOB)],
        **worker_options(REDIS_URL, env.queue.queue_name),
        burst=True,
        keep_result=0,
        poll_delay=0.05,
        ctx={"ementa": Resources(env.jobs, NoIngestor(), retry_base_s=0.1, drafting=env.runner)},
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()
    return worker


def test_a_run_pauses_and_resumes_through_the_queue(env: Env) -> None:
    async def scenario() -> None:
        started = await env.intake.start(AGENT, {"facts": "Fatos fictícios."}, LAWYER)
        worker = await drain(env)
        assert (worker.jobs_complete, worker.jobs_failed) == (1, 0)

        [approval] = env.store.approvals(ApprovalStatus.PENDING)
        job = env.jobs.get(started.job.id)
        assert job is not None and (job.status, job.result) == (
            JobStatus.SUCCEEDED,
            {"run_id": started.run.id, "status": "awaiting_approval", "approvals": [approval.id]},
        )

        decision = ApprovalDecision(action="approve", decided_by=LAWYER)
        decided = await env.intake.decide(approval.id, decision)
        worker = await drain(env)
        assert (worker.jobs_complete, worker.jobs_failed) == (1, 0)

        run = env.store.run(started.run.id)
        assert run is not None
        assert (run.status, run.result) == (
            RunStatus.FINALIZED,
            {"text": "Rascunho: Fatos fictícios."},
        )
        resume = env.jobs.get(decided.job.id)
        assert resume is not None and resume.status is JobStatus.SUCCEEDED
        assert resume.subject == approval.id

    asyncio.run(scenario())
