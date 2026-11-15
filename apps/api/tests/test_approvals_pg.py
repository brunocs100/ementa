"""The approval flow through the API on a real Postgres: `ai.runs`, `ai.approvals`, `ai.jobs`,
the LangGraph checkpointer and store, and `audit.events`, each in a throwaway schema.

The queue runs each leg at once with the real runner and records the job as the worker does.
Set EMENTA_TEST_DATABASE_URL to run. Every name here is fictitious.
"""

import asyncio
import hashlib
import math
import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg import sql

from ementa.agents.legal_research import ResearchAnswer
from ementa.documents import DocumentIntake, PgDocumentStore
from ementa.pii import pii_guard
from ementa_api.app import create_app
from ementa_api.auth import Role, User, create_access_token
from ementa_kit.audit import PgAuditLog
from ementa_kit.hitl import ApprovalRequest, ask_for_approval
from ementa_kit.hitl.runner import DECISION_EVENT, DraftingIntake, DraftingRunner
from ementa_kit.hitl.store import PgDraftingStore
from ementa_kit.jobs import JobQueue, PgJobStore
from ementa_kit.memory import Episode, Episodes, open_postgres_store, postgres_checkpointer

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("EMENTA_TEST_DATABASE_URL", "")
AGENT = "brief_drafting"
FACTS = "Banco cobrou tarifa de pacote de serviços sem contrato assinado. Caso fictício."
ASSOCIADO = User(id="u-associado", name="Rafael Moreira", role=Role.ASSOCIADO)


class Draft(TypedDict, total=False):
    facts: str
    examples: list[str]
    draft: str
    final: str | None


