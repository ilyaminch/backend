import json
from collections.abc import Iterator, Sequence

from pydantic import ValidationError

from app.catalog.definitions import LAYERS
from app.contracts.import_api import (
    ImportFeature,
    ImportGeometry,
    LineGeometry,
    PointGeometry,
    PolygonGeometry,
    Position,
    RegionImportCollection,
    ThematicImportCollection,
    ThematicImportProperties,
)
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.json import load_json

ImportCollection = ThematicImportCollection | RegionImportCollection
REGION_NAMES = {"Кировский", "Советский", "Ленинский", "Октябрьский"}


def invalid(path: str, code: str, *, conflict: bool = False) -> ApiError:
    return ApiError(
        409 if conflict else 422,
        "IMPORT_CONFLICT" if conflict else "IMPORT_INVALID",
        "Файл конфликтует с текущими данными."
        if conflict
        else "Файл не соответствует формату импорта.",
        details=[{"path": path, "code": code}],
    )


def geometry_positions(geometry: ImportGeometry) -> Iterator[Position]:
    if isinstance(geometry, PointGeometry):
        yield geometry.coordinates
    elif isinstance(geometry, LineGeometry):
        yield from geometry.coordinates
    else:
        polygons = (
            [geometry.coordinates]
            if isinstance(geometry, PolygonGeometry)
            else geometry.coordinates
        )
        for polygon in polygons:
            for ring in polygon:
                if ring[0] != ring[-1]:
                    raise ValueError("Ring is not closed")
                yield from ring


def parse_layer(content: bytes, layer_code: str, settings: Settings) -> ImportCollection:
    try:
        # Explicit UTF-8, not json.loads' automatic UTF-16/32 detection.
        value = load_json(content.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise invalid("file", "INVALID_JSON") from error
    if isinstance(value, dict) and isinstance(value.get("features"), list):
        if len(value["features"]) > settings.import_max_features:
            raise invalid("features", "TOO_MANY_FEATURES")
    try:
        collection: ImportCollection = (
            RegionImportCollection.model_validate(value)
            if layer_code == "regions"
            else ThematicImportCollection.model_validate(value)
        )
    except ValidationError as error:
        raise ApiError(
            422,
            "IMPORT_INVALID",
            "Файл не соответствует формату импорта.",
            details=[
                {"path": ".".join(map(str, e["loc"])), "code": e["type"]}
                for e in error.errors(include_input=False, include_context=False)
            ],
        ) from error
    if len(collection.features) > settings.import_max_features:
        raise invalid("features", "TOO_MANY_FEATURES")
    ids: set[str] = set()
    codes: set[str] = set()
    for index, source in enumerate(collection.data_sources):
        if source.code in codes:
            raise invalid(f"data_sources.{index}.code", "DUPLICATE_SOURCE")
        codes.add(source.code)
    total_positions = 0
    items: Sequence[ImportFeature] = collection.features
    for index, item in enumerate(items):
        path = f"features.{index}"
        if item.id in ids:
            raise invalid(path + ".id", "DUPLICATE_ID")
        ids.add(item.id)
        if isinstance(item.properties, ThematicImportProperties):
            if item.properties.category_code != layer_code:
                raise invalid(path + ".properties.category_code", "CATEGORY_MISMATCH")
        allowed = (
            {"Polygon", "MultiPolygon"} if layer_code == "regions" else {LAYERS[layer_code][1]}
        )
        if item.geometry.type not in allowed:
            raise invalid(path + ".geometry.type", "GEOMETRY_TYPE")
        count = 0
        try:
            for longitude, latitude in geometry_positions(item.geometry):
                if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                    raise ValueError("Coordinate outside EPSG:4326")
                count += 1
        except ValueError as error:
            raise invalid(path + ".geometry.coordinates", "COORDINATES_INVALID") from error
        if count > settings.import_max_positions_per_geometry:
            raise invalid(path + ".geometry", "TOO_MANY_POSITIONS")
        total_positions += count
        if total_positions > settings.import_max_positions:
            raise invalid("features", "TOO_MANY_POSITIONS")
    if layer_code == "regions":
        names = [item.properties.name for item in items]
        if len(names) != 4 or set(names) != REGION_NAMES:
            raise invalid("features", "REGION_SET_MISMATCH")
    return collection


def geometry_json(geometry: ImportGeometry) -> str:
    return json.dumps(geometry.model_dump(exclude_none=True), allow_nan=False)
