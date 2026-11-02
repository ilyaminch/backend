import asyncio
from datetime import UTC, datetime
from time import monotonic

from fastapi import Request
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.dependencies import authenticate, ensure_admin
from app.catalog.definitions import CATEGORY_NAMES, LAYERS
from app.contracts.demo import DemoResult
from app.core.config import Settings
from app.core.errors import ApiError
from app.data_admin.demo_package import DemoPackage, check_deadline, load_package, package_invalid
from app.data_admin.service import PreparedLayer, confirmed_commit, prepare_layer
from app.db.models import Category, DatasetState, DataSource, GisObject, LayerState, Region
from app.db.session import snapshot


async def check_request(request: Request | None, db: AsyncSession) -> None:
    if request is not None:
        ensure_admin(await authenticate(request, db))
        if await request.is_disconnected():
            raise ApiError(409, "RESTORE_CANCELLED", "Восстановление отменено.")


async def write_package(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    package: DemoPackage,
    prepared: list[PreparedLayer],
    deadline: float,
    *,
    initialize: bool,
    request: Request | None,
) -> str:
    commit_started = False
    async with sessions() as db:
        try:
            async with asyncio.timeout_at(deadline):
                await db.begin()
                await db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                await db.execute(
                    select(
                        func.set_config(
                            "statement_timeout",
                            str(max(1, int((deadline - monotonic()) * 1000))),
                            True,
                        )
                    )
                )
                await db.execute(
                    select(
                        func.set_config(
                            "lock_timeout",
                            str(max(1, int(settings.import_lock_timeout_seconds * 1000))),
                            True,
                        )
                    )
                )
                state = await db.scalar(
                    select(DatasetState).where(DatasetState.id == 1).with_for_update()
                )
                if state is None:
                    raise ApiError(503, "DATABASE_UNAVAILABLE", "База данных не подготовлена.")
                await check_request(request, db)
                layer_states = list(await db.scalars(select(LayerState)))
                if {item.layer_code for item in layer_states} != set(LAYERS):
                    raise ApiError(503, "DATABASE_UNAVAILABLE", "Каталог слоёв не подготовлен.")
                if initialize and (
                    await db.scalar(select(func.count()).select_from(GisObject))
                    or await db.scalar(select(func.count()).select_from(Region))
                    or any(item.imported_at is not None for item in layer_states)
                ):
                    raise ApiError(
                        409,
                        "DATASET_NOT_EMPTY",
                        "Начальная загрузка разрешена только до первого импорта геоданных.",
                    )
                await db.execute(delete(GisObject))
                await db.execute(delete(Region))
                await db.execute(delete(DataSource))
                await db.execute(delete(Category))
                await db.execute(
                    insert(Category),
                    [
                        {"code": code, "name": name, "geometry_type": LAYERS[code][1]}
                        for code, name in CATEGORY_NAMES.items()
                    ],
                )
                await db.execute(insert(DataSource), [s.model_dump() for s in package.sources])
                for layer in prepared:
                    model = Region if layer.layer_code == "regions" else GisObject
                    await db.execute(insert(model).execution_options(render_nulls=True), layer.rows)
                now = datetime.now(UTC)
                state.version += 1
                state.changed_at = now
                for item in layer_states:
                    item.version = state.version
                    item.imported_at = now
                await db.flush()
                await check_request(request, db)
                version = str(state.version)
                await db.execute(
                    select(
                        func.set_config(
                            "statement_timeout",
                            str(max(1, int((deadline - monotonic()) * 1000))),
                            True,
                        )
                    )
                )
            check_deadline(deadline)
            commit_started = True
            await confirmed_commit(db)
            return version
        except BaseException as error:
            await db.rollback()
            if isinstance(error, DBAPIError):
                if commit_started:
                    raise ApiError(
                        503,
                        "RESTORE_RESULT_UNKNOWN",
                        "Не удалось подтвердить восстановление. Обновите реестр слоёв.",
                    ) from error
                code = getattr(error.orig, "sqlstate", None)
                if code == "55P03":
                    raise ApiError(
                        503, "DATA_BUSY", "Набор занят. Повторите позже.", True
                    ) from error
                if code == "57014":
                    raise ApiError(
                        504, "REQUEST_TIMEOUT", "Время обработки демонабора истекло."
                    ) from error
                if isinstance(error, IntegrityError):
                    raise package_invalid("DATABASE_CONFLICT") from error
            raise


async def install_demo(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    initialize: bool = False,
    request: Request | None = None,
    deadline: float | None = None,
) -> DemoResult:
    deadline = deadline if deadline is not None else monotonic() + settings.import_timeout_seconds
    try:
        async with asyncio.timeout_at(deadline):
            package = await asyncio.to_thread(load_package, settings, deadline)
            async with snapshot(sessions) as db:
                prepared = [
                    await prepare_layer(db, collection, code)
                    for code, collection in package.layers.items()
                ]
        version = await write_package(
            settings, sessions, package, prepared, deadline, initialize=initialize, request=request
        )
    except TimeoutError as error:
        raise ApiError(504, "REQUEST_TIMEOUT", "Время обработки демонабора истекло.") from error
    except ApiError as error:
        if error.code in {"IMPORT_INVALID", "IMPORT_CONFLICT"}:
            raise package_invalid("INVALID_GEOMETRY") from error
        raise
    return DemoResult(
        dataset_version=version,
        layer_counts={
            entry.layer_code: len(package.layers[entry.layer_code].features)
            for entry in package.manifest.files
        },
    )
