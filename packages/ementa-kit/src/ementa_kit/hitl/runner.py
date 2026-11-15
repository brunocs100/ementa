"""Drafting runs with a lawyer in the loop: start a drafting agent's graph on a thread of its
own, stop where it asks for approval, resume it with the lawyer's decision (F4).

`DraftingIntake` is the API side. It records a run or a decision and puts the `run_drafting` job
on the arq queue. `DraftingRunner` is the worker side. It moves the graph as far as it goes,
records each `ApprovalRequest` the agent stops with as a pending approval and, when the graph
ends, the result. Both legs of a run go through the worker, the start and each resume: a graph
calls models for minutes, and after a reject an agent may draft again and ask again.

A leg can run twice, since arq runs a job at least once. So `advance` reads where the thread
stopped before it moves it: a resume whose interrupt was already answered never answers the next
one, and a graph that a failure stopped halfway goes on from its last checkpoint.

Nothing is finalized without a decision. The graph cannot get past `ask_for_approval` until it is
resumed, the runner resumes it only with a decision recorded in the store, and a graph that ends
without having asked fails the run instead of finishing it.
"""

import asyncio
import json
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Interrupt
from pydantic import ValidationError

from ementa_kit.audit import AuditEvent, AuditLog, Outcome
from ementa_kit.hitl import ApprovalDecision, ApprovalRequest
from ementa_kit.hitl.store import (
    FINISHED,
    Approval,
    ApprovalStatus,
    DraftingStore,
    Run,
    RunStatus,
)
from ementa_kit.jobs import Job, JobStatus
from ementa_kit.memory.episodes import EXAMPLES, Episode, Episodes, edit_diff
from ementa_kit.telemetry import get_logger, get_tracer

DRAFTING_JOB = "run_drafting"
DECISION_EVENT = "approval.decide"

# Namespace of the run ids drawn from an Idempotency-Key: the same key, user and agent give the
# same run.
_RUN_KEYS = uuid.UUID("5f0c8a52-3a3e-4f41-9d0e-6b1c2d7e4a90")

tracer = get_tracer(__name__)
log = get_logger(__name__)


class DraftingAgent(Protocol):
    """A drafting agent the runner can start and resume, such as `brief_drafting`.

    `graph` is compiled with the Postgres checkpointer and calls `ask_for_approval` before it
    finalizes anything. The runner starts it with `build_input(request, examples)`: `request` is
    the JSON the route accepted, `examples` the agent's past episodes closest to
    `example_query(request)`, the closest first, with markers in place of personal data. When
    the graph ends, `result(values)` turns its final state into what the run returns, as JSON.
    """

    @property
    def id(self) -> str: ...

    @property
    def graph(self) -> CompiledStateGraph[Any, Any, Any, Any]: ...

    def example_query(self, request: Mapping[str, Any]) -> str: ...

    def build_input(
        self, request: Mapping[str, Any], examples: Sequence[Episode]
    ) -> dict[str, Any]: ...

    def result(self, values: Mapping[str, Any]) -> dict[str, Any]: ...


class DraftingRunError(RuntimeError):
    """A run that cannot go on, however many times it is tried: an unknown run or agent, a
    resume without a decision, an interrupt that is not an approval request, a graph that ended
    without asking for approval, a result that is not JSON. The run fails with this message."""


@dataclass(frozen=True)
class Progress:
    run: Run
    pending: list[Approval]  # what the run waits on; empty once it ended

    def job_result(self) -> dict[str, Any]:
        """What the job of the leg records: the run, its status and the approvals it waits on.
        The result of the run itself stays in the run."""
        return {
            "run_id": self.run.id,
            "status": str(self.run.status),
            "approvals": [approval.id for approval in self.pending],
        }


def _config(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}}


