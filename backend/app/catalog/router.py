from datetime import UTC
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from app.auth.dependencies import CurrentUser
from app.catalog.capabilities import CapabilitiesResponse, capabilities
from app.catalog.definitions import LAYERS
from app.contracts.errors import ErrorResponse
from app.contracts.filter import FilterV1
from app.contracts.read_api import (
    Bigint,
    CardResponse,
    FeatureCollection,
    Layer,
    LayersResponse,
    ResultResponse,
    SearchResult,
)
from app.core.errors import ApiError
from app.db.models import GisObject, LayerState, Region
from app.db.session import snapshot
from app.geo.read import card, dataset_version, layer_features
from app.geo.search import search

router = APIRouter(
    tags=["Слои и объекты"],
    responses={status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 422, 503)},
)


@router.get("/query-capabilities", response_model=CapabilitiesResponse)
async def query_capabilities(request: Request, _principal: CurrentUser) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        request_id=UUID(request.state.request_id),
        **capabilities(request.app.state.settings).model_dump(),
    )


@router.post("/search", response_model=ResultResponse)
async def direct_search(
    body: FilterV1, request: Request, _principal: CurrentUser
) -> ResultResponse:
    async with snapshot(request.app.state.sessions) as db:
        result = await search(db, body)
    return ResultResponse(request_id=UUID(request.state.request_id), result=result)


@router.get("/layers", response_model=LayersResponse)
async def layers(request: Request, principal: CurrentUser) -> LayersResponse:
    async with snapshot(request.app.state.sessions) as db:
        version = await dataset_version(db)
        count_rows = await db.execute(
            select(GisObject.category_code, func.count(GisObject.id)).group_by(
                GisObject.category_code
            )
        )
        counts = {code: count for code, count in count_rows}
        counts["regions"] = (await db.scalar(select(func.count(Region.id)))) or 0
        states = {state.layer_code: state for state in (await db.scalars(select(LayerState)))}
        if set(states) != set(LAYERS):
            raise ApiError(503, "DATABASE_UNAVAILABLE", "Каталог слоёв не подготовлен.", True)
        updated = {
            code: state.imported_at.astimezone(UTC) if state.imported_at else None
            for code, state in states.items()
        }
        result = [
            Layer(
                code=code,
                name=name,
                kind="regions" if code == "regions" else "thematic",
                geometry_types=["Polygon", "MultiPolygon"] if code == "regions" else [geometry],
                feature_count=counts.get(code, 0),
                updated_at=updated[code],
                can_import=principal.user.role == "admin",
            )
            for code, (name, geometry) in LAYERS.items()
        ]
    return LayersResponse(
        request_id=UUID(request.state.request_id), dataset_version=version, layers=result
    )


@router.get("/layers/{layer_code}/features", response_model=ResultResponse)
async def features(layer_code: str, request: Request, _principal: CurrentUser) -> ResultResponse:
    async with snapshot(request.app.state.sessions) as db:
        if layer_code not in LAYERS:
            raise ApiError(404, "LAYER_NOT_FOUND", "Слой не найден.")
        version = await dataset_version(db)
        found = await layer_features(db, layer_code)
    regions = layer_code == "regions"
    return ResultResponse(
        request_id=UUID(request.state.request_id),
        result=SearchResult(
            kind="regions" if regions else "objects",
            dataset_version=version,
            category=None if regions else layer_code,
            count=len(found),
            feature_collection=FeatureCollection(features=found),
            matched_object_count=None if regions else len(found),
        ),
    )


async def get_card(
    request: Request,
    object_id: str,
    expected: str | None,
    regions: bool,
) -> CardResponse:
    async with snapshot(request.app.state.sessions) as db:
        version = await dataset_version(db, expected)
        found, source = await card(db, int(object_id), regions)
    return CardResponse(
        request_id=UUID(request.state.request_id),
        dataset_version=version,
        feature=found,
        source_info=source,
    )


@router.get("/objects/{object_id}", response_model=CardResponse)
async def object_card(
    object_id: Bigint,
    request: Request,
    _principal: CurrentUser,
    dataset_version: Bigint | None = None,
) -> CardResponse:
    return await get_card(request, object_id, dataset_version, False)


@router.get("/regions/{region_id}", response_model=CardResponse)
async def region_card(
    region_id: Bigint,
    request: Request,
    _principal: CurrentUser,
    dataset_version: Bigint | None = None,
) -> CardResponse:
    return await get_card(request, region_id, dataset_version, True)