class FakeBriefs:
    id = AGENT

    def __init__(self, checkpointer: BaseCheckpointSaver[Any]) -> None:
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
        examples = [f"Exemplo aprovado: {e}" for e in state.get("examples", [])]
        return {"draft": "\n".join([f"Rascunho: {state['facts']}", *examples])}

    def review(self, state: Draft) -> Draft:
        request = ApprovalRequest(agent=AGENT, kind="brief", title="Peça", draft=state["draft"])
        decision = ask_for_approval(request)
        if decision.action == "reject":
            return {"final": None}
        return {"final": decision.text if decision.action == "edit" else state["draft"]}

    def example_query(self, request: Mapping[str, Any]) -> str:
        return str(request["facts"])

    def build_input(
        self, request: Mapping[str, Any], examples: Sequence[Episode]
    ) -> dict[str, Any]:
        return {"facts": request["facts"], "examples": [e.final for e in examples]}

    def result(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {"text": values.get("final")}


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


class InlineQueue:
    """Runs the leg at once and records the job in `ai.jobs` as the worker does."""

    def __init__(self, runner: DraftingRunner, jobs: PgJobStore) -> None:
        self.runner = runner
        self.jobs = jobs

    async def enqueue(self, kind: str, job_id: str, **kwargs: Any) -> bool:
        self.jobs.start(job_id, 1)
        progress = await asyncio.to_thread(
            self.runner.advance, kwargs["run_id"], kwargs.get("approval_id")
        )
        self.jobs.succeed(job_id, progress.job_result())
        return True


class NoResearch:
    def ask(self, question: str) -> ResearchAnswer:
        raise AssertionError("not used here")


@dataclass
class Firm:
    http: TestClient
    audit: PgAuditLog


@pytest.fixture
def firm() -> Iterator[Firm]:
    if not DATABASE_URL:
        pytest.skip("EMENTA_TEST_DATABASE_URL not set")
    prefix = f"test_{uuid.uuid4().hex[:8]}"
    ai, graphs, audit_schema, firm_schema = (
        f"{prefix}_ai",
        f"{prefix}_langgraph",
        f"{prefix}_audit",
        f"{prefix}_firm",
    )
    store = PgDraftingStore(DATABASE_URL, schema=ai)
    jobs = PgJobStore(DATABASE_URL, schema=ai)
    audit = PgAuditLog(DATABASE_URL, schema=audit_schema)
    documents = PgDocumentStore(DATABASE_URL, schema=firm_schema)
    for table in (store, jobs, audit, documents):
        table.ensure_schema()
    checkpointer = postgres_checkpointer(DATABASE_URL, graphs)
    memory = open_postgres_store(DATABASE_URL, HashEmbedder(), schema=graphs)
    episodes = Episodes(memory, pii_guard, subject_key=b"test")
    runner = DraftingRunner({AGENT: FakeBriefs(checkpointer)}, store, episodes)
    intake = DraftingIntake(store, jobs, InlineQueue(runner, jobs), audit, agents=[AGENT])
    unused = JobQueue("redis://127.0.0.1:1/0")  # /v1/jobs only reads ai.jobs
    app = create_app(
        researcher=NoResearch,
        chunks=lambda: 1,
        audit=lambda: audit,
        documents=lambda: DocumentIntake(documents, jobs, unused),
        drafting=lambda: intake,
        firm_documents=lambda: documents,
    )
    token = create_access_token(ASSOCIADO)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        yield Firm(TestClient(app, raise_server_exceptions=False, headers=headers), audit)
    finally:
        checkpointer.conn.close()
        memory.conn.close()
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            for schema in (ai, graphs, audit_schema, firm_schema):
                conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


def test_the_approval_flow_end_to_end_on_postgres(firm: Firm) -> None:
    http = firm.http

    started = http.post("/v1/briefs", json={"facts": FACTS}, headers={"Idempotency-Key": "p-1"})
    assert started.status_code == 202
    run_id, job_id = started.json()["run_id"], started.json()["job_id"]
    job = http.get(f"/v1/jobs/{job_id}").json()
    [item] = http.get("/v1/approvals").json()["items"]
    assert (job["kind"], job["subject"], job["status"]) == ("run_drafting", run_id, "succeeded")
    assert job["result"] == {
        "run_id": run_id,
        "status": "awaiting_approval",
        "approvals": [item["id"]],
    }
    assert http.get(f"/v1/approvals/{item['id']}").json()["run"]["result"] is None

    edited = f"Rascunho: {FACTS}\nMaria Souza pede a devolução em dobro da tarifa."
    decided = http.post(f"/v1/approvals/{item['id']}", json={"action": "edit", "text": edited})

    assert decided.status_code == 202
    resume = http.get(f"/v1/jobs/{decided.json()['job_id']}").json()
    assert (resume["subject"], resume["status"]) == (item["id"], "succeeded")
    assert resume["result"] == {"run_id": run_id, "status": "finalized", "approvals": []}
    approval = http.get(f"/v1/approvals/{item['id']}").json()
    assert (approval["status"], approval["final_text"]) == ("edited", edited)
    assert approval["run"]["status"] == "finalized"
    assert approval["run"]["result"] == {"text": edited}
    assert approval["run"]["finished_at"] is not None
    events = [r.event for r in firm.audit.page(limit=200)]
    [decision] = [e for e in events if e.action == DECISION_EVENT]
    assert (decision.actor, decision.details["approval_id"]) == (ASSOCIADO.id, item["id"])
    assert firm.audit.verify().intact

    again = http.post("/v1/briefs", json={"facts": FACTS}, headers={"Idempotency-Key": "p-1"})
    assert (again.json()["run_id"], again.json()["duplicate"]) == (run_id, True)

    http.post("/v1/briefs", json={"facts": "Tarifa cobrada sem contrato. Outro caso fictício."})
    [nxt] = http.get("/v1/approvals").json()["items"]
    draft = http.get(f"/v1/approvals/{nxt['id']}").json()["draft"]
    assert re.search(r"Exemplo aprovado: .*\n\[PESSOA_\d\] pede a devolução em dobro", draft)
    assert "Maria Souza" not in draft
