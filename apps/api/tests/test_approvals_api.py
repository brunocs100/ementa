"""The drafting routes and the approvals inbox over HTTP, end to end on a fake graph.

The queue runs each leg at once with the real runner, as the worker would; the stores are the
in-memory ones. No model, no database. Every name and CPF here is fictitious.
"""

import asyncio
import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, TypedDict

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.memory import InMemoryStore

from ementa.agents.legal_research import ResearchAnswer
from ementa.documents import Document, MediaType
from ementa.pii import pii_guard
from ementa_api.app import create_app
from ementa_api.auth import Role, User, create_access_token
from ementa_kit.audit import MemoryAuditLog
from ementa_kit.hitl import ApprovalRequest, ask_for_approval
from ementa_kit.hitl.runner import DECISION_EVENT, DraftingIntake, DraftingRunner
from ementa_kit.hitl.store import MemoryDraftingStore
from ementa_kit.jobs import Job, JobStatus, QueueUnavailableError
from ementa_kit.memory import Episode, Episodes, LocalEmbeddings
from ementa_kit.policy import DataClass

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
FACTS = "Banco cobrou tarifa de pacote de serviços sem contrato assinado. Caso fictício."
ASSOCIADO = User(id="u-associado", name="Rafael Moreira", role=Role.ASSOCIADO)
OUTRO = User(id="u-outro", name="Associado Fictício", role=Role.ASSOCIADO)
SOCIA = User(id="u-socia", name="Helena Duarte", role=Role.SOCIA)


class Draft(TypedDict, total=False):
    facts: str
    examples: list[dict[str, str]]
    draft: str
    final: str | None


