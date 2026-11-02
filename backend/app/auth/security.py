"""Общие проверки источника запроса и CSRF для cookie-аутентификации."""

import secrets

from fastapi import Request

from app.core.errors import ApiError

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def check_origin(request: Request) -> None:
    if request.headers.get("origin") != request.app.state.settings.public_origin:
        raise ApiError(403, "CSRF_FAILED", "Недопустимый источник запроса.")


def check_csrf(request: Request, expected_token: str) -> None:
    if request.method in SAFE_METHODS:
        return
    check_origin(request)
    # compare_digest(str, str) supports ASCII only. Compare bytes so malformed
    # Unicode header values are rejected normally instead of raising TypeError.
    supplied = request.headers.get("X-CSRF-Token", "").encode("utf-8")
    if not secrets.compare_digest(supplied, expected_token.encode("utf-8")):
        raise ApiError(403, "CSRF_FAILED", "Недопустимый CSRF-токен.")
