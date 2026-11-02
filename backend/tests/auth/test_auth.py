# Запуск из backend: uv run --frozen pytest tests/auth
# Отчёт: allure serve reports/allure/local

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import allure
import pytest
from sqlalchemy import func, select, update

from app.db.models import AuthSession, User
from tests.api_support import (
    API,
    ORIGIN,
    PASSWORD,
    ApiContext,
    assert_error,
    assert_headers,
    search_filter,
)


@allure.feature("Авторизация и права доступа")
class TestAuth:
    @allure.story("AUTH-01. Вход с правильными и неверными реквизитами")
    @pytest.mark.parametrize("name", ["user", "admin", "wrong-password"])
    async def test_login(self, api: ApiContext, name: str) -> None:
        with allure.step("Отправляем реквизиты входа"):
            client = await api.client()
            response = await client.post(
                f"{API}/auth/login",
                json={
                    "login": api.logins["user" if name == "wrong-password" else name],
                    "password": "incorrect" if name == "wrong-password" else PASSWORD,
                },
            )
        with allure.step("Проверяем ответ и серверную сессию"):
            if name == "wrong-password":
                assert_error(response, 401, "INVALID_CREDENTIALS")
                assert "gis_session" not in client.cookies
                async with api.sessions() as db:
                    count = await db.scalar(
                        select(func.count())
                        .select_from(AuthSession)
                        .where(AuthSession.user_id.in_(api.user_ids.values()))
                    )
                    assert count == 0
            else:
                assert response.status_code == 200
                assert_headers(response)
                assert response.json()["user"]["role"] == name
                assert response.json()["csrf_token"]
                cookie = response.headers["set-cookie"].lower()
                assert "httponly" in cookie
                assert "samesite=lax" in cookie
                session = await api.session(client)
                assert session.user_id == api.user_ids[name]
                me = await client.get(f"{API}/auth/me")
                assert me.status_code == 200
                assert me.json()["user"] == response.json()["user"]
                assert me.json()["csrf_token"] == response.json()["csrf_token"]

    @allure.story("AUTH-02. Прикладные маршруты требуют входа")
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/auth/me"),
            ("POST", "/auth/logout"),
            ("GET", "/layers"),
            ("GET", "/layers/school/features"),
            ("GET", "/objects/{object_id}"),
            ("GET", "/regions/{region_id}"),
            ("GET", "/query-capabilities"),
            ("POST", "/search"),
            ("POST", "/chat/sessions"),
            ("DELETE", "/chat/sessions/{dialogue_id}"),
            ("POST", "/chat/sessions/{dialogue_id}/messages"),
        ],
        ids=[
            "me",
            "logout",
            "layers",
            "features",
            "object",
            "region",
            "capabilities",
            "search",
            "create-chat",
            "delete-chat",
            "message",
        ],
    )
    async def test_protected_routes(self, api: ApiContext, method: str, path: str) -> None:
        with allure.step("Подготавливаем корректные ID и тело запроса"):
            owner = await api.login()
            dialogue_id = await api.create_dialogue(owner)
            path = path.format(
                object_id=api.object_ids[0], region_id=api.region_id, dialogue_id=dialogue_id
            )
            body: dict[str, Any] | None = None
            if path == "/search":
                body = search_filter(api.object_ids)
            elif path.endswith("/messages"):
                body = {"text": "Покажи школы", "client_request_id": str(uuid4())}
        with allure.step("Обращаемся к защищённому маршруту без cookie"):
            guest = await api.client()
            response = await guest.request(method, API + path, json=body)
        with allure.step("Проверяем отказ без изменения состояния"):
            assert_error(response, 401, "AUTH_REQUIRED")
            assert not api.dialogue.calls
            assert dialogue_id in api.app.state.chat_store.sessions
            assert len(api.app.state.chat_store.sessions) == 1
            assert not api.app.state.chat_store.sessions[dialogue_id].used_ids
            assert (await api.session(owner)).revoked_at is None

    @allure.story("AUTH-02. Истёкшая сессия не даёт доступа")
    async def test_expired_session(self, api: ApiContext) -> None:
        with allure.step("Завершаем срок действительной сессии в БД"):
            client = await api.login()
            session = await api.session(client)
            async with api.sessions() as db, db.begin():
                await db.execute(
                    update(AuthSession)
                    .where(AuthSession.token_hash == session.token_hash)
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
        with allure.step("Проверяем запрос с прежней cookie"):
            assert_error(await client.get(f"{API}/auth/me"), 401, "SESSION_EXPIRED")

    @allure.story("AUTH-03. Выход отзывает серверную сессию")
    async def test_logout(self, api: ApiContext) -> None:
        with allure.step("Входим и сохраняем старую cookie"):
            client = await api.login()
            old_token = client.cookies["gis_session"]
            session = await api.session(client)
        with allure.step("Выходим и повторяем запрос со старой cookie"):
            response = await client.post(f"{API}/auth/logout")
            assert response.status_code == 204
            assert_headers(response)
            assert response.content == b""
            assert "gis_session" not in client.cookies
            replay = await api.client()
            replay.cookies.set("gis_session", old_token)
            assert_error(await replay.get(f"{API}/auth/me"), 401, "AUTH_REQUIRED")
        with allure.step("Проверяем отзыв в БД"):
            async with api.sessions() as db:
                stored = await db.get(AuthSession, session.token_hash)
                assert stored is not None and stored.revoked_at is not None

    @allure.story("AUTH-04. Права администратора проверяются на сервере")
    async def test_admin_permissions(self, api: ApiContext) -> None:
        with allure.step("User запрещена операция, admin разрешена"):
            user = await api.login()
            admin = await api.login("admin")
            assert_error(await user.post("/test/admin-operation"), 403, "FORBIDDEN")
            assert api.app.state.admin_calls == 0
            assert (await admin.post("/test/admin-operation")).status_code == 200
            assert api.app.state.admin_calls == 1
        with allure.step("Понижаем роль в БД и повторяем запрос без нового входа"):
            async with api.sessions() as db, db.begin():
                await db.execute(
                    update(User).where(User.id == api.user_ids["admin"]).values(role="user")
                )
            assert_error(await admin.post("/test/admin-operation"), 403, "FORBIDDEN")
            assert api.app.state.admin_calls == 1
            assert (await admin.get(f"{API}/auth/me")).json()["user"]["role"] == "user"

    @allure.story("AUTH-05. Изменяющие запросы проверяют CSRF и Origin")
    @pytest.mark.parametrize("operation", ["create", "message", "delete", "search", "logout"])
    @pytest.mark.parametrize("invalid", ["csrf", "origin"])
    async def test_csrf(self, api: ApiContext, operation: str, invalid: str) -> None:
        with allure.step("Входим и подготавливаем корректный запрос"):
            client = await api.login()
            dialogue_id = await api.create_dialogue(client)
            paths = {
                "create": "/chat/sessions",
                "message": f"/chat/sessions/{dialogue_id}/messages",
                "delete": f"/chat/sessions/{dialogue_id}",
                "search": "/search",
                "logout": "/auth/logout",
            }
            body = (
                search_filter(api.object_ids)
                if operation == "search"
                else {"text": "Покажи школы", "client_request_id": str(uuid4())}
                if operation == "message"
                else None
            )
            method = "DELETE" if operation == "delete" else "POST"
            store = api.app.state.chat_store
        with allure.step("Отправляем запрос с неверным CSRF или Origin"):
            header = "X-CSRF-Token" if invalid == "csrf" else "Origin"
            response = await client.request(
                method, API + paths[operation], json=body, headers={header: "invalid"}
            )
            assert_error(response, 403, "CSRF_FAILED")
        with allure.step("Проверяем отсутствие побочных эффектов и корректный повтор"):
            assert len(store.sessions) == 1
            assert not store.sessions[dialogue_id].used_ids
            assert not api.dialogue.calls
            assert (await api.session(client)).revoked_at is None
            good = await client.request(method, API + paths[operation], json=body)
            assert good.status_code == {"create": 201, "delete": 204, "logout": 204}.get(
                operation, 200
            )

    @allure.story("AUTH-05. Вход разрешён только с доверенного Origin")
    async def test_login_origin(self, api: ApiContext) -> None:
        with allure.step("Отправляем правильные реквизиты с чужим Origin"):
            client = await api.client()
            body = {"login": api.logins["user"], "password": PASSWORD}
            response = await client.post(
                f"{API}/auth/login", json=body, headers={"Origin": "http://untrusted.invalid"}
            )
            assert_error(response, 403, "CSRF_FAILED")
            assert "gis_session" not in client.cookies
        with allure.step("Повторяем вход с правильным Origin без предварительного CSRF"):
            response = await client.post(f"{API}/auth/login", json=body, headers={"Origin": ORIGIN})
            assert response.status_code == 200

    @allure.story("AUTH-06. Лимит блокирует подбор пароля")
    async def test_login_rate_limit(self, api: ApiContext) -> None:
        client = await api.client()
        with allure.step("Исчерпываем лимит неудачных попыток пары IP и логина"):
            for _ in range(api.app.state.settings.login_failed_attempts):
                response = await client.post(
                    f"{API}/auth/login", json={"login": api.logins["user"], "password": "wrong"}
                )
                assert_error(response, 401, "INVALID_CREDENTIALS")
        with allure.step("Проверяем блокировку следующей попытки даже с правильным паролем"):
            response = await client.post(
                f"{API}/auth/login", json={"login": api.logins["user"], "password": PASSWORD}
            )
            assert_error(response, 429, "RATE_LIMITED")
            assert int(response.headers["Retry-After"]) > 0
            assert "gis_session" not in client.cookies
