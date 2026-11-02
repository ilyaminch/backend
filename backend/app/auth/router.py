import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import select, update

from app.auth.dependencies import COOKIE_NAME, CurrentUser
from app.auth.passwords import DUMMY_HASH, check_password
from app.auth.rate_limit import LoginRateLimiter
from app.auth.security import check_origin
from app.contracts.auth import AuthResponse, LoginInput, UserInfo
from app.contracts.errors import ErrorResponse
from app.core.errors import ApiError
from app.db.models import AuthSession, User

router = APIRouter(
    prefix="/auth",
    tags=["Вход"],
    responses={status: {"model": ErrorResponse} for status in (400, 401, 403, 422, 429, 503)},
)


def auth_payload(request: Request, user: User, session: AuthSession) -> AuthResponse:
    return AuthResponse(
        request_id=UUID(request.state.request_id),
        user=UserInfo.model_validate({"id": user.id, "login": user.login, "role": user.role}),
        expires_at=session.expires_at.astimezone(UTC),
        csrf_token=session.csrf_token,
    )


@router.post("/login")
async def login(body: LoginInput, request: Request, response: Response) -> AuthResponse:
    check_origin(request)
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise ApiError(400, "MALFORMED_JSON", "Требуется JSON-запрос.")
    limiter: LoginRateLimiter = request.app.state.login_limiter
    attempt = limiter.begin(request.client.host if request.client else "unknown", body.login)
    success: bool | None = None
    try:
        async with request.app.state.sessions() as db, db.begin():
            user = await db.scalar(select(User).where(User.login == body.login))
            verified_hash = user.password_hash if user else DUMMY_HASH
            valid = await asyncio.to_thread(
                check_password,
                verified_hash,
                body.password.get_secret_value(),
            )
            # A concurrent role/password/active change must be respected after
            # waiting for Argon2; no browser-supplied role is ever accepted.
            if user:
                user = await db.scalar(
                    select(User).where(User.id == user.id).execution_options(populate_existing=True)
                )
            if (
                not valid
                or not user
                or not user.active
                or user.role not in {"user", "admin"}
                or user.password_hash != verified_hash
            ):
                success = False
                raise ApiError(401, "INVALID_CREDENTIALS", "Неверный логин или пароль.")
            now = datetime.now(UTC)
            token = secrets.token_urlsafe(32)
            session = AuthSession(
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                user_id=user.id,
                created_at=now,
                expires_at=now
                + timedelta(
                    seconds=request.app.state.settings.auth_session_max_age_seconds,
                ),
                revoked_at=None,
                csrf_token=secrets.token_urlsafe(32),
            )
            db.add(session)
            payload = auth_payload(request, user, session)
        success = True
    finally:
        limiter.finish(attempt, success=success)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=request.app.state.settings.cookie_secure,
        max_age=request.app.state.settings.auth_session_max_age_seconds,
    )
    return payload


@router.get("/me")
async def me(request: Request, principal: CurrentUser) -> AuthResponse:
    return auth_payload(request, principal.user, principal.session)


@router.post("/logout", status_code=204)
async def logout(request: Request, principal: CurrentUser) -> Response:
    async with request.app.state.sessions() as db, db.begin():
        await db.execute(
            update(AuthSession)
            .where(
                AuthSession.token_hash == principal.session.token_hash,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
    request.app.state.chat_store.close_owner(principal.session.token_hash)
    response = Response(status_code=204)
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=request.app.state.settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response
