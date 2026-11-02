import json
from datetime import UTC

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.definitions import CATEGORY_NAMES
from app.contracts.read_api import Feature, Geometry, ObjectProperties, RegionProperties, SourceInfo
from app.core.errors import ApiError
from app.db.models import DatasetState, DataSource, GisObject, Region


async def dataset_version(db: AsyncSession, expected: str | None = None) -> str:
    version = await db.scalar(select(DatasetState.version).where(DatasetState.id == 1))
    if version is None:
        raise ApiError(503, "DATABASE_UNAVAILABLE", "База данных не подготовлена.", True)
    if expected is not None and expected != str(version):
        raise ApiError(409, "DATA_CHANGED", "Данные изменились. Повторите поиск.")
    return str(version)


def feature(record: GisObject | Region, geometry_json: str) -> Feature:
    geometry = Geometry.model_validate(json.loads(geometry_json))
    properties: ObjectProperties | RegionProperties
    if isinstance(record, GisObject):
        properties = ObjectProperties(
            name=record.name,
            category_code=record.category_code,
            category_name=CATEGORY_NAMES[record.category_code],
            geometry_type=geometry.type,
            status=record.status,
            rating=float(record.rating) if record.rating is not None else None,
            capacity=record.capacity,
            area_m2=record.area_m2,
            length_m=record.length_m,
            opening_date=record.opening_date,
            is_accessible=record.is_accessible,
            source=record.source,
            source_id=record.source_id,
            synthetic_fields=record.synthetic_fields,
            updated_at=record.updated_at.astimezone(UTC),
        )
    else:
        properties = RegionProperties(
            name=record.name, source=record.source, source_id=record.source_id
        )
    return Feature(id=str(record.id), geometry=geometry, properties=properties)


async def layer_features(db: AsyncSession, layer_code: str) -> list[Feature]:
    if layer_code == "regions":
        query = select(Region, func.ST_AsGeoJSON(Region.geom, 15)).order_by(Region.id)
    else:
        query = (
            select(GisObject, func.ST_AsGeoJSON(GisObject.geom, 15))
            .where(
                GisObject.category_code == layer_code,
            )
            .order_by(GisObject.id)
        )
    rows = await db.execute(query)
    return [feature(record, geometry) for record, geometry in rows]


async def card(db: AsyncSession, object_id: int, regions: bool) -> tuple[Feature, SourceInfo]:
    model = Region if regions else GisObject
    row = (
        await db.execute(
            select(model, func.ST_AsGeoJSON(model.geom, 15)).where(
                model.id == object_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "OBJECT_NOT_FOUND", "Объект не найден.")
    record, geometry = row
    source = await db.get(DataSource, record.source)
    if source is None:
        raise ApiError(500, "INTERNAL_ERROR", "Не удалось прочитать источник данных.")
    return feature(record, geometry), SourceInfo(
        code=source.code,
        title=source.title,
        url=source.url,
        usage_note=source.usage_note,
        snapshot_date=source.snapshot_date,
    )
