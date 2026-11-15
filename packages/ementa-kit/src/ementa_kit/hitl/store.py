"""Where drafting runs and their approvals live: `ai.runs` and `ai.approvals` on Postgres, or
dictionaries for tests and processes without a database (F4).

A run is one execution of a drafting agent on a LangGraph thread of its own: the run id is the
thread id. It keeps the request the API accepted, which the worker starts the graph from, the
status and, once the graph ends, the result. An approval is one pause of a run: the
`ApprovalRequest` the agent sent through `ask_for_approval`, keyed by the interrupt it came in.
Once a lawyer decides, it keeps the `ApprovalDecision` that resumes the graph, verbatim, and the
diff from the draft to the final text. A decision is taken once: the update matches only a
pending approval.
"""

import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ementa_kit.hitl import Action, ApprovalDecision, ApprovalRequest
from ementa_kit.policy import DataClass


class RunStatus(StrEnum):
    QUEUED = "queued"  # waiting for the worker: just started, or decided and about to resume
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    FINALIZED = "finalized"  # ended after the lawyer approved or edited the draft
    REJECTED = "rejected"  # ended after the lawyer rejected it
    FAILED = "failed"


FINISHED = frozenset({RunStatus.FINALIZED, RunStatus.REJECTED, RunStatus.FAILED})


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


DECIDED_AS: Mapping[Action, ApprovalStatus] = {
    "approve": ApprovalStatus.APPROVED,
    "reject": ApprovalStatus.REJECTED,
    "edit": ApprovalStatus.EDITED,
}


@dataclass(frozen=True)
class Run:
    id: str  # also the LangGraph thread id
    agent: str
    requested_by: str
    input: dict[str, Any]  # the request as the route accepted it, JSON
    status: RunStatus
    result: dict[str, Any] | None  # what the agent produced, once the graph ended
    error: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class Approval:
    id: str
    thread_id: str  # the run
    interrupt_id: str  # the LangGraph interrupt the request came in
    request: ApprovalRequest
    status: ApprovalStatus
    requested_by: str  # who started the run
    decision: ApprovalDecision | None
    diff: str | None  # from the draft to the final text; None until decided, and on a reject
    created_at: datetime
    decided_at: datetime | None

    @property
    def final_text(self) -> str | None:
        """What the lawyer let through: the draft when approved, their text when edited."""
        if self.decision is None or self.decision.action == "reject":
            return None
        if self.decision.action == "edit" and self.decision.text is not None:
            return self.decision.text
        return self.request.draft


