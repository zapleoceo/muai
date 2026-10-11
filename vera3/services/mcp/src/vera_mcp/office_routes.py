"""HTTP-фасад office intake (/office/v1). Тонкий слой: принципал берётся только из scope.

Токен и тексты задач не логируются: в журнал идёт лишь класс ошибки.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from vera_shared.office import intake
from vera_shared.office.acl import OfficeForbidden, OfficePrincipal
from vera_shared.office.intake_input import InvalidInput

from vera_mcp.auth import OFFICE_PRINCIPAL_KEY

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
Handler = Callable[[Request, OfficePrincipal], Awaitable[Response]]


def error_response(status: int, error: str, detail: str = "", *, retryable: bool = False,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": error, "retryable": retryable}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, headers=headers)


def _principal(request: Request) -> OfficePrincipal | None:
    found = request.scope.get(OFFICE_PRINCIPAL_KEY)
    return found if isinstance(found, OfficePrincipal) else None


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("request body is too large")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise ValueError("body must be valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


def _guarded(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
    async def run(request: Request) -> Response:
        principal = _principal(request)
        if principal is None:
            return error_response(403, "forbidden")
        try:
            return await handler(request, principal)
        except OfficeForbidden:
            return error_response(403, "forbidden")
        except intake.IntakeNotFound:
            return error_response(404, "not_found")
        except intake.IntakeConflict as exc:
            return error_response(409, exc.code, str(exc))
        except intake.IntakeBusy as exc:
            return error_response(503, "in_progress", str(exc), retryable=True,
                                  headers={"Retry-After": "1"})
        except InvalidInput as exc:
            return error_response(422, "invalid_request", str(exc))
        except ValueError as exc:
            return error_response(400, "malformed_request", str(exc))
        except Exception as exc:
            log.error("office intake failed: %s", type(exc).__name__)
            return error_response(500, "internal_error", retryable=True)
    return run


async def _create(request: Request, principal: OfficePrincipal) -> Response:
    data = await _json(request)
    result = await intake.create_task(
        principal, data.get("client_task_id"), data.get("title"), data.get("body"),
        data.get("origin"), data.get("result_recipient"), data.get("causation_id"),
        data.get("context"), data.get("attachments"))
    return JSONResponse(result, status_code=201 if result["created"] else 200)


async def _answer(request: Request, principal: OfficePrincipal) -> Response:
    data = await _json(request)
    qid, rev = data.get("question_id"), data.get("expected_revision")
    if isinstance(qid, bool) or not isinstance(qid, int):
        raise InvalidInput("question_id must be an integer")
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise InvalidInput("expected_revision must be an integer")
    result = await intake.answer(principal, request.path_params["task_id"], qid,
                                 data.get("text"), rev, data.get("idempotency_key"))
    return JSONResponse(result)


async def _delivered(request: Request, principal: OfficePrincipal) -> Response:
    data = await _json(request)
    return JSONResponse(await intake.mark_delivered(
        principal, request.path_params["task_id"], data.get("revision")))


async def _list(request: Request, principal: OfficePrincipal) -> Response:
    q = request.query_params
    return JSONResponse(await intake.list_tasks(
        principal, q.get("updated_since"), q.get("cursor"), q.get("limit")))


def office_app() -> Starlette:
    return Starlette(routes=[
        Route("/tasks", _guarded(_create), methods=["POST"]),
        Route("/tasks", _guarded(_list), methods=["GET"]),
        Route("/tasks/{task_id}/answers", _guarded(_answer), methods=["POST"]),
        Route("/tasks/{task_id}/delivered", _guarded(_delivered), methods=["POST"]),
    ])
