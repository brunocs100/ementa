"""Errors as RFC 9457 problem details (`application/problem+json`).

Validation errors list where the request is wrong, never the values sent: a question can carry
personal data, and error bodies end up in logs. For the same reason `instance` is the template of
the matched route, not the path as sent, which could hold a CPF typed in by mistake.
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from ementa_kit.llm import ModelUnavailableError

PROBLEM_JSON = "application/problem+json"


class FieldError(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[FieldError] | None = None


def problem_response(problem: Problem, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=problem.status,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


def _title(status: int) -> str:
    return HTTPStatus(status).phrase


def _instance(request: Request) -> str:
    """`/v1/data-subjects/{subject_id}/memories`, or the path as sent when no route matched."""
    path = getattr(request.scope.get("route"), "path", None)
    return path if isinstance(path, str) else request.url.path


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` entries for the given statuses, all with the Problem schema."""
    return {
        status: {
            "model": Problem,
            "content": {PROBLEM_JSON: {}},
            "description": _title(status),
        }
        for status in statuses
    }


async def _validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        FieldError(
            loc=list(e.get("loc", ())), msg=str(e.get("msg", "")), type=str(e.get("type", ""))
        )
        for e in exc.errors()
    ]
    return problem_response(
        Problem(
            type="/problems/validation-error",
            title="Invalid request",
            status=422,
            detail="The request does not match the schema; see `errors`.",
            instance=_instance(request),
            errors=errors,
        )
    )


async def _http(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    detail = (
        exc.detail
        if isinstance(exc.detail, str) and exc.detail != _title(exc.status_code)
        else None
    )
    return problem_response(
        Problem(
            title=_title(exc.status_code),
            status=exc.status_code,
            detail=detail,
            instance=_instance(request),
        ),
        headers=exc.headers,  # WWW-Authenticate on 401, Allow on 405
    )


async def _model_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(
        Problem(
            type="/problems/model-unavailable",
            title="Model unavailable",
            status=503,
            detail="The language model is not configured or not reachable.",
            instance=_instance(request),
        )
    )


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(Problem(title=_title(500), status=500, instance=_instance(request)))


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation)
    app.add_exception_handler(StarletteHTTPException, _http)
    app.add_exception_handler(ModelUnavailableError, _model_unavailable)
    app.add_exception_handler(Exception, _unexpected)