class FakeAgent:
    """Drafts from the request and the examples, asks for approval, finalizes what the lawyer
    let through. No model."""

    def __init__(self, agent_id: str, kind: str) -> None:
        self.id = agent_id
        self.kind = kind
        graph = StateGraph(Draft)
        graph.add_node("write", self.write)
        graph.add_node("review", self.review)
        graph.add_edge(START, "write")
        graph.add_edge("write", "review")
        graph.add_edge("review", END)
        self.graph: CompiledStateGraph[Any, Any, Any, Any] = graph.compile(
            checkpointer=InMemorySaver()
        )

    def write(self, state: Draft) -> Draft:
        examples = [f"Exemplo aprovado: {e['final']}" for e in state.get("examples", [])]
        return {"draft": "\n".join([f"Rascunho: {state['facts']}", *examples])}

    def review(self, state: Draft) -> Draft:
        request = ApprovalRequest(
            agent=self.id,
            kind=self.kind,
            title="Peça fictícia",
            draft=state["draft"],
            summary="Confira os fatos.",
        )
        decision = ask_for_approval(request)
        if decision.action == "reject":
            return {"final": None}
        return {"final": decision.text if decision.action == "edit" else state["draft"]}

    def example_query(self, request: Mapping[str, Any]) -> str:
        return str(request.get("facts", ""))

    def build_input(
        self, request: Mapping[str, Any], examples: Sequence[Episode]
    ) -> dict[str, Any]:
        facts = request.get("facts") or f"contrato {request['document_id']}"
        return {"facts": facts, "examples": [{"final": e.final} for e in examples]}

    def result(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {"text": values.get("final")}


class FakeJobs:
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
    """Runs the leg at once with the runner, as the worker would."""

    def __init__(self, runner: DraftingRunner, jobs: FakeJobs) -> None:
        self.runner = runner
        self.jobs = jobs
        self.down = False

    async def enqueue(self, kind: str, job_id: str, **kwargs: Any) -> bool:
        if self.down:
            raise QueueUnavailableError("Redis did not take the job")
        await asyncio.to_thread(self.runner.advance, kwargs["run_id"], kwargs.get("approval_id"))
        self.jobs.finish(job_id, JobStatus.SUCCEEDED)
        return True


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


class FirmDocuments:
    def __init__(self) -> None:
        self.documents: dict[str, Document] = {}

    def add(self, owner: User) -> str:
        document_id = str(uuid.uuid4())
        self.documents[document_id] = Document(
            id=document_id,
            uploaded_by=owner.id,
            owner_id=owner.id,
            acl=[owner.id],
            secrecy_level=DataClass.RESTRICTED,
            sha256="0" * 64,
            filename="contrato.md",
            media_type=MediaType.MARKDOWN,
            size_bytes=10,
            chunks=None,
            created_at=NOW,
            indexed_at=None,
        )
        return document_id

    def get(self, document_id: str) -> Document | None:
        return self.documents.get(document_id)


class NoResearch:
    def ask(self, question: str) -> ResearchAnswer:
        raise AssertionError("not used here")


@dataclass
class Firm:
    store: MemoryDraftingStore
    queue: InlineQueue
    audit: MemoryAuditLog
    documents: FirmDocuments
    intake: DraftingIntake

    def client(self, user: User) -> TestClient:
        app = create_app(
            researcher=NoResearch,
            chunks=lambda: 1,
            audit=lambda: self.audit,
            drafting=lambda: self.intake,
            firm_documents=lambda: self.documents,
        )
        token = create_access_token(user)
        return TestClient(
            app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {token}"}
        )


def firm(agents: Sequence[str] = ("brief_drafting", "contract_review")) -> Firm:
    embeddings = LocalEmbeddings(HashEmbedder())
    memory = InMemoryStore(
        index={"dims": HashEmbedder.dim, "embed": embeddings, "fields": ["text"]}
    )
    store, jobs, audit = MemoryDraftingStore(), FakeJobs(), MemoryAuditLog()
    installed = {
        "brief_drafting": FakeAgent("brief_drafting", "brief"),
        "contract_review": FakeAgent("contract_review", "contract_report"),
    }
    runner = DraftingRunner(installed, store, Episodes(memory, pii_guard, subject_key=b"test"))
    queue = InlineQueue(runner, jobs)
    intake = DraftingIntake(store, jobs, queue, audit, agents=agents)
    return Firm(store, queue, audit, FirmDocuments(), intake)


def pending_id(http: TestClient) -> str:
    [item] = http.get("/v1/approvals").json()["items"]
    return str(item["id"])


def test_a_brief_goes_from_start_to_final_through_the_inbox() -> None:
    f = firm()
    http = f.client(ASSOCIADO)

    started = http.post("/v1/briefs", json={"facts": FACTS})

    assert started.status_code == 202
    body = started.json()
    assert set(body) == {"run_id", "job_id", "status", "duplicate"}
    assert started.headers["location"] == f"/v1/jobs/{body['job_id']}"
    [item] = http.get("/v1/approvals?status=pending").json()["items"]
    assert (item["run_id"], item["agent"], item["kind"]) == (
        body["run_id"],
        "brief_drafting",
        "brief",
    )
    assert (item["requested_by"], item["status"], item["data_class"]) == (
        ASSOCIADO.id,
        "pending",
        "restricted",
    )
    assert "draft" not in item  # the inbox lists; the draft is in the approval itself

    before = http.get(f"/v1/approvals/{item['id']}").json()
    assert before["draft"] == f"Rascunho: {FACTS}"
    assert (before["decision"], before["final_text"], before["diff"]) == (None, None, None)
    assert before["run"]["status"] == "awaiting_approval"
    assert before["run"]["result"] is None  # nothing is final before the decision

    edited = f"Rascunho: {FACTS}\nPede a devolução em dobro da tarifa."
    decided = http.post(
        f"/v1/approvals/{item['id']}",
        json={"action": "edit", "text": edited, "comment": "Faltava o pedido."},
    )

    assert decided.status_code == 202
    approval = decided.json()["approval"]
    assert decided.headers["location"] == f"/v1/jobs/{decided.json()['job_id']}"
    assert (approval["status"], approval["final_text"]) == ("edited", edited)
    assert approval["decision"] == {
        "action": "edit",
        "text": edited,
        "comment": "Faltava o pedido.",
        "decided_by": ASSOCIADO.id,
    }
    assert approval["diff"].endswith("+Pede a devolução em dobro da tarifa.")
    assert approval["run"]["status"] == "finalized"
    assert approval["run"]["result"] == {"text": edited}
    assert http.get(f"/v1/approvals/{item['id']}").json() == approval
    assert http.get("/v1/approvals").json() == {"items": [], "next_cursor": None}
    assert [a["id"] for a in http.get("/v1/approvals?status=edited").json()["items"]] == [
        item["id"]
    ]

    [decision] = [r.event for r in f.audit.records if r.event.action == DECISION_EVENT]
    assert (decision.actor, decision.resource, decision.outcome) == (
        ASSOCIADO.id,
        "brief_drafting",
        "ok",
    )
    assert decision.details["approval_id"] == item["id"]
    assert decision.details["action"] == "edit"
    requests = [r.event.resource for r in f.audit.records if r.event.action == "http.request"]
    assert "POST /v1/approvals/{approval_id}" in requests
    assert f.audit.verify().intact


def test_the_lawyers_edit_shows_up_as_an_example_in_the_next_run() -> None:
    f = firm()
    http = f.client(ASSOCIADO)
    http.post("/v1/briefs", json={"facts": FACTS})
    edited = "Maria Souza, CPF 123.456.789-09, pede a devolução em dobro da tarifa sem contrato."
    http.post(f"/v1/approvals/{pending_id(http)}", json={"action": "edit", "text": edited})

    http.post("/v1/briefs", json={"facts": "Tarifa cobrada sem contrato. Outro caso fictício."})

    draft = http.get(f"/v1/approvals/{pending_id(http)}").json()["draft"]
    assert re.search(
        r"Exemplo aprovado: \[PESSOA_\d\], CPF \[CPF_1\], pede a devolução em dobro", draft
    )
    assert "Maria Souza" not in draft and "123.456.789-09" not in draft


@pytest.mark.parametrize("role", [Role.ESTAGIARIO, Role.CONTROLADORIA, Role.DPO, Role.TI])
def test_only_the_socia_and_the_associado_draft_and_decide(role: Role) -> None:
    f = firm()
    f.client(ASSOCIADO).post("/v1/briefs", json={"facts": FACTS})
    approval_id = pending_id(f.client(ASSOCIADO))
    http = f.client(User(id=f"u-{role}", name="Pessoa Fictícia", role=role))

    responses = [
        http.post("/v1/briefs", json={"facts": FACTS}),
        http.post(f"/v1/contracts/{uuid.uuid4()}/review"),
        http.get("/v1/approvals"),
        http.get(f"/v1/approvals/{approval_id}"),
        http.post(f"/v1/approvals/{approval_id}", json={"action": "approve"}),
    ]

    assert [r.status_code for r in responses] == [403] * 5
    assert all(r.headers["content-type"] == "application/problem+json" for r in responses)
    pending = f.store.approval(approval_id)
    assert pending is not None and pending.decision is None


def test_an_associado_sees_and_decides_only_their_own_runs() -> None:
    f = firm()
    mine = f.client(ASSOCIADO)
    mine.post("/v1/briefs", json={"facts": FACTS})
    approval_id = pending_id(mine)
    other, socia = f.client(OUTRO), f.client(SOCIA)

    assert other.get("/v1/approvals").json()["items"] == []
    hidden = other.get(f"/v1/approvals/{approval_id}")
    unknown = other.get(f"/v1/approvals/{uuid.uuid4()}")
    refused = other.post(f"/v1/approvals/{approval_id}", json={"action": "approve"})

    assert hidden.status_code == unknown.status_code == refused.status_code == 404
    assert hidden.json() == unknown.json()  # nothing tells them apart
    assert hidden.json()["instance"] == "/v1/approvals/{approval_id}"
    assert [i["id"] for i in socia.get("/v1/approvals").json()["items"]] == [approval_id]
    decided = socia.post(f"/v1/approvals/{approval_id}", json={"action": "approve"})
    assert decided.status_code == 202
    assert decided.json()["approval"]["decision"]["decided_by"] == SOCIA.id
    assert decided.json()["approval"]["run"]["status"] == "finalized"


def test_a_decision_is_final() -> None:
    f = firm()
    http = f.client(ASSOCIADO)
    http.post("/v1/briefs", json={"facts": FACTS})
    approval_id = pending_id(http)
    http.post(f"/v1/approvals/{approval_id}", json={"action": "reject", "comment": "Refazer."})

    other = http.post(f"/v1/approvals/{approval_id}", json={"action": "approve"})
    same = http.post(
        f"/v1/approvals/{approval_id}", json={"action": "reject", "comment": "Refazer."}
    )

    assert other.status_code == 409
    assert other.headers["content-type"] == "application/problem+json"
    assert other.json()["type"] == "/problems/approval-decided"
    assert other.json()["detail"] == "The approval is already rejected: decisions are final."
    assert same.status_code == 202  # a retry of the same decision
    approval = same.json()["approval"]
    assert (approval["status"], approval["final_text"], approval["diff"]) == (
        "rejected",
        None,
        None,
    )
    assert approval["run"]["status"] == "rejected"
    assert len([r for r in f.audit.records if r.event.action == DECISION_EVENT]) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"action": "edit"},
        {"action": "edit", "text": "  "},
        {"action": "approve", "text": "outro texto"},
        {"action": "aprovar"},
        {"action": "approve", "decided_by": "u-socia"},
    ],
    ids=["edit-without-text", "edit-blank", "approve-with-text", "unknown-action", "decided-by"],
)
def test_a_decision_out_of_shape_is_a_validation_problem(body: dict[str, Any]) -> None:
    f = firm()
    http = f.client(ASSOCIADO)
    http.post("/v1/briefs", json={"facts": FACTS})
    approval_id = pending_id(http)

    response = http.post(f"/v1/approvals/{approval_id}", json=body)

    assert response.status_code == 422
    assert response.json()["type"] == "/problems/validation-error"
    assert "outro texto" not in response.text
    pending = f.store.approval(approval_id)
    assert pending is not None and pending.decision is None


