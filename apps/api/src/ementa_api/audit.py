"""Access trail of the REST API: one audit event per /v1 request, written after the response.

The event says who called (the user id from the token), which route (the template, as
"POST /v1/legal-research"), the status and the duration. Never the body, the query string or
the values in the path: any of them can carry personal data.
"""

import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import anyio
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ementa.domain.access import LEGAL_RESEARCH_ROUTE
from ementa_api.auth import User
from ementa_kit.audit import AuditEvent, AuditLog, AuditRecord, Outcome
from ementa_kit.policy import DataClass
from ementa_kit.telemetry import get_logger

HTTP_REQUEST = "http.request"

DATA_CLASSES: Mapping[str, DataClass] = {LEGAL_RESEARCH_ROUTE: DataClass.PUBLIC}
"""What each route returns. A route not listed counts as restricted."""

log = get_logger(__name__)


def outcome_of(status: int) -> Outcome:
    if status < 400:
        return Outcome.OK
    if status in (401, 403):
        return Outcome.DENIED
    return Outcome.REJECTED if status < 500 else Outcome.ERROR


def request_event(scope: Scope, status: int, duration_ms: int) -> AuditEvent:
    route = scope.get("route")
    method = scope["method"]
    resource = f"{method} {route.path}" if isinstance(route, APIRoute) else f"{method} (no route)"
    user = scope.get("state", {}).get("user")
    return AuditEvent(
        actor=user.id if isinstance(user, User) else None,
        action=HTTP_REQUEST,
        resource=resource,
        data_class=DATA_CLASSES.get(resource, DataClass.RESTRICTED),
        outcome=outcome_of(status),
        details={"status": status, "duration_ms": duration_ms},
    )


class AuditMiddleware:
    """Pure ASGI, so streamed responses (SSE) pass through untouched and are recorded when they
    end. A failed write is logged and the request goes on."""

    def __init__(self, app: ASGIApp, audit: Callable[[], AuditLog]) -> None:
        self.app = app
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/v1" or path.startswith("/v1/")):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500  # an exception that reaches this point has sent no response yet

        async def send_and_capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_capture)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000)
            event = request_event(scope, status, duration_ms)
            with anyio.CancelScope(shield=True):  # a client that hangs up still leaves a trail
                await self._record(event)

    async def _record(self, event: AuditEvent) -> None:
        try:
            await anyio.to_thread.run_sync(lambda: self.audit().append(event))
        except Exception:
            log.exception("audit_write_failed", resource=event.resource, outcome=event.outcome)


class AuditEventOut(BaseModel):
    id: int
    occurred_at: datetime
    actor: str | None
    action: str
    resource: str
    data_class: str
    outcome: str
    details: dict[str, Any]
    prev_hash: str | None
    hash: str

    @classmethod
    def from_record(cls, record: AuditRecord) -> "AuditEventOut":
        event = record.event
        return cls(
            id=record.id,
            occurred_at=event.occurred_at,
            actor=event.actor,
            action=event.action,
            resource=event.resource,
            data_class=event.data_class,
            outcome=event.outcome,
            details=dict(event.details),
            prev_hash=record.prev_hash.hex() if record.prev_hash is not None else None,
            hash=record.hash.hex(),
        )


class AuditPage(BaseModel):
    items: list[AuditEventOut]
    next_cursor: str | None


AUDIT_DESCRIPTION = (
    "Eventos de `audit.events`, do mais novo ao mais antigo: quem chamou, que rota, status e "
    "duração, com o hash encadeado de cada um. Nunca o corpo do pedido. Para a próxima página, "
    "mande o `next_cursor` recebido em `cursor`. Só sócia, DPO e TI; a conferência da cadeia é "
    "`uv run ementa audit verify`."
)
