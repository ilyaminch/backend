"""Strict GeoJSON delivery profile. Coordinate repair is deliberately absent."""

import math
import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BeforeValidator,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.contracts.filter import CategoryCode
from app.contracts.read_api import Bigint, Contract


def checked_string(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("Ожидается текст без нулевых символов")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("Текст содержит некорректный Unicode") from error
    return value


def checked_text(value: object) -> str:
    text = checked_string(value)
    if not text.strip():
        raise ValueError("Ожидается непустой текст без нулевых символов")
    return text


def checked_date(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Ожидается календарная дата YYYY-MM-DD")
    return date.fromisoformat(value)


def checked_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    ):
        raise ValueError("Ожидается RFC3339 с часовым поясом")
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError) as error:
        raise ValueError("Дата выходит за поддерживаемый диапазон UTC") from error


def checked_number(value: object) -> int | float:
    if type(value) not in {int, float}:
        raise ValueError("Ожидается число без преобразования типов")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Ожидается конечное число")
    # Python integers are finite; do not overflow by coercing them to float.
    if isinstance(value, (int, float)):
        return value
    raise ValueError("Ожидается число")


Text = Annotated[StrictStr, BeforeValidator(checked_text)]
PlainText = Annotated[StrictStr, BeforeValidator(checked_string)]
DateValue = Annotated[date, BeforeValidator(checked_date)]
Timestamp = Annotated[datetime, BeforeValidator(checked_timestamp)]
Number = Annotated[StrictInt | StrictFloat, BeforeValidator(checked_number)]
Position = Annotated[list[Number], Field(min_length=2, max_length=2)]
Ring = Annotated[list[Position], Field(min_length=4)]
PolygonCoordinates = Annotated[list[Ring], Field(min_length=1)]
BBox = Annotated[list[Number], Field(min_length=4, max_length=4)]


class GeoContract(Contract):
    bbox: BBox | None = None

    @model_validator(mode="before")
    @classmethod
    def bbox_not_null(cls, value: object) -> object:
        if isinstance(value, dict) and "bbox" in value and value["bbox"] is None:
            raise ValueError("bbox должен быть массивом")
        return value

    @field_validator("bbox")
    @classmethod
    def bbox_ranges(cls, value: list[int | float] | None) -> list[int | float] | None:
        if value is not None:
            west, south, east, north = value
            if (
                not -180 <= west <= 180
                or not -180 <= east <= 180
                or not -90 <= south <= north <= 90
            ):
                raise ValueError("bbox выходит за диапазон EPSG:4326")
        return value


class PointGeometry(GeoContract):
    type: Literal["Point"]
    coordinates: Position


class LineGeometry(GeoContract):
    type: Literal["LineString"]
    coordinates: Annotated[list[Position], Field(min_length=2)]


class PolygonGeometry(GeoContract):
    type: Literal["Polygon"]
    coordinates: PolygonCoordinates


class MultiPolygonGeometry(GeoContract):
    type: Literal["MultiPolygon"]
    coordinates: Annotated[list[PolygonCoordinates], Field(min_length=1)]


ImportGeometry = Annotated[
    PointGeometry | LineGeometry | PolygonGeometry | MultiPolygonGeometry,
    Field(discriminator="type"),
]


class ImportSource(Contract):
    code: Text
    title: Text
    url: Text | None = None
    usage_note: Text
    snapshot_date: DateValue

    @field_validator("url")
    @classmethod
    def source_url(cls, value: str | None) -> str | None:
        if value is not None:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
            ):
                raise ValueError("Ожидается HTTP(S) URL без учётных данных")
            _ = url.port
        return value

    @model_validator(mode="after")
    def generated_source(self) -> Self:
        if self.code == "generated" and self.url is not None:
            raise ValueError("У generated нет внешнего URL")
        return self


class ThematicImportProperties(Contract):
    name: Text
    category_code: CategoryCode
    status: Literal["active", "closed", "unknown"]
    rating: Annotated[Number, Field(ge=1, le=5)] | None = None
    # Storage type is PostgreSQL int4, not an arbitrary precision integer.
    capacity: Annotated[StrictInt, Field(ge=0, le=2147483647)] | None = None
    area_m2: Annotated[Number, Field(ge=0)] | None = None
    length_m: Annotated[Number, Field(ge=0)] | None = None
    opening_date: DateValue | None = None
    is_accessible: StrictBool | None = None
    source: Text
    source_id: PlainText | None = None
    synthetic_fields: list[Text]
    updated_at: Timestamp

    @field_validator("synthetic_fields")
    @classmethod
    def synthetic_names(cls, value: list[str]) -> list[str]:
        # "geom" marks synthetic geometry in the delivered verification data.
        allowed = (set(cls.model_fields) - {"source", "synthetic_fields"}) | {"geom"}
        if len(set(value)) != len(value) or not set(value) <= allowed:
            raise ValueError("Повторяющееся или неизвестное искусственное свойство")
        return value


class RegionImportProperties(Contract):
    name: Text
    source: Text
    source_id: PlainText | None = None


class ThematicImportFeature(GeoContract):
    type: Literal["Feature"]
    id: Bigint
    geometry: ImportGeometry
    properties: ThematicImportProperties


class RegionImportFeature(GeoContract):
    type: Literal["Feature"]
    id: Bigint
    geometry: ImportGeometry
    properties: RegionImportProperties


ImportFeature = ThematicImportFeature | RegionImportFeature


class ThematicImportCollection(GeoContract):
    type: Literal["FeatureCollection"]
    schema_version: Literal["1"]
    data_sources: list[ImportSource]
    features: Annotated[list[ThematicImportFeature], Field(min_length=1)]


class RegionImportCollection(GeoContract):
    type: Literal["FeatureCollection"]
    schema_version: Literal["1"]
    data_sources: list[ImportSource]
    features: Annotated[list[RegionImportFeature], Field(min_length=1)]


class ImportResponse(Contract):
    request_id: UUID
    layer_code: str
    imported_count: int
    dataset_version: Bigint
    message: str
    suggest_repeat_search: Literal[True] = True