def test_queue_down_keeps_the_decision_and_the_same_one_again_resumes_the_run() -> None:
    f = firm()
    http = f.client(ASSOCIADO)
    http.post("/v1/briefs", json={"facts": FACTS})
    approval_id = pending_id(http)
    f.queue.down = True

    down = http.post(f"/v1/approvals/{approval_id}", json={"action": "approve"})
    f.queue.down = False
    again = http.post(f"/v1/approvals/{approval_id}", json={"action": "approve"})

    assert down.status_code == 503
    assert down.json()["type"] == "/problems/queue-unavailable"
    assert "send the same decision again" in down.json()["detail"]
    assert again.status_code == 202
    assert again.json()["approval"]["run"]["status"] == "finalized"


def test_the_same_idempotency_key_is_the_same_run() -> None:
    f = firm()
    http = f.client(ASSOCIADO)
    key = {"Idempotency-Key": "peticao-0001"}

    first = http.post("/v1/briefs", json={"facts": FACTS}, headers=key)
    again = http.post("/v1/briefs", json={"facts": FACTS}, headers=key)
    reused = http.post("/v1/briefs", json={"facts": "Outros fatos."}, headers=key)

    assert first.status_code == again.status_code == 202
    assert again.json()["run_id"] == first.json()["run_id"]
    assert (first.json()["duplicate"], again.json()["duplicate"]) == (False, True)
    assert again.json()["status"] == "awaiting_approval"
    assert reused.status_code == 422
    assert reused.json()["type"] == "/problems/idempotency-key-reused"
    assert len(f.store.runs) == 1
    bad_key = http.post("/v1/briefs", json={"facts": FACTS}, headers={"Idempotency-Key": "a b"})
    assert bad_key.status_code == 422


