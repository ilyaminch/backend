"""Импорт заменяет слои целиком: каждый случай получает отдельную схему gis_test."""

import os
import runpy
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

import app.main
from app.core.config import Settings


@pytest.fixture(autouse=True)
async def isolated_import_database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        pytest.fail("Для тестов импорта задайте TEST_DATABASE_URL БД gis_test.", pytrace=False)
    url = make_url(raw_url)
    if url.drivername != "postgresql+asyncpg" or url.database != "gis_test":
        pytest.fail(
            "Тестам импорта разрешена только PostgreSQL/asyncpg БД gis_test.", pytrace=False
        )

    schema = "test_import_" + uuid4().hex
    admin_engine = create_async_engine(url)
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )

    def migrate(connection: Connection) -> None:
        revision = Path(__file__).resolve().parents[2] / "migrations/versions/0001_initial.py"
        with Operations.context(MigrationContext.configure(connection)):
            runpy.run_path(str(revision))["upgrade"]()

    def sessions(_settings: Settings) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with admin_engine.begin() as db:
            # PostGIS уже должен быть установлен миграцией в public: иначе DROP
            # временной схемы мог бы удалить созданное в ней расширение.
            revision = await db.scalar(text("SELECT version_num FROM public.alembic_version"))
            assert revision == app.main.MIGRATION_HEAD, "Сначала выполните alembic upgrade head"
            await db.execute(CreateSchema(schema))
        async with engine.begin() as db:
            await db.run_sync(migrate)
        # Меняем только соединения: настоящие маршруты, авторизация, миграция и SQL.
        monkeypatch.setattr(app.main, "create_sessions", sessions)
        yield
    finally:
        await engine.dispose()
        async with admin_engine.begin() as db:
            await db.execute(DropSchema(schema, cascade=True, if_exists=True))
        await admin_engine.dispose()