class DraftingRunner:
    """Moves drafting runs along, in the worker. `agents` maps an agent id to the agent;
    `episodes`, when given, feeds each start up to `examples` past episodes and keeps each
    approved or edited draft as a new one."""

    def __init__(
        self,
        agents: Mapping[str, DraftingAgent],
        store: DraftingStore,
        episodes: Episodes | None = None,
        *,
        examples: int = EXAMPLES,
    ) -> None:
        self.agents = agents
        self.store = store
        self.episodes = episodes
        self.examples = examples

    def advance(self, run_id: str, approval_id: str | None = None) -> Progress:
        """Move the run as far as it goes: start it (`approval_id` None) or resume it with the
        decision recorded on `approval_id`, until it stops for approval or ends."""
        with self.store.lock(run_id):
            run = self.store.run(run_id)
            if run is None:
                raise DraftingRunError(f"There is no run {run_id}.")
            if run.status in FINISHED:
                return Progress(run, [])
            # A leg that cannot be this run's fails alone: the run still waits on its approval.
            approval = self._decided(run, approval_id) if approval_id is not None else None
            try:
                return self._advance(run, approval)
            except DraftingRunError as error:
                self.store.update_run(run.id, RunStatus.FAILED, error=str(error))
                raise

    def fail(self, run_id: str, error: str) -> None:
        """Mark the run failed for good: the worker ran out of tries."""
        self.store.update_run(run_id, RunStatus.FAILED, error=error)

    def _advance(self, run: Run, approval: Approval | None) -> Progress:
        agent = self.agents.get(run.agent)
        if agent is None:
            raise DraftingRunError(f"No drafting agent {run.agent!r} is installed.")
        attributes: dict[str, str] = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": run.agent,
            "gen_ai.conversation.id": run.id,
        }
        if approval is not None and approval.decision is not None:
            attributes["ementa.approval.id"] = approval.id
            attributes["ementa.approval.action"] = approval.decision.action
        with tracer.start_as_current_span(
            f"invoke_agent {run.agent}", attributes=attributes
        ) as span:
            self.store.update_run(run.id, RunStatus.RUNNING)
            graph, config = agent.graph, _config(run.id)
            snapshot = graph.get_state(config)
            waiting = {interrupt.id for interrupt in snapshot.interrupts}
            if approval is not None and approval.interrupt_id in waiting:
                assert approval.decision is not None  # `_decided` checked it
                # Addressed to its interrupt: should the run be waiting on another one by now,
                # this decision cannot answer it.
                resume = {approval.interrupt_id: approval.decision.model_dump()}
                graph.invoke(Command(resume=resume), config)
            elif approval is None and snapshot.created_at is None:  # a new thread
                examples = self._examples(agent, run.input)
                graph.invoke(agent.build_input(run.input, examples), config)
            elif snapshot.next and not waiting:  # a failure stopped it halfway
                graph.invoke(None, config)
            progress = self._settle(run, agent, config)
            span.set_attribute("ementa.run.status", str(progress.run.status))
            if approval is not None:
                self._remember(approval)
        return progress

    def _decided(self, run: Run, approval_id: str) -> Approval:
        approval = self.store.approval(approval_id)
        if approval is None or approval.thread_id != run.id:
            raise DraftingRunError(f"Run {run.id} has no approval {approval_id}.")
        if approval.decision is None:
            raise DraftingRunError(
                f"Approval {approval_id} is not decided: a run resumes only with a decision."
            )
        return approval

    def _examples(self, agent: DraftingAgent, request: Mapping[str, Any]) -> list[Episode]:
        """Past episodes for the start. A store that fails costs the run its examples, not the
        run."""
        if self.episodes is None or self.examples <= 0:
            return []
        query = agent.example_query(request)
        try:
            return self.episodes.examples_for(agent.id, query, self.examples)
        except Exception as error:
            log.warning("episodes_read_failed", agent=agent.id, error=type(error).__name__)
            return []

    def _settle(self, run: Run, agent: DraftingAgent, config: RunnableConfig) -> Progress:
        """Record where the graph stopped: the approvals it waits on, or how it ended."""
        snapshot = agent.graph.get_state(config)
        if snapshot.interrupts:
            pending = [self._open(run, interrupt) for interrupt in snapshot.interrupts]
            self.store.update_run(run.id, RunStatus.AWAITING_APPROVAL)
            return Progress(replace(run, status=RunStatus.AWAITING_APPROVAL), pending)
        if snapshot.next:  # a breakpoint, not a question: another try would only step past it
            raise DraftingRunError(
                f"{run.agent} stopped before its end without asking for approval."
            )
        last = self.store.last_decided(run.id)
        if last is None:
            raise DraftingRunError(
                "The agent ended without asking for approval, so nothing was finalized."
            )
        status = (
            RunStatus.REJECTED if last.status is ApprovalStatus.REJECTED else RunStatus.FINALIZED
        )
        result = agent.result(snapshot.values)
        try:
            json.dumps(result)
        except (TypeError, ValueError) as error:
            raise DraftingRunError(f"The result of {run.agent} is not JSON: {error}") from error
        self.store.update_run(run.id, status, result=result)
        return Progress(replace(run, status=status, result=result), [])

    def _open(self, run: Run, interrupt: Interrupt) -> Approval:
        try:
            request = ApprovalRequest.model_validate(interrupt.value)
        except ValidationError as error:
            raise DraftingRunError(
                f"{run.agent} stopped on an interrupt that is not an approval request."
            ) from error
        if request.agent != run.agent:
            raise DraftingRunError(
                f"The approval request names the agent {request.agent!r}; the run is of "
                f"{run.agent!r}."
            )
        return self.store.open_approval(run.id, interrupt.id, request, run.requested_by)

    def _remember(self, approval: Approval) -> None:
        """Keep an approved or edited draft as an episode. The run already ended well: a failure
        is logged and the run stays as it is."""
        final = approval.final_text
        if self.episodes is None or final is None or approval.decision is None:
            return
        request = approval.request
        try:
            self.episodes.record(
                approval.id,
                agent=request.agent,
                kind=request.kind,
                title=request.title,
                draft=request.draft,
                final=final,
                action=approval.decision.action,
                comment=approval.decision.comment,
                decided_by=approval.decision.decided_by,
            )
        except Exception as error:
            log.warning("episode_write_failed", agent=request.agent, error=type(error).__name__)


