import copy
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
from sqlalchemy import Table, func, select

from app.db.models import DatasetState, DataSource, GisObject, LayerState, Region
from tests.api_support import API, ApiContext

FIRST_ID = 9007199254740993
REGION_NAMES = ("Кировский", "Советский", "Ленинский", "Октябрьский")


def collection(layer: str = "school", count: int = 1) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "contracts/examples/import-school.geojson"
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    source = "test-import-" + uuid4().hex
    payload["data_sources"][0]["code"] = source
    example = payload["features"][0]
    payload["features"] = []
    for index in range(4 if layer == "regions" else count):
        feature = copy.deepcopy(example)
        feature["id"] = str(FIRST_ID + index)
        feature["properties"]["source"] = source
        feature["properties"]["category_code"] = layer
        ring = [[84, 56], [84.01, 56], [84.01, 56.01], [84, 56.01], [84, 56]]
        if layer in {"park", "regions"}:
            feature["geometry"] = {"type": "Polygon", "coordinates": [ring]}
        elif layer == "improvement_zone":
            feature["geometry"] = {"type": "LineString", "coordinates": ring[:2]}
        if layer == "regions":
            feature["properties"] = {"name": REGION_NAMES[index], "source": source}
            if index == 3:
                feature["geometry"] = {"type": "MultiPolygon", "coordinates": [[ring]]}
        payload["features"].append(feature)
    return payload


def encoded(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


async def upload(
    client: httpx.AsyncClient,
    payload: dict[str, Any] | bytes,
    layer: str = "school",
    filename: str = "layer.geojson",
) -> httpx.Response:
    return await client.post(
        f"{API}/admin/layers/{layer}/import",
        files={"file": (filename, encoded(payload) if isinstance(payload, dict) else payload)},
    )


async def database_state(api: ApiContext) -> dict[str, list[dict[str, Any]]]:
    """Сравниваем все геоданные, источники и метаданные, включая время и геометрию."""
    state = {}
    async with api.sessions() as db:
        for model in (DataSource, GisObject, Region, DatasetState, LayerState):
            table = cast(Table, model.__table__)
            columns = [
                func.ST_AsEWKT(column).label(column.name) if column.name == "geom" else column
                for column in table.columns
            ]
            rows = await db.execute(select(*columns).order_by(*table.primary_key.columns))
            state[table.name] = [dict(row) for row in rows.mappings()]
    return state
