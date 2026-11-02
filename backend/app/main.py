import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.auth.rate_limit import LoginRateLimiter
from app.auth.router import router as auth_router
from app.catalog.router import router as catalog_router
from app.chat.router import router as chat_router
from app.chat.store import ChatStore
from app.core.config import Settings
from app.core.errors import ApiError, error_response
from app.core.http import RequestContext
from app.data_admin.router import router as data_admin_router
from app.db.session import create_sessions
from app.dialogue.protocol import Dialogue, load_dialogue
from app.llm.client import LLMClient

logger = logging.getLogger("gis.http")
MIGRATION_HEAD = "0001_initial"


def create_app(settings: Settings | None = None, *, dialogue: Dialogue | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = settings or Settings()
        application.state.sessions = create_sessions(application.state.settings)
        application.state.login_limiter = LoginRateLimiter(application.state.settings)
        application.state.chat_store = ChatStore(application.state.settings)
        application.state.dialogue = dialogue or load_dialogue(
            application.state.settings.dialogue_factory
        )
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as http:
            application.state.llm_client = LLMClient(application.state.settings, http)
            reaper = asyncio.create_task(application.state.chat_store.reap())
            try:
                yield
            finally:
                application.state.chat_store.shutdown()
                pending = set(application.state.chat_store.tasks)
                if pending:
                    _, unfinished = await asyncio.wait(pending, timeout=5)
                    if unfinished:
                        logger.warning(
                            "Dialogue module ignored cancellation: count=%d", len(unfinished)
                        )
                reaper.cancel()
                with suppress(asyncio.CancelledError):
                    await reaper
                await application.state.sessions.kw["bind"].dispose()

    application = FastAPI(title="ГИС-помощник", version="0.1.0", lifespan=lifespan)

    application.add_middleware(RequestContext)

    @application.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(
            request,
            error.status,
            error.code,
            error.message,
            error.retryable,
            headers=error.headers,
            details=error.details,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        malformed = any(item["type"] == "json_invalid" for item in error.errors())
        details = [
            {"path": ".".join(map(str, item["loc"])), "code": item["type"]}
            for item in error.errors()
        ]
        return error_response(
            request,
            400 if malformed else 422,
            "MALFORMED_JSON"
            if malformed
            else ("FILTER_INVALID" if request.url.path == "/api/v1/search" else "VALIDATION_ERROR"),
            "Некорректный формат запроса.",
            details=details,
        )

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return error_response(
            request,
            error.status_code,
            "NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR",
            "Маршрут не найден." if error.status_code == 404 else "Не удалось выполнить запрос.",
        )

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        return error_response(
            request, 503, "DATABASE_UNAVAILABLE", "База данных временно недоступна.", True
        )

    @application.exception_handler(ConnectionError)
    async def connection_error(request: Request, error: ConnectionError) -> JSONResponse:
        return error_response(
            request, 503, "DATABASE_UNAVAILABLE", "База данных временно недоступна.", True
        )

    @application.get("/api/v1/health/live", tags=["Состояние"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/v1/health/ready", tags=["Состояние"])
    async def ready(request: Request) -> dict[str, str]:
        async with request.app.state.sessions() as db:
            revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != MIGRATION_HEAD:
                raise ApiError(503, "DATABASE_UNAVAILABLE", "Требуется обновление базы данных.")
        return {"status": "ok"}

    application.include_router(auth_router, prefix="/api/v1")
    application.include_router(catalog_router, prefix="/api/v1")
    application.include_router(chat_router, prefix="/api/v1")
    application.include_router(data_admin_router, prefix="/api/v1")
    return application


app = create_app()
