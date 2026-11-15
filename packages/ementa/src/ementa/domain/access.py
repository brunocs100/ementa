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

PERMISSIONS: Mapping[Role, Grants] = MappingProxyType(
    {
        Role.SOCIA: _RESEARCH_AND_AUDIT,
        Role.ASSOCIADO: _RESEARCH,
        Role.ESTAGIARIO: _RESEARCH,
        Role.CONTROLADORIA: _RESEARCH,
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