def test_a_contract_review_runs_on_a_document_of_the_caller() -> None:
    f = firm()
    own = f.documents.add(ASSOCIADO)
    others = f.documents.add(OUTRO)
    http = f.client(ASSOCIADO)

    started = http.post(f"/v1/contracts/{own}/review")
    stranger = http.post(f"/v1/contracts/{others}/review")
    unknown = http.post(f"/v1/contracts/{uuid.uuid4()}/review")
    malformed = http.post("/v1/contracts/123.456.789-09/review")

    assert started.status_code == 202
    run = f.store.run(started.json()["run_id"])
    assert run is not None and (run.agent, run.input) == ("contract_review", {"document_id": own})
    [item] = http.get("/v1/approvals").json()["items"]
    assert (item["agent"], item["kind"]) == ("contract_review", "contract_report")
    assert stranger.status_code == unknown.status_code == 404
    assert stranger.json() == unknown.json()
    assert malformed.status_code == 422
    assert "123.456.789-09" not in malformed.text


def test_an_agent_not_installed_is_503_and_nothing_is_stored() -> None:
    f = firm(agents=[])

    response = f.client(SOCIA).post("/v1/briefs", json={"facts": FACTS})

    assert response.status_code == 503
    assert response.json() == {
        "type": "/problems/agent-unavailable",
        "title": "Agent unavailable",
        "status": 503,
        "detail": "The brief_drafting agent is not installed here.",
        "instance": "/v1/briefs",
    }
    assert f.store.runs == {}


def test_the_brief_request_is_kept_as_the_route_accepted_it() -> None:
    f = firm()
    case_id = str(uuid.uuid4())

    started = f.client(ASSOCIADO).post(
        "/v1/briefs", json={"facts": FACTS, "kind": "contestacao", "case_id": case_id}
    )
    unknown = f.client(ASSOCIADO).post("/v1/briefs", json={"facts": FACTS, "cpf": "1"})

    run = f.store.run(started.json()["run_id"])
    assert run is not None
    assert run.input == {"facts": FACTS, "kind": "contestacao", "case_id": case_id}
    assert json.loads(json.dumps(run.input)) == run.input
    assert unknown.status_code == 422


def test_the_inbox_pages_oldest_first_with_a_cursor() -> None:
    f = firm()
    http = f.client(SOCIA)
    for n in range(3):
        http.post("/v1/briefs", json={"facts": f"Fatos fictícios {n}."})

    first = http.get("/v1/approvals?limit=2").json()
    rest = http.get(f"/v1/approvals?limit=2&cursor={first['next_cursor']}").json()

    assert [i["id"] for i in first["items"]] == [a.id for a in f.store.approvals_by_id.values()][:2]
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    assert http.get("/v1/approvals?status=aprovado").status_code == 422
    assert http.get("/v1/approvals?cursor=12345678909").status_code == 422
