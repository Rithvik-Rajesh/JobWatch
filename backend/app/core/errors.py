"""Error model. Services raise AppError subclasses; the API renders them as
{"error": {"code", "message", "details"}} with the matching HTTP status."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.core.logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    """State conflict, e.g. stale row_version (optimistic concurrency) or duplicate name."""

    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            _body("validation_failed", "Request validation failed", jsonable(exc.errors())), status_code=422
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        log.warning("integrity_error", error=str(exc.orig))
        return JSONResponse(_body("conflict", "The change conflicts with existing data"), status_code=409)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error=str(exc))
        return JSONResponse(_body("internal_error", "Internal server error"), status_code=500)


def jsonable(errors: Any) -> Any:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(errors, custom_encoder={Exception: str})


class NotImplementedYet(AppError):
    status_code = 501
    code = "not_implemented"


def todo() -> NotImplementedYet:
    """Placeholder used by contract stubs until the owning agent implements the endpoint."""
    return NotImplementedYet("Endpoint defined by the API contract but not implemented yet")