class JobSubmitter(Protocol):
    """The part of `PgJobStore` the intake uses."""

    def submit(self, kind: str, subject: str, requested_by: str) -> Job: ...


class JobSender(Protocol):
    """The part of `JobQueue` the intake uses."""

    async def enqueue(self, kind: str, job_id: str, **kwargs: Any) -> bool: ...


class UnknownAgentError(LookupError):
    """No drafting agent with this id is installed."""


class IdempotencyKeyReusedError(ValueError):
    """The Idempotency-Key already started a run with another request."""


class ApprovalNotFoundError(LookupError):
    pass


class AlreadyDecidedError(RuntimeError):
    """The approval was decided before, otherwise: a decision is final."""

    def __init__(self, approval: Approval) -> None:
        super().__init__(f"Approval {approval.id} is already {approval.status}.")
        self.approval = approval


@dataclass(frozen=True)
class Started:
    run: Run
    job: Job
    duplicate: bool  # an Idempotency-Key seen before: the run already existed


@dataclass(frozen=True)
class Decided:
    approval: Approval
    job: Job
    repeated: bool  # the same decision sent again: nothing new was recorded


def check_decision(decision: ApprovalDecision) -> None:
    """An edit carries the edited text; an approve or a reject carries none."""
    if decision.action == "edit":
        if not (decision.text or "").strip():
            raise ValueError("An edit needs the edited text.")
    elif decision.text is not None:
        raise ValueError(f"A {decision.action} takes no text; to change the draft, edit it.")


def run_id_for(requested_by: str, agent: str, idempotency_key: str | None) -> str:
    """A new run id, or with an Idempotency-Key, the id that key always gives this user and
    agent."""
    if idempotency_key is None:
        return str(uuid.uuid4())
    return str(uuid.uuid5(_RUN_KEYS, "\n".join((requested_by, agent, idempotency_key))))


