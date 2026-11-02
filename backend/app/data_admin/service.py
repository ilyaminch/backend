import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from fastapi import Request
from geoalchemy2 import Geography, WKBElement
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Integer,
    Text,
    any_,
    bindparam,
    case,
    cast,
    column,
    delete,
    func,
    insert,
    null,
    select,
    text,
    values,
)
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import authenticate, ensure_admin
from app.contracts.import_api import ImportFeature, ImportSource, ThematicImportProperties
from app.core.errors import ApiError
from app.data_admin.validation import ImportCollection, geometry_json, invalid
from app.db.models import DatasetState, DataSource, GisObject, LayerState, Region


@dataclass(frozen=True)
class PreparedLayer:
    layer_code: str
    collection: ImportCollection
    rows: list[dict[str, Any]]


async def prepare_layer(
    db: AsyncSession,
    collection: ImportCollection,
    layer_code: str,
) -> PreparedLayer:
    """Validate all geometries and compute geography metrics before any write."""
    rows: list[dict[str, Any]] = []
    items: Sequence[ImportFeature] = collection.features
    for offset in range(0, len(items), 128):
        batch = items[offset : offset + 128]
        incoming = values(column("i", Integer), column("gjson", Text), name="incoming").data(
            [(offset + index, geometry_json(item.geometry)) for index, item in enumerate(batch)]
        )
        geometries = (
            select(
                incoming.c.i,
                func.ST_SetSRID(func.ST_GeomFromGeoJSON(incoming.c.gjson), 4326).label("geom"),
            )
            .cte("geometries")
            .prefix_with("MATERIALIZED")
        )
        geom = geometries.c.geom
        valid = func.ST_IsValid(geom)
        metric = (
            func.ST_Area(cast(geom, Geography(srid=4326)))
            if layer_code == "park"
            else func.ST_Length(cast(geom, Geography(srid=4326)))
        )
        query = select(
            geometries.c.i,
            valid,
            func.ST_IsEmpty(geom),
            func.ST_NDims(geom),
            func.ST_SRID(geom),
            func.ST_AsEWKB(geom),
            case((valid, metric), else_=None)
            if layer_code in {"park", "improvement_zone"}
            else null(),
        ).order_by(geometries.c.i)
        for index, is_valid, empty, dimensions, srid, wkb, measured in await db.execute(query):
            if not is_valid or empty or dimensions != 2 or srid != 4326:
                raise invalid(f"features.{index}.geometry", "GEOMETRY_INVALID")
            item = items[index]
            properties = item.properties
            row: dict[str, Any] = {
                "id": int(item.id),
                "geom": WKBElement(bytes(wkb), srid=4326, extended=True),
                "name": properties.name,
                "source": properties.source,
                "source_id": properties.source_id,
            }
            if isinstance(properties, ThematicImportProperties):
                row.update(
                    category_code=layer_code,
                    status=properties.status,
                    rating=properties.rating,
                    capacity=properties.capacity,
                    opening_date=properties.opening_date,
                    is_accessible=properties.is_accessible,
                    synthetic_fields=properties.synthetic_fields,
                    updated_at=properties.updated_at,
                    area_m2=measured if layer_code == "park" else None,
                    length_m=measured if layer_code == "improvement_zone" else None,
                )
            rows.append(row)
    return PreparedLayer(layer_code, collection, rows)


async def check_sources(db: AsyncSession, prepared: PreparedLayer) -> list[ImportSource]:
    declared = {source.code: source for source in prepared.collection.data_sources}
    referenced = {row["source"] for row in prepared.rows}
    codes = list(set(declared) | referenced)
    found = {
        source.code: source
        for source in await db.scalars(
            select(DataSource).where(
                DataSource.code == any_(bindparam("source_codes", codes, type_=ARRAY(Text)))
            )
        )
    }
    for index, source in enumerate(prepared.collection.data_sources):
        existing = found.get(source.code)
        if existing is not None and any(
            getattr(existing, key) != value for key, value in source.model_dump().items()
        ):
            raise invalid(f"data_sources.{index}", "SOURCE_CONFLICT", conflict=True)
    for index, row in enumerate(prepared.rows):
        if row["source"] not in declared and row["source"] not in found:
            raise invalid(f"features.{index}.properties.source", "SOURCE_UNKNOWN")
    return [source for source in declared.values() if source.code not in found]


