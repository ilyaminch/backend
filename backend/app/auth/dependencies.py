import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import check_csrf
from app.core.errors import ApiError
from app.db.models import AuthSession, User

COOKIE_NAME = "gis_session"
session_cookie = APIKeyCookie(name=COOKIE_NAME, scheme_name="SessionCookie", auto_error=False)


@dataclass
class Principal:
    user: User
    session: AuthSession


async def authenticate(request: Request, db: AsyncSession) -> Principal:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise ApiError(401, "AUTH_REQUIRED", "Войдите в приложение.")
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = (
        await db.execute(
            select(User, AuthSession)
            .join(AuthSession)
            .where(
                AuthSession.token_hash == digest,
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if (
        row is None
        or row.AuthSession.revoked_at is not None
        or not row.User.active
        or row.User.role not in {"user", "admin"}
    ):
        raise ApiError(401, "AUTH_REQUIRED", "Войдите в приложение.")
    if row.AuthSession.expires_at <= datetime.now(UTC):
        raise ApiError(401, "SESSION_EXPIRED", "Срок входа истёк. Войдите снова.")
    return Principal(row.User, row.AuthSession)


def ensure_admin(principal: Principal) -> None:
    """Reusable inside a write transaction when rights must be rechecked."""
    if principal.user.role != "admin":
        raise ApiError(403, "FORBIDDEN", "Операция доступна только администратору.")


async def require_user(
    request: Request,
    token: Annotated[str | None, Security(session_cookie)],
) -> Principal:
    """Authenticate from the database on every protected request.

    The short authorization transaction is closed before reading an upload
    or calling LLM; the caller never retains its connection across such work.
    """
    if not token:
        raise ApiError(401, "AUTH_REQUIRED", "Войдите в приложение.")
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with sessions() as db, db.begin():
        principal = await authenticate(request, db)
        check_csrf(request, principal.session.csrf_token)
        return principal


async def require_admin(principal: Annotated[Principal, Depends(require_user)]) -> Principal:
    ensure_admin(principal)
    return principal


CurrentUser = Annotated[Principal, Depends(require_user)]
AdminUser = Annotated[Principal, Depends(require_admin)]