class DraftingStore(Protocol):
    def create_run(
        self, run_id: str, agent: str, requested_by: str, request: Mapping[str, Any]
    ) -> tuple[Run, bool]:
        """The run with this id, created queued when there is none; and whether this call
        created it."""
        ...

    def run(self, run_id: str) -> Run | None: ...

    def update_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Set the status, with the result and the error (None clears them)."""
        ...

    def lock(self, run_id: str) -> AbstractContextManager[None]:
        """Hold the run while its graph moves: a second worker waits, so two executions never
        share a LangGraph thread."""
        ...

    def open_approval(
        self, thread_id: str, interrupt_id: str, request: ApprovalRequest, requested_by: str
    ) -> Approval:
        """The approval of this interrupt, created pending when there is none."""
        ...

    def approval(self, approval_id: str) -> Approval | None: ...

    def approvals(
        self,
        status: ApprovalStatus,
        *,
        requested_by: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> list[Approval]:
        """Oldest first; only the runs of `requested_by` when given; `after` is the id where the
        previous page stopped."""
        ...

    def decide(
        self, approval_id: str, decision: ApprovalDecision, diff: str | None
    ) -> Approval | None:
        """Record the decision of a pending approval and queue its run again; None when the
        approval is not pending (already decided) or does not exist."""
        ...

    def last_decided(self, thread_id: str) -> Approval | None:
        """The latest decided approval of the run, which says how it ended."""
        ...


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryDraftingStore:
    """The same in dictionaries, for tests and for processes without a database."""

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.approvals_by_id: dict[str, Approval] = {}  # in the order they were opened
        self._lock = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}

    def create_run(
        self, run_id: str, agent: str, requested_by: str, request: Mapping[str, Any]
    ) -> tuple[Run, bool]:
        with self._lock:
            if run_id in self.runs:
                return self.runs[run_id], False
            now = _now()
            run = Run(
                id=run_id,
                agent=agent,
                requested_by=requested_by,
                input=dict(request),
                status=RunStatus.QUEUED,
                result=None,
                error=None,
                created_at=now,
                updated_at=now,
                finished_at=None,
            )
            self.runs[run_id] = run
            return run, True

    def run(self, run_id: str) -> Run | None:
        with self._lock:
            return self.runs.get(run_id)

    def update_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            run = self.runs[run_id]
            now = _now()
            finished = (run.finished_at or now) if status in FINISHED else None
            self.runs[run_id] = replace(
                run,
                status=status,
                result=dict(result) if result is not None else None,
                error=error,
                updated_at=now,
                finished_at=finished,
            )

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        with self._lock:
            held = self._run_locks.setdefault(run_id, threading.Lock())
        with held:
            yield

    def open_approval(
        self, thread_id: str, interrupt_id: str, request: ApprovalRequest, requested_by: str
    ) -> Approval:
        with self._lock:
            for approval in self.approvals_by_id.values():
                if (approval.thread_id, approval.interrupt_id) == (thread_id, interrupt_id):
                    return approval
            approval = Approval(
                str(uuid.uuid4()),
                thread_id,
                interrupt_id,
                request,
                ApprovalStatus.PENDING,
                requested_by,
                None,
                None,
                _now(),
                None,
            )
            self.approvals_by_id[approval.id] = approval
            return approval

    def approval(self, approval_id: str) -> Approval | None:
        with self._lock:
            return self.approvals_by_id.get(approval_id)

    def approvals(
        self,
        status: ApprovalStatus,
        *,
        requested_by: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> list[Approval]:
        with self._lock:
            ordered = list(self.approvals_by_id.values())
        if after is not None:
            ids = [a.id for a in ordered]
            ordered = ordered[ids.index(after) + 1 :] if after in ids else []
        return [
            a
            for a in ordered
            if a.status is status and (requested_by is None or a.requested_by == requested_by)
        ][:limit]

    def decide(
        self, approval_id: str, decision: ApprovalDecision, diff: str | None
    ) -> Approval | None:
        with self._lock:
            approval = self.approvals_by_id.get(approval_id)
            if approval is None or approval.status is not ApprovalStatus.PENDING:
                return None
            now = _now()
            decided = replace(
                approval,
                status=DECIDED_AS[decision.action],
                decision=decision,
                diff=diff,
                decided_at=now,
            )
            self.approvals_by_id[approval_id] = decided
            run = self.runs[approval.thread_id]
            self.runs[run.id] = replace(run, status=RunStatus.QUEUED, updated_at=now)
            return decided

    def last_decided(self, thread_id: str) -> Approval | None:
        with self._lock:
            decided = [
                a
                for a in self.approvals_by_id.values()
                if a.thread_id == thread_id and a.decided_at is not None
            ]
        return max(decided, key=lambda a: a.decided_at or a.created_at, default=None)


_SCHEMA = """
create schema if not exists {schema};
create table if not exists {schema}.runs (
    id uuid primary key,
    agent text not null,
    requested_by text not null,
    input jsonb not null,
    status text not null default 'queued' check (status in
        ('queued', 'running', 'awaiting_approval', 'finalized', 'rejected', 'failed')),
    result jsonb,
    error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    finished_at timestamptz
);
create table if not exists {schema}.approvals (
    id uuid primary key,
    thread_id uuid not null references {schema}.runs (id),
    interrupt_id text not null,
    agent text not null,
    kind text not null,
    title text not null,
    draft text not null,
    summary text not null,
    data_class text not null check (data_class in ('public', 'restricted', 'secret')),
    status text not null default 'pending'
        check (status in ('pending', 'approved', 'rejected', 'edited')),
    requested_by text not null,
    decided_by text,
    decision jsonb,
    diff text,
    created_at timestamptz not null default now(),
    decided_at timestamptz,
    unique (thread_id, interrupt_id),
    check ((status = 'pending') = (decision is null))
);
"""

_RUN_COLUMNS = (
    "id, agent, requested_by, input, status, result, error, created_at, updated_at, finished_at"
)
_APPROVAL_COLUMNS = (
    "id, thread_id, interrupt_id, agent, kind, title, draft, summary, data_class, status, "
    "requested_by, decision, diff, created_at, decided_at"
)


def _run(row: Mapping[str, Any]) -> Run:
    return Run(**{**row, "id": str(row["id"]), "status": RunStatus(row["status"])})


def _approval(row: Mapping[str, Any]) -> Approval:
    request = ApprovalRequest(
        agent=row["agent"],
        kind=row["kind"],
        title=row["title"],
        draft=row["draft"],
        summary=row["summary"],
        data_class=DataClass(row["data_class"]),
    )
    decision = row["decision"]
    return Approval(
        id=str(row["id"]),
        thread_id=str(row["thread_id"]),
        interrupt_id=row["interrupt_id"],
        request=request,
        status=ApprovalStatus(row["status"]),
        requested_by=row["requested_by"],
        decision=ApprovalDecision.model_validate(decision) if decision is not None else None,
        diff=row["diff"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


class PgDraftingStore:
    """`ai.runs` and `ai.approvals` on Postgres."""

    def __init__(self, database_url: str, schema: str = "ai") -> None:
        self.database_url = database_url
        self.schema = schema

    def _sql(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(schema=sql.Identifier(self.schema))

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(
            self.database_url, autocommit=True, row_factory=dict_row, connect_timeout=5
        )

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(self._sql(_SCHEMA))

    def create_run(
        self, run_id: str, agent: str, requested_by: str, request: Mapping[str, Any]
    ) -> tuple[Run, bool]:
        with self._connect() as conn, conn.transaction():
            row = conn.execute(
                self._sql(
                    "insert into {schema}.runs (id, agent, requested_by, input) "
                    f"values (%s, %s, %s, %s) on conflict (id) do nothing returning {_RUN_COLUMNS}"
                ),
                (run_id, agent, requested_by, Jsonb(dict(request))),
            ).fetchone()
            if row is not None:
                return _run(row), True
            row = conn.execute(
                self._sql(f"select {_RUN_COLUMNS} from {{schema}}.runs where id = %s"), (run_id,)
            ).fetchone()
        assert row is not None  # the insert conflicted with this row
        return _run(row), False

    def run(self, run_id: str) -> Run | None:
        if not is_uuid(run_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                self._sql(f"select {_RUN_COLUMNS} from {{schema}}.runs where id = %s"), (run_id,)
            ).fetchone()
        return _run(row) if row else None

    def update_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    "update {schema}.runs set status = %(status)s, result = %(result)s, "
                    "error = %(error)s, updated_at = now(), finished_at = case "
                    "when %(finished)s then coalesce(finished_at, now()) end where id = %(id)s"
                ),
                {
                    "status": str(status),
                    "result": Jsonb(dict(result)) if result is not None else None,
                    "error": error,
                    "finished": status in FINISHED,
                    "id": run_id,
                },
            )

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        """A session advisory lock on a connection of its own, for as long as the block runs.
        Closing the connection releases it too, so a worker that dies holds nothing."""
        key = f"{self.schema}.runs:{run_id}"
        with self._connect() as conn:
            conn.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (key,))
            try:
                yield
            finally:
                conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (key,))

    def open_approval(
        self, thread_id: str, interrupt_id: str, request: ApprovalRequest, requested_by: str
    ) -> Approval:
        with self._connect() as conn, conn.transaction():
            conn.execute(
                self._sql(
                    "insert into {schema}.approvals (id, thread_id, interrupt_id, agent, kind, "
                    "title, draft, summary, data_class, requested_by) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "on conflict (thread_id, interrupt_id) do nothing"
                ),
                (
                    uuid.uuid4(),
                    thread_id,
                    interrupt_id,
                    request.agent,
                    request.kind,
                    request.title,
                    request.draft,
                    request.summary,
                    str(request.data_class),
                    requested_by,
                ),
            )
            row = conn.execute(
                self._sql(
                    f"select {_APPROVAL_COLUMNS} from {{schema}}.approvals "
                    "where thread_id = %s and interrupt_id = %s"
                ),
                (thread_id, interrupt_id),
            ).fetchone()
        assert row is not None  # inserted or found in the same transaction
        return _approval(row)

    def approval(self, approval_id: str) -> Approval | None:
        if not is_uuid(approval_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                self._sql(f"select {_APPROVAL_COLUMNS} from {{schema}}.approvals where id = %s"),
                (approval_id,),
            ).fetchone()
        return _approval(row) if row else None

    def approvals(
        self,
        status: ApprovalStatus,
        *,
        requested_by: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> list[Approval]:
        if after is not None and not is_uuid(after):
            return []
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(
                    f"select {_APPROVAL_COLUMNS} from {{schema}}.approvals "
                    "where status = %(status)s "
                    "and (%(requested_by)s::text is null or requested_by = %(requested_by)s) "
                    "and (%(after)s::uuid is null or (created_at, id) > "
                    "(select created_at, id from {schema}.approvals where id = %(after)s)) "
                    "order by created_at, id limit %(limit)s"
                ),
                {
                    "status": str(status),
                    "requested_by": requested_by,
                    "after": after,
                    "limit": limit,
                },
            ).fetchall()
        return [_approval(row) for row in rows]

    def decide(
        self, approval_id: str, decision: ApprovalDecision, diff: str | None
    ) -> Approval | None:
        if not is_uuid(approval_id):
            return None
        with self._connect() as conn, conn.transaction():
            row = conn.execute(
                self._sql(
                    "update {schema}.approvals set status = %s, decided_by = %s, decision = %s, "
                    "diff = %s, decided_at = now() where id = %s and status = 'pending' "
                    f"returning {_APPROVAL_COLUMNS}"
                ),
                (
                    str(DECIDED_AS[decision.action]),
                    decision.decided_by,
                    Jsonb(decision.model_dump(mode="json")),
                    diff,
                    approval_id,
                ),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                self._sql(
                    "update {schema}.runs set status = 'queued', updated_at = now() where id = %s"
                ),
                (row["thread_id"],),
            )
        return _approval(row)

    def last_decided(self, thread_id: str) -> Approval | None:
        if not is_uuid(thread_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    f"select {_APPROVAL_COLUMNS} from {{schema}}.approvals "
                    "where thread_id = %s and decided_at is not null "
                    "order by decided_at desc limit 1"
                ),
                (thread_id,),
            ).fetchone()
        return _approval(row) if row else None