async def check_ids(db: AsyncSession, prepared: PreparedLayer) -> None:
    if prepared.layer_code == "regions":
        return
    conflict = await db.scalar(
        select(GisObject.id)
        .where(
            GisObject.category_code != prepared.layer_code,
            GisObject.id
            == any_(
                bindparam(
                    "import_ids", [row["id"] for row in prepared.rows], type_=ARRAY(BigInteger)
                )
            ),
        )
        .limit(1)
    )
    if conflict is not None:
        index = next(i for i, row in enumerate(prepared.rows) if row["id"] == conflict)
        raise invalid(f"features.{index}.id", "ID_CONFLICT", conflict=True)


async def confirmed_commit(db: AsyncSession) -> None:
    # Once COMMIT is in flight, wait for its actual outcome. A timeout/cancel
    # must not return an error while an unobserved commit keeps running.
    task = asyncio.create_task(db.commit())
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()


async def replace_layer(request: Request, prepared: PreparedLayer, deadline: float) -> str:
    settings = request.app.state.settings
    commit_started = False
    async with request.app.state.sessions() as db:
        try:
            async with asyncio.timeout_at(deadline):
                await db.begin()
                await db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                remaining_ms = max(1, int((deadline - monotonic()) * 1000))
                await db.execute(
                    select(func.set_config("statement_timeout", str(remaining_ms), True))
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
                ensure_admin(await authenticate(request, db))
                layer_state = await db.get(LayerState, prepared.layer_code)
                if layer_state is None:
                    raise ApiError(503, "DATABASE_UNAVAILABLE", "Каталог слоёв не подготовлен.")
                sources = await check_sources(db, prepared)
                await check_ids(db, prepared)
                if await request.is_disconnected():
                    raise ApiError(409, "IMPORT_CANCELLED", "Загрузка отменена.")
                if sources:
                    await db.execute(insert(DataSource), [s.model_dump() for s in sources])
                model = Region if prepared.layer_code == "regions" else GisObject
                deletion = delete(model)
                if model is GisObject:
                    deletion = deletion.where(GisObject.category_code == prepared.layer_code)
                await db.execute(deletion)
                await db.execute(insert(model).execution_options(render_nulls=True), prepared.rows)
                now = datetime.now(UTC)
                state.version += 1
                state.changed_at = now
                layer_state.version = state.version
                layer_state.imported_at = now
                await db.flush()
                ensure_admin(await authenticate(request, db))
                if await request.is_disconnected():
                    raise ApiError(409, "IMPORT_CANCELLED", "Загрузка отменена.")
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
            if monotonic() >= deadline:
                raise ApiError(504, "REQUEST_TIMEOUT", "Время обработки файла истекло.")
            commit_started = True
            await confirmed_commit(db)
            return version
        except BaseException as error:
            await db.rollback()
            if isinstance(error, DBAPIError):
                if commit_started:
                    raise ApiError(
                        503,
                        "IMPORT_RESULT_UNKNOWN",
                        "Не удалось подтвердить результат импорта. Обновите реестр слоёв.",
                    ) from error
                code = getattr(error.orig, "sqlstate", None)
                if code == "55P03":
                    raise ApiError(
                        503, "DATA_BUSY", "Набор занят. Повторите импорт позже.", True
                    ) from error
                if code == "57014":
                    raise ApiError(
                        504, "REQUEST_TIMEOUT", "Время обработки файла истекло."
                    ) from error
                if isinstance(error, IntegrityError):
                    raise invalid("file", "DATABASE_CONFLICT", conflict=True) from error
            raise
