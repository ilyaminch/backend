from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        retryable: bool = False,
        *,
        headers: dict[str, str] | None = None,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        self.status, self.code, self.message, self.retryable = status, code, message, retryable
        self.headers = headers or {}
        self.details = details or []


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "request_id": request.state.request_id,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or [],
        },
        "map_action": "preserve",
    }
    return JSONResponse(
        payload,
        status_code=status,
        headers={
            **(headers or {}),
            "X-Request-ID": request.state.request_id,
            "Cache-Control": "no-store",
        },
    )
