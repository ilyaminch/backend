"""Pure ASGI request context: preserve http.disconnect for chat cancellation."""

import logging
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_response

logger = logging.getLogger("gis.http")


class RequestContext:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status = 500
        response_started = False

        async def send_context(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status, response_started = message["status"], True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_context)
        except Exception:
            if response_started:
                raise
            response = error_response(
                Request(scope), 500, "INTERNAL_ERROR", "Внутренняя ошибка сервера."
            )
            await response(scope, receive, send_context)
        finally:
            logger.info(
                "request_id=%s route=%s duration_ms=%.1f status=%s",
                request_id,
                getattr(scope.get("route"), "path", "unmatched"),
                (perf_counter() - started) * 1000,
                status,
            )
