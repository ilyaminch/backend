"""Запуск: uv run --frozen pytest tests/imports (нужна одноразовая БД gis_test)."""

import asyncio
import copy
from collections.abc import AsyncIterator

import allure
import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Connection, ExecutionContext

from app.db.models import DatasetState, User
from tests.api_support import API, ApiContext, assert_error, assert_headers, search_filter
from tests.imports.support import FIRST_ID, collection, database_state, encoded, upload


@allure.feature("Импорт слоёв GeoJSON")
class TestImport:
    @allure.story("IMPORT-01. Все типы слоёв импортируются и доступны через API")
    @pytest.mark.parametrize(
        "layer", ["school", "hospital", "pharmacy", "park", "improvement_zone", "regions"]
    )
    async def test_import_layer(self, api: ApiContext, layer: str) -> None:
        client = await api.login("admin")
        payload = collection(layer)
        if layer != "regions":
            # Эти значения сервер должен вычислить по геометрии, а не взять из файла.
            payload["features"][0]["properties"].update(area_m2=999999999, length_m=999999999)
        before = await database_state(api)
        with allure.step("Загружаем GeoJSON с ID больше безопасного целого JavaScript"):
            response = await upload(client, payload, layer)
            assert response.status_code == 200, response.text
            assert_headers(response)
            body = response.json()
            assert body["layer_code"] == layer
            assert body["imported_count"] == len(payload["features"])
            version = str(before["dataset_state"][0]["version"] + 1)
            assert body["dataset_version"] == version
            assert body["suggest_repeat_search"] is True
        with allure.step("Проверяем геометрию, свойства и метаданные через чтение API"):
            result = (await client.get(f"{API}/layers/{layer}/features")).json()["result"]
            assert result["dataset_version"] == version
            assert result["count"] == len(payload["features"])
            found = {item["id"]: item for item in result["feature_collection"]["features"]}
            for item in payload["features"]:
                assert found[item["id"]]["geometry"] == item["geometry"]
                assert found[item["id"]]["properties"]["name"] == item["properties"]["name"]
            if layer != "regions":
                properties = found[str(FIRST_ID)]["properties"]
                assert properties["capacity"] == 0
                assert properties["is_accessible"] is False
                assert properties["opening_date"] is None
                for metric, measured in (
                    ("area_m2", layer == "park"),
                    ("length_m", layer == "improvement_zone"),
                ):
                    if measured:
                        assert 0 < properties[metric] < 999999999
                    else:
                        assert properties[metric] is None
            registry = (await client.get(f"{API}/layers")).json()
            assert registry["dataset_version"] == version
            imported = next(item for item in registry["layers"] if item["code"] == layer)
            assert imported["feature_count"] == len(payload["features"])
            assert imported["updated_at"] is not None
        with allure.step("Проверяем, что остальные слои и их метаданные сохранены"):
            after = await database_state(api)
            assert [s for s in after["layer_state"] if s["layer_code"] != layer] == [
                s for s in before["layer_state"] if s["layer_code"] != layer
            ]
            assert [o for o in after["gis_objects"] if o["category_code"] != layer] == [
                o for o in before["gis_objects"] if o["category_code"] != layer
            ]
            if layer != "regions":
                assert after["regions"] == before["regions"]
            else:
                assert after["gis_objects"] == before["gis_objects"]
            if layer == "school":
                search = await client.post(f"{API}/search", json=search_filter([str(FIRST_ID)]))
                assert search.status_code == 200
                assert search.json()["result"]["count"] == 1

    @allure.story("IMPORT-02. Повторный импорт заменяет весь слой и меняет версию")
    async def test_replace_layer(self, api: ApiContext) -> None:
        client = await api.login("admin")
        payload = collection(count=2)
        first = await upload(client, payload)
        assert first.status_code == 200
        old_version = first.json()["dataset_version"]
        # Повторно используем источник и один ID; второй объект должен исчезнуть.
        payload["features"] = payload["features"][:1]
        payload["features"][0]["properties"]["name"] = "Обновлённая школа"
        response = await upload(client, payload)
        assert response.status_code == 200
        assert response.json()["dataset_version"] == str(int(old_version) + 1)
        result = (await client.get(f"{API}/layers/school/features")).json()["result"]
        assert result["count"] == 1
        assert (
            result["feature_collection"]["features"][0]["properties"]["name"] == "Обновлённая школа"
        )
        assert_error(await client.get(f"{API}/objects/{FIRST_ID + 1}"), 404, "OBJECT_NOT_FOUND")
        stale = await client.get(
            f"{API}/objects/{FIRST_ID}", params={"dataset_version": old_version}
        )
        assert_error(stale, 409, "DATA_CHANGED")

    @allure.story("IMPORT-03. Права и CSRF проверяются до разбора файла")
    @pytest.mark.parametrize("access", ["guest", "user", "csrf", "origin", "demoted"])
    async def test_access(self, api: ApiContext, access: str) -> None:
        client = (
            await api.client()
            if access == "guest"
            else await api.login("user" if access == "user" else "admin")
        )
        if access == "csrf":
            client.headers.pop("X-CSRF-Token")
        elif access == "origin":
            client.headers["Origin"] = "http://untrusted.invalid"
        elif access == "demoted":
            async with api.sessions() as db, db.begin():
                await db.execute(
                    update(User).where(User.id == api.user_ids["admin"]).values(role="user")
                )
        before = await database_state(api)
        # Заведомо неверное тело: ожидаем ошибку доступа, а не парсинга multipart.
        response = await client.post(f"{API}/admin/layers/school/import", content=b"broken")
        status, code = (
            (401, "AUTH_REQUIRED")
            if access == "guest"
            else (403, "CSRF_FAILED" if access in {"csrf", "origin"} else "FORBIDDEN")
        )
        assert_error(response, status, code)
        assert await database_state(api) == before

    @allure.story("IMPORT-04. Некорректный JSON отклоняется без изменений БД")
    @pytest.mark.parametrize(
        "content",
        [
            b"{",
            b'{"type":1,"type":2}',
            b'{"x":NaN}',
            b'{"\\ud800":1}',
            '{"type":"FeatureCollection"}'.encode("utf-16"),
        ],
        ids=["broken", "duplicate-key", "nan", "invalid-unicode", "utf16"],
    )
    async def test_invalid_json(self, api: ApiContext, content: bytes) -> None:
        client = await api.login("admin")
        before = await database_state(api)
        response = await upload(client, content)
        assert_error(response, 422, "IMPORT_INVALID")
        assert response.json()["error"]["details"] == [{"path": "file", "code": "INVALID_JSON"}]
        assert await database_state(api) == before

    @allure.story("IMPORT-05. Ошибка одного объекта отклоняет весь файл")
    @pytest.mark.parametrize(
        "case",
        [
            "empty",
            "schema-version",
            "duplicate-id",
            "duplicate-source",
            "category",
            "geometry-type",
            "coordinates",
            "third-coordinate",
            "rating",
            "capacity",
            "bool",
            "missing-name",
            "extra-field",
            "unknown-source",
            "bbox-null",
        ],
    )
    async def test_invalid_collection(self, api: ApiContext, case: str) -> None:
        client = await api.login("admin")
        payload = collection(count=2)
        item = payload["features"][1]
        properties = item["properties"]
        if case == "empty":
            payload["features"] = []
        elif case == "schema-version":
            payload["schema_version"] = "2"
        elif case == "duplicate-id":
            item["id"] = payload["features"][0]["id"]
        elif case == "duplicate-source":
            payload["data_sources"].append(copy.deepcopy(payload["data_sources"][0]))
        elif case == "category":
            properties["category_code"] = "hospital"
        elif case == "geometry-type":
            item["geometry"] = {"type": "LineString", "coordinates": [[84, 56], [85, 56]]}
        elif case in {"coordinates", "third-coordinate"}:
            item["geometry"]["coordinates"] = [181, 56] if case == "coordinates" else [84, 56, 0]
        elif case == "rating":
            properties["rating"] = 6
        elif case == "capacity":
            properties["capacity"] = "100"
        elif case == "bool":
            properties["is_accessible"] = 1
        elif case == "missing-name":
            properties.pop("name")
        elif case == "extra-field":
            properties["unexpected"] = True
        elif case == "unknown-source":
            properties["source"] = "missing-source"
        else:
            item["bbox"] = None
        before = await database_state(api)
        response = await upload(client, payload)
        assert_error(response, 422, "IMPORT_INVALID")
        assert response.json()["error"]["details"]
        assert await database_state(api) == before

    @allure.story("IMPORT-06. Невалидные полигоны и наборы районов отклоняются")
    @pytest.mark.parametrize("case", ["open-ring", "self-intersection", "three-regions", "names"])
    async def test_invalid_regions(self, api: ApiContext, case: str) -> None:
        client = await api.login("admin")
        payload = collection("regions")
        if case == "open-ring":
            payload["features"][0]["geometry"]["coordinates"][0][-1] = [85, 57]
        elif case == "self-intersection":
            payload["features"][0]["geometry"]["coordinates"] = [
                [[84, 56], [85, 57], [84, 57], [85, 56], [84, 56]]
            ]
        elif case == "three-regions":
            payload["features"].pop()
        else:
            payload["features"][0]["properties"]["name"] = "Несуществующий район"
        before = await database_state(api)
        assert_error(await upload(client, payload, "regions"), 422, "IMPORT_INVALID")
        assert await database_state(api) == before

    @allure.story("IMPORT-07. Конфликты с существующими данными сохраняют прежний набор")
    @pytest.mark.parametrize("case", ["id", "source"])
    async def test_conflict(self, api: ApiContext, case: str) -> None:
        client = await api.login("admin")
        payload = collection("hospital")
        if case == "id":
            payload["features"][0]["id"] = api.object_ids[0]
        else:
            assert (await upload(client, payload, "hospital")).status_code == 200
            payload["data_sources"][0]["title"] = "Попытка изменить существующий источник"
        before = await database_state(api)
        response = await upload(client, payload, "hospital")
        assert_error(response, 409, "IMPORT_CONFLICT")
        assert response.json()["error"]["details"][0]["code"] == (
            "ID_CONFLICT" if case == "id" else "SOURCE_CONFLICT"
        )
        assert await database_state(api) == before

    @allure.story("IMPORT-08. Формат multipart и расширение файла проверяются")
    @pytest.mark.parametrize(
        "case", ["json-body", "wrong-field", "two-files", "extra-field", "suffix"]
    )
    async def test_upload_format(self, api: ApiContext, case: str) -> None:
        client = await api.login("admin")
        before = await database_state(api)
        path = f"{API}/admin/layers/school/import"
        content = encoded(collection())
        if case == "json-body":
            response = await client.post(path, json=collection())
        elif case == "wrong-field":
            response = await client.post(path, files={"other": ("layer.geojson", content)})
        elif case == "two-files":
            response = await client.post(
                path, files=[("file", ("one.geojson", content)), ("file", ("two.geojson", content))]
            )
        elif case == "extra-field":
            response = await client.post(
                path, files={"file": ("layer.geojson", content)}, data={"extra": "value"}
            )
        else:
            response = await upload(client, content, filename="layer.txt")
        assert_error(
            response,
            422 if case == "suffix" else 400,
            "IMPORT_INVALID" if case == "suffix" else "MALFORMED_MULTIPART",
        )
        assert await database_state(api) == before

    @allure.story("IMPORT-09. Лимиты файла, запроса, объектов и координат соблюдаются")
    @pytest.mark.parametrize("limit", ["file", "request", "features", "positions", "geometry"])
    async def test_limits(self, api: ApiContext, limit: str) -> None:
        client = await api.login("admin")
        payload = collection(count=2)
        settings = api.app.state.settings
        if limit == "file":
            settings.import_max_file_bytes = len(encoded(payload)) - 1
        elif limit == "request":
            settings.import_max_request_bytes = len(encoded(payload))
        elif limit == "features":
            settings.import_max_features = 1
        elif limit == "positions":
            settings.import_max_positions = 1
        else:
            payload = collection("park")
            settings.import_max_positions_per_geometry = 4
        before = await database_state(api)
        response = await upload(client, payload, "park" if limit == "geometry" else "school")
        large = limit in {"file", "request"}
        assert_error(
            response, 413 if large else 422, "PAYLOAD_TOO_LARGE" if large else "IMPORT_INVALID"
        )
        assert await database_state(api) == before

    @allure.story("IMPORT-10. Ошибка записи откатывает удаление, источники и версии")
    async def test_database_rollback(self, api: ApiContext) -> None:
        client = await api.login("admin")
        payload = collection(count=2)
        payload["features"][1]["properties"]["name"] = "reject-this-object"
        async with api.sessions() as db, db.begin():
            await db.execute(
                text("""
                CREATE FUNCTION reject_test_object() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.name = 'reject-this-object' THEN
                        RAISE EXCEPTION 'Test write failure' USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END $$
            """)
            )
            await db.execute(
                text("""
                CREATE TRIGGER reject_test_object BEFORE INSERT ON gis_objects
                FOR EACH ROW EXECUTE FUNCTION reject_test_object()
            """)
            )
        before = await database_state(api)
        assert_error(await upload(client, payload), 409, "IMPORT_CONFLICT")
        assert await database_state(api) == before
        payload["features"][1]["properties"]["name"] = "Допустимая школа"
        assert (await upload(client, payload)).status_code == 200

    @allure.story("IMPORT-11. Блокировка и таймаут не оставляют частичных данных")
    @pytest.mark.parametrize("failure", ["lock", "deadline"])
    async def test_busy_database(self, api: ApiContext, failure: str) -> None:
        client = await api.login("admin")
        settings = api.app.state.settings
        settings.import_lock_timeout_seconds = 0.1 if failure == "lock" else 5
        settings.import_timeout_seconds = 5 if failure == "lock" else 0.3
        before = await database_state(api)
        async with api.sessions() as db, db.begin():
            await db.scalar(select(DatasetState).where(DatasetState.id == 1).with_for_update())
            response = await asyncio.wait_for(upload(client, collection()), timeout=10)
        assert_error(
            response,
            503 if failure == "lock" else 504,
            "DATA_BUSY" if failure == "lock" else "REQUEST_TIMEOUT",
        )
        assert await database_state(api) == before

    @allure.story("IMPORT-12. Параллельные импорты выполняются целыми транзакциями")
    async def test_concurrent_imports(self, api: ApiContext) -> None:
        client = await api.login("admin")
        first, second = collection(count=2), collection(count=3)
        before = await database_state(api)
        responses = await asyncio.gather(upload(client, first), upload(client, second))
        assert [response.status_code for response in responses] == [200, 200]
        versions = [int(response.json()["dataset_version"]) for response in responses]
        original = before["dataset_state"][0]["version"]
        assert sorted(versions) == [original + 1, original + 2]
        winner = first if versions[0] > versions[1] else second
        after = await database_state(api)
        assert {row["source"] for row in after["gis_objects"]} == {
            winner["data_sources"][0]["code"]
        }
        assert len(after["gis_objects"]) == len(winner["features"])
        assert after["dataset_state"][0]["version"] == original + 2
        assert after["regions"] == before["regions"]

    @allure.story("IMPORT-13. Неизвестный слой не изменяет данные")
    async def test_unknown_layer(self, api: ApiContext) -> None:
        client = await api.login("admin")
        before = await database_state(api)
        assert_error(await upload(client, collection(), "missing"), 404, "LAYER_NOT_FOUND")
        assert await database_state(api) == before

    @allure.story("IMPORT-14. Отзыв прав во время загрузки запрещает запись")
    @pytest.mark.parametrize("change", ["demote", "deactivate"])
    async def test_permissions_changed_during_import(self, api: ApiContext, change: str) -> None:
        client = await api.login("admin")
        before = await database_state(api)
        waiting = asyncio.Event()
        engine = api.sessions.kw["bind"]

        def observe_lock(
            connection: Connection,
            cursor: object,
            statement: str,
            parameters: object,
            context: ExecutionContext,
            executemany: bool,
        ) -> None:
            if "dataset_state" in statement and "FOR UPDATE" in statement:
                waiting.set()

        # Событие синхронизирует тест с реальным SQL, не подменяя авторизацию/запись.
        async with api.sessions() as blocker, blocker.begin():
            await blocker.scalar(select(DatasetState).where(DatasetState.id == 1).with_for_update())
            event.listen(engine.sync_engine, "before_cursor_execute", observe_lock)
            pending = asyncio.create_task(upload(client, collection()))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=5)
                async with api.sessions() as db, db.begin():
                    query = update(User).where(User.id == api.user_ids["admin"])
                    await db.execute(
                        query.values(role="user")
                        if change == "demote"
                        else query.values(active=False)
                    )
            except BaseException:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                raise
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", observe_lock)
        response = await asyncio.wait_for(pending, timeout=5)
        assert_error(
            response,
            403 if change == "demote" else 401,
            "FORBIDDEN" if change == "demote" else "AUTH_REQUIRED",
        )
        assert await database_state(api) == before

    @allure.story("IMPORT-09. Поток без Content-Length ограничивается по фактическим байтам")
    async def test_streamed_request_limit(self, api: ApiContext) -> None:
        client = await api.login("admin")
        request = client.build_request(
            "POST",
            f"{API}/admin/layers/school/import",
            files={"file": ("layer.geojson", encoded(collection()))},
        )
        body = request.read()
        api.app.state.settings.import_max_request_bytes = len(body) - 1
        before = await database_state(api)

        async def chunks() -> AsyncIterator[bytes]:
            for offset in range(0, len(body), 64):
                yield body[offset : offset + 64]

        response = await client.post(
            request.url,
            content=chunks(),
            headers={"Content-Type": request.headers["Content-Type"]},
        )
        assert "Content-Length" not in response.request.headers
        assert_error(response, 413, "PAYLOAD_TOO_LARGE")
        assert await database_state(api) == before

    @allure.story("IMPORT-09. Точные границы лимитов допускаются, UTF-8 BOM поддерживается")
    async def test_exact_limits(self, api: ApiContext) -> None:
        client = await api.login("admin")
        payload = collection(count=2)
        content = b"\xef\xbb\xbf" + encoded(payload)
        settings = api.app.state.settings
        settings.import_max_file_bytes = len(content)
        settings.import_max_features = 2
        settings.import_max_positions = 2
        settings.import_max_positions_per_geometry = 1
        response = await upload(client, content, filename="layer.JSON")
        assert response.status_code == 200
        assert response.json()["imported_count"] == 2

    @allure.story("IMPORT-07. Существующий источник можно использовать без повторного описания")
    async def test_existing_source(self, api: ApiContext) -> None:
        client = await api.login("admin")
        before = await database_state(api)
        payload = collection()
        payload["data_sources"] = []
        payload["features"][0]["properties"]["source"] = before["data_sources"][0]["code"]
        assert (await upload(client, payload)).status_code == 200
        after = await database_state(api)
        assert after["data_sources"] == before["data_sources"]