class DraftingIntake:
    """What the drafting routes run: record a run or a decision, then queue the leg that moves
    the run. `agents` are the ids the worker has an agent for. Every decision is an audit event
    in `audit`."""

    def __init__(
        self,
        store: DraftingStore,
        jobs: JobSubmitter,
        queue: JobSender,
        audit: AuditLog,
        agents: Collection[str],
    ) -> None:
        self.store = store
        self.jobs = jobs
        self.queue = queue
        self.audit = audit
        self.agents = frozenset(agents)

    async def start(
        self,
        agent: str,
        request: Mapping[str, Any],
        requested_by: str,
        idempotency_key: str | None = None,
    ) -> Started:
        """Record a run of `agent` on `request` and queue its start. With an Idempotency-Key,
        sending the same request again returns the same run, and a run that had failed is
        queued again; the key with another request is refused."""
        if agent not in self.agents:
            raise UnknownAgentError(agent)
        run_id = run_id_for(requested_by, agent, idempotency_key)
        run, created = await asyncio.to_thread(
            self.store.create_run, run_id, agent, requested_by, request
        )
        if not created and run.input != dict(request):
            raise IdempotencyKeyReusedError(run.id)
        job = await asyncio.to_thread(self.jobs.submit, DRAFTING_JOB, run.id, requested_by)
        requeued = await asyncio.to_thread(self._requeue, run.id, job)
        run = requeued or run
        if job.status is not JobStatus.SUCCEEDED:
            await self.queue.enqueue(DRAFTING_JOB, job.id, run_id=run.id)
        return Started(run, job, duplicate=not created)

    async def decide(self, approval_id: str, decision: ApprovalDecision) -> Decided:
        """Record the lawyer's decision and queue the resume of the run.

        A decision is final: another one raises `AlreadyDecidedError`. The same decision by the
        same person again is a retry (the queue was down, or the resume failed for good): it
        records nothing and queues the resume once more.
        """
        check_decision(decision)
        approval, repeated = await asyncio.to_thread(self._record, approval_id, decision)
        if not repeated:
            await asyncio.to_thread(self._audit, approval)
        job = await asyncio.to_thread(
            self.jobs.submit, DRAFTING_JOB, approval.id, decision.decided_by
        )
        await asyncio.to_thread(self._requeue, approval.thread_id, job)
        if job.status is not JobStatus.SUCCEEDED:
            await self.queue.enqueue(
                DRAFTING_JOB, job.id, run_id=approval.thread_id, approval_id=approval.id
            )
        return Decided(approval, job, repeated)

    def approval(self, approval_id: str) -> Approval | None:
        return self.store.approval(approval_id)

    def approvals(
        self,
        status: ApprovalStatus,
        *,
        requested_by: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> list[Approval]:
        return self.store.approvals(status, requested_by=requested_by, after=after, limit=limit)

    def run(self, run_id: str) -> Run | None:
        return self.store.run(run_id)

    def _requeue(self, run_id: str, job: Job) -> Run | None:
        """A job that failed for good goes back to the queue on a retry, and its run with it;
        the run as it is now, or None when nothing changed."""
        run = self.store.run(run_id)
        if run is None or job.status is not JobStatus.QUEUED or run.status is not RunStatus.FAILED:
            return None
        self.store.update_run(run.id, RunStatus.QUEUED)
        return replace(run, status=RunStatus.QUEUED, error=None, finished_at=None)

    def _record(self, approval_id: str, decision: ApprovalDecision) -> tuple[Approval, bool]:
        current = self.store.approval(approval_id)
        if current is None:
            raise ApprovalNotFoundError(approval_id)
        draft = current.request.draft
        # An approve diffs the draft with itself: empty, the draft went through as written.
        diff = None if decision.action == "reject" else edit_diff(draft, decision.text or draft)
        decided = self.store.decide(approval_id, decision, diff)
        if decided is not None:
            return decided, False
        latest = self.store.approval(approval_id)
        assert latest is not None  # approvals are never deleted
        if latest.decision == decision:
            return latest, True
        raise AlreadyDecidedError(latest)

    def _audit(self, approval: Approval) -> None:
        """The decision as an audit event: who, which approval and run, and the action, never
        the text or the comment. A failed write is logged; the decision stands."""
        decision = approval.decision
        assert decision is not None
        event = AuditEvent(
            actor=decision.decided_by,
            action=DECISION_EVENT,
            resource=approval.request.agent,
            data_class=str(approval.request.data_class),
            outcome=Outcome.OK,
            details={
                "approval_id": approval.id,
                "run_id": approval.thread_id,
                "kind": approval.request.kind,
                "action": decision.action,
                "requested_by": approval.requested_by,
            },
        )
        try:
            self.audit.append(event)
        except Exception:
            log.exception("audit_write_failed", resource=event.resource, action=DECISION_EVENT)
