"""Who may do what in the firm: the RBAC matrix of docs/SEGURANCA-E-GOVERNANCA.md, in code.

One table, role to the REST routes and the agent tools it may use. The API reads it for every
route (`ementa_api.auth.authorized_user`) and the agents read it before a tool call
(`can_use_tool`). Whatever the table does not grant is denied, so a new route or tool stays
closed until it gets a line here.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Role(StrEnum):
    """Roles of the fictitious firm."""

    SOCIA = "socia"
    ASSOCIADO = "associado"
    ESTAGIARIO = "estagiario"
    CONTROLADORIA = "controladoria"
    DPO = "dpo"
    TI = "ti"


# Routes are "METHOD /path", with the path template the API declares.
TOKEN_ROUTE = "POST /v1/auth/token"
LEGAL_RESEARCH_ROUTE = "POST /v1/legal-research"
AUDIT_EVENTS_ROUTE = "GET /v1/audit/events"
FORGET_ROUTE = "DELETE /v1/data-subjects/{subject_id}/memories"
CHAT_ROUTE = "POST /v1/chat"
THREAD_ROUTE = "GET /v1/threads/{thread_id}"
DOCUMENTS_ROUTE = "POST /v1/documents"
JOB_ROUTE = "GET /v1/jobs/{job_id}"
BRIEFS_ROUTE = "POST /v1/briefs"
CONTRACT_REVIEW_ROUTE = "POST /v1/contracts/{document_id}/review"
APPROVALS_ROUTE = "GET /v1/approvals"
APPROVAL_ROUTE = "GET /v1/approvals/{approval_id}"
DECIDE_ROUTE = "POST /v1/approvals/{approval_id}"

OPEN_ROUTES = frozenset({TOKEN_ROUTE})
"""Routes anyone may call without a token: only the one that hands tokens out."""

PUBLIC_TOOLS = frozenset(
    {
        "legal_research",  # the subagent the assistant delegates research to
        "search_legal_corpus",
        "answer_legal_question",
        "get_statute_article",
        "calculate_deadline",
        "list_skills",
        "load_skill",
    }
)
"""Tools over public data (statutes, súmulas, deadline math). "Pesquisar corpus público" is open
to every role."""


@dataclass(frozen=True)
class Grants:
    routes: frozenset[str]
    tools: frozenset[str]


_RESEARCH = Grants(
    routes=frozenset({LEGAL_RESEARCH_ROUTE, CHAT_ROUTE, THREAD_ROUTE}), tools=PUBLIC_TOOLS
)
# "Ler auditoria": sócia, DPO and TI. The associado may read only their own events, which needs
# a filter by actor the route does not have yet; until then the associado reads none.
_RESEARCH_AND_AUDIT = Grants(routes=_RESEARCH.routes | {AUDIT_EVENTS_ROUTE}, tools=PUBLIC_TOOLS)
# "Pedir esquecimento (LGPD)": the DPO asks and TI executes; nobody else.
_PRIVACY = Grants(routes=_RESEARCH_AND_AUDIT.routes | {FORGET_ROUTE}, tools=PUBLIC_TOOLS)

# "Enviar documento do escritório": the lawyers and the controladoria; the job status is the
# uploader's own (the route answers 404 to anyone else).
_UPLOAD = frozenset({DOCUMENTS_ROUTE, JOB_ROUTE})
# "Aprovar peça ou relatório": the sócia, and the associado on the runs they asked for
# (`may_decide`). Only who may approve starts a draft, so every run has someone to decide it.
_DRAFTING = frozenset(
    {BRIEFS_ROUTE, CONTRACT_REVIEW_ROUTE, APPROVALS_ROUTE, APPROVAL_ROUTE, DECIDE_ROUTE}
)

PERMISSIONS: Mapping[Role, Grants] = MappingProxyType(
    {
        Role.SOCIA: Grants(
            routes=_RESEARCH_AND_AUDIT.routes | _UPLOAD | _DRAFTING, tools=PUBLIC_TOOLS
        ),
        Role.ASSOCIADO: Grants(routes=_RESEARCH.routes | _UPLOAD | _DRAFTING, tools=PUBLIC_TOOLS),
        Role.ESTAGIARIO: Grants(routes=_RESEARCH.routes | _UPLOAD, tools=PUBLIC_TOOLS),
        Role.CONTROLADORIA: Grants(routes=_RESEARCH.routes | _UPLOAD, tools=PUBLIC_TOOLS),
        Role.DPO: _PRIVACY,
        Role.TI: _PRIVACY,
    }
)


def _known(role: Role | str) -> Role | None:
    try:
        return Role(role)
    except ValueError:
        return None


def can_call_route(role: Role | str, route: str) -> bool:
    """Whether `role` may call `route`, as "GET /v1/audit/events". An unknown role may not."""
    known = _known(role)
    return known is not None and route in PERMISSIONS[known].routes


def can_use_tool(role: Role | str, tool: str) -> bool:
    """Whether an agent acting for `role` may call `tool`. Unknown roles and tools are denied."""
    known = _known(role)
    return known is not None and tool in PERMISSIONS[known].tools


def decides_every_run(role: Role | str) -> bool:
    """Whether `role` decides the approvals of every run, not only its own: the sócia."""
    return _known(role) is Role.SOCIA


def may_decide(role: Role | str, user_id: str, requested_by: str) -> bool:
    """Whether the user may see and decide an approval of a run `requested_by` asked for: the
    sócia always, the associado on their own runs, nobody else."""
    if decides_every_run(role):
        return True
    return _known(role) is Role.ASSOCIADO and bool(user_id) and user_id == requested_by
