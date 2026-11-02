# Запуск из backend: uv run --frozen pytest tests/chat
# Отчёт: allure serve reports/allure/local

import asyncio
from uuid import uuid4

import allure
import pytest
from sqlalchemy import select

from app.contracts.chat import Candidate, Clarification
from app.db.models import DatasetState
from tests.api_support import API, ApiContext, assert_error, assert_headers, candidate


def clarification() -> Clarification:
    return Clarification(
        kind="clarification",
        message="Какие школы показать?",
        options=[],
        proposed_state={"pending_category": "school"},
    )


@allure.feature("Сценарии чата")
class TestChat:
    @allure.story("CHAT-01. Сообщение возвращает результат поиска")
    @pytest.mark.parametrize("empty", [False, True], ids=["objects-found", "empty-result"])
    async def test_search_result(self, api: ApiContext, empty: bool) -> None:
        with allure.step("Создаём диалог и задаём известный фильтр"):
            client = await api.login()
            dialogue_id = await api.create_dialogue(client)
            decision = candidate(api.object_ids, completed=True)
            if empty:
                decision.filter["conditions"].append({"field": "id", "op": "eq", "value": "0"})
            api.dialogue.decision = decision
            request_id = str(uuid4())
        with allure.step("Отправляем сообщение и проверяем результат реального SQL-поиска"):
            response = await api.message(client, dialogue_id, request_id)
            assert response.status_code == 200
            assert_headers(response)
            body = response.json()
            assert body["dialogue_id"] == str(dialogue_id)
            assert body["client_request_id"] == request_id
            assert body["outcome"] == "result"
            assert body["validated_filter"] == decision.filter
            result = body["result"]
            expected_ids = [] if empty else api.object_ids
            assert result["count"] == result["matched_object_count"] == len(expected_ids)
            assert result["map_action"] == "replace"
            assert result["feature_collection"]["type"] == "FeatureCollection"
            features = result["feature_collection"]["features"]
            assert [item["id"] for item in features] == expected_ids
            if empty:
                assert body["message"] == "По этим условиям ничего не найдено"
            else:
                assert [item["geometry"] for item in features] == [
                    {"type": "Point", "coordinates": [84, 56]},
                    {"type": "Point", "coordinates": [85, 56]},
                ]
            async with api.sessions() as db:
                version = await db.scalar(select(DatasetState.version).where(DatasetState.id == 1))
            assert result["dataset_version"] == str(version)
            assert api.dialogue.calls == [{}]
            assert api.app.state.chat_store.sessions[dialogue_id].state == {"completed": True}
            assert "proposed_state" not in body

    @allure.story("CHAT-02. Уточнение передаёт контекст следующему сообщению")
    async def test_clarification(self, api: ApiContext) -> None:
        client = await api.login()
        dialogue_id = await api.create_dialogue(client)
        with allure.step("Получаем уточнение и сохраняем карту"):
            api.dialogue.decision = clarification()
            first = await api.message(client, dialogue_id)
            assert first.status_code == 200
            assert_headers(first)
            assert first.json()["outcome"] == "clarification"
            assert first.json()["message"] == "Какие школы показать?"
            assert first.json()["options"] == []
            assert first.json()["result"] is None
            assert first.json()["map_action"] == "preserve"
        with allure.step("Продолжаем диалог с сохранёнными условиями"):
            api.dialogue.decision = candidate([api.object_ids[0]], completed=True)
            second = await api.message(client, dialogue_id)
            assert second.status_code == 200
            assert second.json()["outcome"] == "result"
            assert second.json()["result"]["count"] == 1
            assert api.dialogue.calls == [{}, {"pending_category": "school"}]
            assert api.app.state.chat_store.sessions[dialogue_id].state == {"completed": True}

    @allure.story("CHAT-03. Чужой вход не получает доступ к диалогу")
    @pytest.mark.parametrize("visitor_name", ["other", "user"], ids=["other-user", "second-login"])
    async def test_dialogue_owner(self, api: ApiContext, visitor_name: str) -> None:
        with allure.step("Создаём диалог владельца и отдельный вход посетителя"):
            owner = await api.login()
            dialogue_id = await api.create_dialogue(owner)
            visitor = await api.login(visitor_name)
        with allure.step("Проверяем запрет отправки и удаления чужого диалога"):
            assert_error(await api.message(visitor, dialogue_id), 404, "CHAT_SESSION_NOT_FOUND")
            response = await visitor.delete(f"{API}/chat/sessions/{dialogue_id}")
            assert_error(response, 404, "CHAT_SESSION_NOT_FOUND")
            assert not api.dialogue.calls
            assert api.app.state.chat_store.sessions[dialogue_id].state == {}
        with allure.step("Проверяем сохранение доступа владельца"):
            assert (await api.message(owner, dialogue_id)).status_code == 200

    @allure.story("CHAT-03. Собственные диалоги имеют независимый контекст")
    async def test_independent_contexts(self, api: ApiContext) -> None:
        client = await api.login()
        first = await api.create_dialogue(client)
        second = await api.create_dialogue(client)
        with allure.step("Сохраняем разные состояния двух диалогов"):
            api.dialogue.decision = clarification()
            assert (await api.message(client, first)).status_code == 200
            api.dialogue.decision = candidate([api.object_ids[1]], second_dialogue=True)
            assert (await api.message(client, second)).status_code == 200
            assert api.dialogue.calls == [{}, {}]
        with allure.step("Продолжаем первый диалог и проверяем его собственное состояние"):
            api.dialogue.decision = candidate([api.object_ids[0]], first_dialogue=True)
            assert (await api.message(client, first)).status_code == 200
            assert api.dialogue.calls[-1] == {"pending_category": "school"}
            assert api.app.state.chat_store.sessions[second].state == {"second_dialogue": True}

    @allure.story("CHAT-04. Повторный ID сообщения не запускает обработку снова")
    async def test_duplicate_message(self, api: ApiContext) -> None:
        client = await api.login()
        dialogue_id = await api.create_dialogue(client)
        request_id = str(uuid4())
        with allure.step("Завершаем первое сообщение и повторяем его ID"):
            assert (await api.message(client, dialogue_id, request_id)).status_code == 200
            response = await api.message(client, dialogue_id, request_id)
        with allure.step("Проверяем отказ без повторной интерпретации"):
            assert_error(response, 409, "REQUEST_ALREADY_USED")
            assert len(api.dialogue.calls) == 1

    @allure.story("CHAT-04. Параллельное сообщение в занятый диалог отклоняется")
    async def test_busy_dialogue(self, api: ApiContext) -> None:
        client = await api.login()
        dialogue_id = await api.create_dialogue(client)
        first_id, second_id = str(uuid4()), str(uuid4())
        with allure.step("Удерживаем первое сообщение и отправляем повтор и новое сообщение"):
            async with api.pending_message(client, dialogue_id, first_id):
                assert_error(
                    await api.message(client, dialogue_id, first_id), 409, "REQUEST_ALREADY_USED"
                )
                assert_error(await api.message(client, dialogue_id, second_id), 409, "CHAT_BUSY")
                assert len(api.dialogue.calls) == 1
        with allure.step("После завершения шага новый ID остаётся пригодным"):
            assert (await api.message(client, dialogue_id, second_id)).status_code == 200
            assert len(api.dialogue.calls) == 2

    @allure.story("CHAT-05. Недопустимый фильтр сохраняет прежний контекст")
    async def test_invalid_filter(self, api: ApiContext) -> None:
        client = await api.login()
        dialogue_id = await api.create_dialogue(client)
        api.dialogue.decision = clarification()
        assert (await api.message(client, dialogue_id)).status_code == 200
        request_id = str(uuid4())
        with allure.step("Предлагаем недопустимый фильтр и новое состояние"):
            api.dialogue.decision = Candidate(
                kind="candidate",
                filter={"sql": "SELECT * FROM users"},
                proposed_state={"must_not_be_saved": True},
            )
            response = await api.message(client, dialogue_id, request_id)
        with allure.step("Проверяем ошибку, карту и сохранность контекста"):
            assert_error(response, 422, "FILTER_INVALID")
            assert response.json()["error"]["details"]
            assert "SELECT *" not in response.text
            entry = api.app.state.chat_store.sessions[dialogue_id]
            assert entry.state == {"pending_category": "school"}
            assert entry.current_id is None
            assert_error(
                await api.message(client, dialogue_id, request_id), 409, "REQUEST_ALREADY_USED"
            )
        with allure.step("Новый шаг получает прежний контекст и успешно завершается"):
            api.dialogue.decision = candidate([api.object_ids[0]], completed=True)
            assert (await api.message(client, dialogue_id)).status_code == 200
            assert api.dialogue.calls[-1] == {"pending_category": "school"}

    @allure.story("CHAT-05. Без модуля диалога возвращается безопасная ошибка")
    @pytest.mark.parametrize("api", [False], indirect=True, ids=["module-not-installed"])
    async def test_unavailable_dialogue(self, api: ApiContext) -> None:
        client = await api.login()
        dialogue_id = await api.create_dialogue(client)
        with allure.step("Отправляем сообщение без установленного Dialogue"):
            assert_error(await api.message(client, dialogue_id), 503, "DIALOGUE_UNAVAILABLE")
        with allure.step("Проверяем освобождение шага и доступность прямого поиска"):
            entry = api.app.state.chat_store.sessions[dialogue_id]
            assert entry.state == {}
            assert entry.current_id is None
            response = await client.post(f"{API}/search", json=candidate(api.object_ids).filter)
            assert response.status_code == 200
            assert response.json()["result"]["count"] == 2

    @allure.story("CHAT-06. Удаление и logout отменяют шаг без поздней записи")
    @pytest.mark.parametrize("operation", ["delete", "logout"])
    async def test_cancel_message(self, api: ApiContext, operation: str) -> None:
        client = await api.login()
        dialogue_id = await api.create_dialogue(client)
        entry = api.app.state.chat_store.sessions[dialogue_id]
        api.dialogue.finish_after_cancel = True
        with allure.step("Удерживаем генерацию и отменяем диалог"):
            async with api.pending_message(client, dialogue_id, str(uuid4())) as pending:
                worker = entry.task
                assert worker is not None
                response = (
                    await client.delete(f"{API}/chat/sessions/{dialogue_id}")
                    if operation == "delete"
                    else await client.post(f"{API}/auth/logout")
                )
                assert response.status_code == 204
                assert_headers(response)
                await asyncio.wait_for(api.dialogue.cancelled.wait(), timeout=5)
                assert not api.dialogue.returned.is_set()
                assert entry.closed
                assert dialogue_id not in api.app.state.chat_store.sessions
                assert entry.state == {}
                # Модуль завершится после удаления/logout, но до ответа
                # исходного HTTP-handler, чтобы проверить запрет поздней записи.
                api.dialogue.release.set()
                assert_error(await asyncio.wait_for(pending, timeout=5), 409, "CHAT_SESSION_CLOSED")
                await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=5)
        with allure.step("Проверяем отсутствие восстановления контекста"):
            assert api.dialogue.returned.is_set()
            assert entry.state == {}
            assert dialogue_id not in api.app.state.chat_store.sessions
            if operation == "delete":
                assert_error(await api.message(client, dialogue_id), 404, "CHAT_SESSION_NOT_FOUND")
            else:
                assert_error(await client.get(f"{API}/auth/me"), 401, "AUTH_REQUIRED")
