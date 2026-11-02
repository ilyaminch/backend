import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, JsonValue


def validate_bigint(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0|-?[1-9][0-9]*", value):
        raise ValueError("Ожидается каноническая десятичная строка bigint")
    if not -(2**63) <= int(value) < 2**63:
        raise ValueError("Значение выходит за диапазон bigint")
    return value


Bigint = Annotated[str, BeforeValidator(validate_bigint)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Geometry(Contract):
    type: Literal["Point", "LineString", "Polygon", "MultiPolygon"]
    coordinates: list[JsonValue]


class ObjectProperties(Contract):
    name: str
    category_code: str
    category_name: str
    geometry_type: str
    status: Literal["active", "closed", "unknown"]
    rating: float | None
    capacity: int | None
    area_m2: float | None
    length_m: float | None
    opening_date: date | None
    is_accessible: bool | None
    source: str
    source_id: str | None
    synthetic_fields: list[str]
    updated_at: datetime


class RegionProperties(Contract):
    name: str
    source: str
    source_id: str | None


class CountedRegionProperties(RegionProperties):
    object_count: int


class Feature(Contract):
    type: Literal["Feature"] = "Feature"
    id: Bigint
    geometry: Geometry
    properties: ObjectProperties | CountedRegionProperties | RegionProperties


class FeatureCollection(Contract):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[Feature]


class RegionCount(Contract):
    region_id: Bigint
    name: str
    count: int


class SearchResult(Contract):
    kind: Literal["objects", "regions", "region_counts"]
    dataset_version: Bigint
    category: str | None
    count: int
    feature_collection: FeatureCollection
    region_counts: list[RegionCount] | None = None
    matched_object_count: int | None
    map_action: Literal["replace"] = "replace"


class Layer(Contract):
    code: str
    name: str
    kind: Literal["thematic", "regions"]
    geometry_types: list[str]
    feature_count: int
    updated_at: datetime | None
    can_import: bool


class LayersResponse(Contract):
    request_id: UUID
    dataset_version: Bigint
    layers: list[Layer]


class ResultResponse(Contract):
    request_id: UUID
    result: SearchResult


class SourceInfo(Contract):
    code: str
    title: str
    url: str | None
    usage_note: str
    snapshot_date: date


class CardResponse(Contract):
    request_id: UUID
    dataset_version: Bigint
    feature: Feature
    source_info: SourceInfo
