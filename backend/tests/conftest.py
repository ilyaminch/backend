import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import delete, func
from sqlalchemy.engine import make_url

from app.auth.dependencies import AdminUser
from app.auth.passwords import hasher
from app.core.config import Settings
from app.db.models import AuthSession, DataSource, GisObject, Region, User
from app.main import create_app
from tests.api_support import ORIGIN, PASSWORD, ApiContext, ControlledDialogue, candidate


@pytest.fixture(scope="session")
def pipeline() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / ".gitlab-ci.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def password_hash() -> str:
    return hasher.hash(PASSWORD)


@pytest.fixture
async def api(request: pytest.FixtureRequest, password_hash: str) -> AsyncIterator[ApiContext]:
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        pytest.fail(
            "Для API-тестов задайте TEST_DATABASE_URL выделенной БД gis_test.", pytrace=False
        )
    url = make_url(raw_url)
    if url.drivername != "postgresql+asyncpg" or url.database != "gis_test":
        pytest.fail("API-тестам разрешена только PostgreSQL/asyncpg БД gis_test.", pytrace=False)

    settings = Settings(
        _env_file=None,
        database_url=raw_url,
        app_env="test",
        public_origin=ORIGIN,
        cookie_secure=False,
        dialogue_factory=None,
        llm_base_url=None,
        llm_model=None,
        login_failed_attempts=2,
        login_ip_attempts=100,
    )
    # Уникальные записи позволяют чистить только данные этого теста,
    # сохраняя миграции, справочники и любые посторонние записи БД.
    suffix = uuid4().hex
    first_id = uuid4().int % (2**60) + 1
    object_ids = [str(first_id), str(first_id + 1)]
    region_id = str(first_id + 2)
    source_code = f"test-{suffix}"
    user_ids = {name: uuid4() for name in ("user", "other", "admin")}
    logins = {name: f"test-{name}-{suffix}" for name in user_ids}
    dialogue = ControlledDialogue(candidate([object_ids[0]], completed=True))
    app = create_app(settings, dialogue=dialogue if getattr(request, "param", True) else None)
    app.state.admin_calls = 0

    @app.post("/test/admin-operation")
    async def admin_operation(_principal: AdminUser) -> dict[str, bool]:
        app.state.admin_calls += 1
        return {"ok": True}

    async with app.router.lifespan_context(app), AsyncExitStack() as stack:
        sessions = app.state.sessions
        try:
            async with sessions() as db, db.begin():
                now = datetime.now(UTC)
                db.add_all(
                    User(
                        id=user_ids[name],
                        login=logins[name],
                        password_hash=password_hash,
                        role="admin" if name == "admin" else "user",
                        active=True,
                        created_at=now,
                    )
                    for name in user_ids
                )
                db.add(
                    DataSource(
                        code=source_code,
                        title="Искусственные данные API-теста",
                        url=None,
                        usage_note="Только для автотестов",
                        snapshot_date=date(2026, 1, 1),
                    )
                )
                await db.flush()
                for index, object_id in enumerate(object_ids):
                    db.add(
                        GisObject(
                            id=int(object_id),
                            name=f"Тестовая школа {index + 1}",
                            category_code="school",
                            geom=func.ST_GeomFromText(f"POINT({84 + index} 56)", 4326),
                            status="active",
                            rating=4,
                            capacity=100,
                            opening_date=date(2000, 1, 1),
                            is_accessible=True,
                            source=source_code,
                            source_id=str(index),
                            synthetic_fields=[],
                            updated_at=now,
                        )
                    )
                db.add(
                    Region(
                        id=int(region_id),
                        name=f"Тестовый район {suffix}",
                        geom=func.ST_GeomFromText("POLYGON((83 55,86 55,86 57,83 57,83 55))", 4326),
                        source=source_code,
                        source_id="region",
                    )
                )
            yield ApiContext(
                app, dialogue, stack, user_ids, logins, object_ids, region_id, sessions
            )
        finally:
            # Завершаем фоновые шаги до удаления связанных пользователей.
            app.state.chat_store.shutdown()
            dialogue.release.set()
            if app.state.chat_store.tasks:
                await asyncio.wait_for(
                    asyncio.gather(*app.state.chat_store.tasks, return_exceptions=True), timeout=5
                )
            async with sessions() as db, db.begin():
                await db.execute(
                    delete(AuthSession).where(AuthSession.user_id.in_(user_ids.values()))
                )
                await db.execute(delete(User).where(User.id.in_(user_ids.values())))
                await db.execute(delete(GisObject).where(GisObject.source == source_code))
                await db.execute(delete(Region).where(Region.source == source_code))
                await db.execute(delete(DataSource).where(DataSource.code == source_code))
