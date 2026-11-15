import pytest

from ementa.domain.access import (
    AUDIT_EVENTS_ROUTE,
    LEGAL_RESEARCH_ROUTE,
    OPEN_ROUTES,
    PERMISSIONS,
    PUBLIC_TOOLS,
    TOKEN_ROUTE,
    Role,
    can_call_route,
    can_use_tool,
)

AUDIT_READERS = {Role.SOCIA, Role.DPO, Role.TI}


def test_every_role_has_a_line() -> None:
    assert set(PERMISSIONS) == set(Role)


@pytest.mark.parametrize("role", list(Role))
def test_every_role_researches_the_public_corpus(role: Role) -> None:
    assert can_call_route(role, LEGAL_RESEARCH_ROUTE)
    assert all(can_use_tool(role, tool) for tool in PUBLIC_TOOLS)


@pytest.mark.parametrize("role", list(Role))
def test_only_socia_dpo_and_ti_read_the_audit_trail(role: Role) -> None:
    assert can_call_route(role, AUDIT_EVENTS_ROUTE) is (role in AUDIT_READERS)


def test_what_is_not_granted_is_denied() -> None:
    assert not can_use_tool(Role.SOCIA, "send_email")
    assert not can_call_route(Role.SOCIA, "DELETE /v1/legal-research")
    assert not can_call_route(Role.SOCIA, "post /v1/legal-research")


def test_roles_arrive_as_text_from_agent_state() -> None:
    assert can_use_tool("estagiario", "calculate_deadline")
    assert can_call_route("dpo", AUDIT_EVENTS_ROUTE)
    assert not can_use_tool("cliente", "calculate_deadline")
    assert not can_call_route("", LEGAL_RESEARCH_ROUTE)


def test_only_the_token_route_is_open_and_no_role_needs_it() -> None:
    assert frozenset({TOKEN_ROUTE}) == OPEN_ROUTES
    assert not any(TOKEN_ROUTE in grants.routes for grants in PERMISSIONS.values())
