from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    ColumnElement,
    Numeric,
    SQLColumnExpression,
    and_,
    any_,
    bindparam,
    cast,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.filter import (
    BetweenCondition,
    FilterV1,
    InCondition,
    Value,
    YearCondition,
)
from app.contracts.read_api import (
    CountedRegionProperties,
    FeatureCollection,
    RegionCount,
    SearchResult,
)
from app.core.errors import ApiError
from app.db.models import GisObject, Region
from app.geo.read import dataset_version, feature

_COLUMNS = {
    "id": GisObject.id,
    "name": GisObject.name,
    "status": GisObject.status,
    "rating": GisObject.rating,
    "capacity": GisObject.capacity,
    "area_m2": GisObject.area_m2,
    "length_m": GisObject.length_m,
    "opening_date": GisObject.opening_date,
    "is_accessible": GisObject.is_accessible,
    "source": GisObject.source,
    "source_id": GisObject.source_id,
}


def db_value(field: str, value: Value) -> str | int | float | bool | date | Decimal:
    if field == "id":
        return int(value)
    if field == "opening_date":
        return date.fromisoformat(str(value))
    if field in {"rating", "capacity", "area_m2", "length_m"}:
        return Decimal(str(value))
    return value


def predicate(query: FilterV1) -> ColumnElement[bool]:
    expressions: list[ColumnElement[bool]] = [GisObject.category_code == query.category]
    for condition in query.conditions:
        column: SQLColumnExpression[Any] = _COLUMNS[condition.field]
        if condition.field in {"rating", "capacity", "area_m2", "length_m"}:
            # Comparing a valid huge threshold must not overflow asyncpg int4/float8.
            column = cast(column, Numeric)
        expression: ColumnElement[bool]
        if isinstance(condition, BetweenCondition):
            expression = column.between(
                db_value(condition.field, condition.lower),
                db_value(condition.field, condition.upper),
            )
        elif isinstance(condition, InCondition):
            element_type = (
                Numeric()
                if condition.field in {"rating", "capacity", "area_m2", "length_m"}
                else _COLUMNS[condition.field].type
            )
            values = bindparam(
                None,
                [db_value(condition.field, value) for value in condition.values],
                type_=ARRAY(element_type),
            )
            expression = column == any_(values)
        elif isinstance(condition, YearCondition):
            expression = column.between(date(condition.value, 1, 1), date(condition.value, 12, 31))
        else:
            value = db_value(condition.field, condition.value)
            if condition.op == "contains":
                escaped = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                expression = column.ilike(f"%{escaped}%", escape="\\")
            else:
                operations = {
                    "eq": column.__eq__,
                    "gt": column.__gt__,
                    "gte": column.__ge__,
                    "lt": column.__lt__,
                    "lte": column.__le__,
                }
                expression = operations[condition.op](value)
        expressions.append(expression)
    return and_(*expressions)


async def search(db: AsyncSession, query: FilterV1) -> SearchResult:
    version = await dataset_version(db)
    matches = predicate(query)
    if query.operation == "search_objects":
        rows = await db.execute(
            select(GisObject, func.ST_AsGeoJSON(GisObject.geom, 15))
            .where(matches)
            .order_by(GisObject.id)
        )
        found = [feature(record, geometry) for record, geometry in rows]
        return SearchResult(
            kind="objects",
            dataset_version=version,
            category=query.category,
            count=len(found),
            feature_collection=FeatureCollection(features=found),
            matched_object_count=len(found),
        )
    count = await db.scalar(select(func.count(GisObject.id)).where(matches))
    matching = select(GisObject.id, GisObject.geom).where(matches).subquery()
    counts = (
        select(Region.id.label("region_id"), func.count(func.distinct(matching.c.id)).label("n"))
        .select_from(Region)
        .outerjoin(matching, func.ST_Intersects(Region.geom, matching.c.geom))
        .group_by(Region.id)
        .subquery()
    )
    rows = await db.execute(
        select(Region, func.ST_AsGeoJSON(Region.geom, 15), counts.c.n)
        .join(counts, counts.c.region_id == Region.id)
        .order_by(Region.name, Region.id)
    )
    found, region_counts = [], []
    for record, geometry, total in rows:
        item = feature(record, geometry)
        item.properties = CountedRegionProperties(
            **item.properties.model_dump(), object_count=total
        )
        found.append(item)
        region_counts.append(RegionCount(region_id=str(record.id), name=record.name, count=total))
    if len(found) != 4:
        raise ApiError(503, "DATA_BUSY", "Набор из четырёх районов ещё не подготовлен.", True)
    return SearchResult(
        kind="region_counts",
        dataset_version=version,
        category=query.category,
        count=len(found),
        feature_collection=FeatureCollection(features=found),
        region_counts=region_counts,
        matched_object_count=count,
    )


def result_message(result: SearchResult) -> str:
    if result.kind == "region_counts":
        return (
            "Количество объектов по районам: "
            + "; ".join(f"{item.name}: {item.count}" for item in result.region_counts or [])
            + "."
        )
    if result.count == 0:
        return "По этим условиям ничего не найдено"
    return f"Найдено объектов: {result.count}."
