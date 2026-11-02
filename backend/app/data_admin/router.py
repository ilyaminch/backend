import asyncio
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import ValidationError

from app.auth.dependencies import AdminUser
from app.catalog.definitions import LAYERS
from app.contracts.demo import RestoreInput, RestoreResponse
from app.contracts.errors import ErrorResponse
from app.contracts.import_api import ImportResponse
from app.core.errors import ApiError
from app.core.json import load_json
from app.data_admin.demo_service import install_demo
from app.data_admin.service import prepare_layer, replace_layer
from app.data_admin.upload import read_upload
from app.data_admin.validation import parse_layer
from app.db.session import snapshot

router = APIRouter(
    prefix="/admin",
    tags=["Администрирование"],
    responses={
        status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 409, 413, 422, 503, 504)
    },
)


@router.post(
    "/layers/{layer_code}/import",
    response_model=ImportResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "additionalProperties": False,
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def import_layer(layer_code: str, request: Request, _principal: AdminUser) -> ImportResponse:
    # No automatic File/Form body dependency: AdminUser must run before parsing.
    if layer_code not in LAYERS:
        raise ApiError(404, "LAYER_NOT_FOUND", "Слой не найден.")
    settings = request.app.state.settings
    deadline = monotonic() + settings.import_timeout_seconds
    try:
        async with asyncio.timeout_at(deadline):
            content = await read_upload(request, settings)
            collection = await asyncio.to_thread(parse_layer, content, layer_code, settings)
            async with snapshot(request.app.state.sessions) as db:
                prepared = await prepare_layer(db, collection, layer_code)
        version = await replace_layer(request, prepared, deadline)
    except TimeoutError as error:
        raise ApiError(504, "REQUEST_TIMEOUT", "Время обработки файла истекло.") from error
    return ImportResponse(
        request_id=UUID(request.state.request_id),
        layer_code=layer_code,
        imported_count=len(prepared.rows),
        dataset_version=version,
        message="Слой загружен. Повторите поиск, чтобы увидеть обновлённые данные.",
    )


@router.post(
    "/demo/restore",
    response_model=RestoreResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": RestoreInput.model_json_schema()}},
        }
    },
)
async def restore_demo(request: Request, _principal: AdminUser) -> RestoreResponse:
    # Authorize before reading any body; no path/URL/package upload is accepted.
    settings = request.app.state.settings
    deadline = monotonic() + settings.import_timeout_seconds
    try:
        async with asyncio.timeout_at(deadline):
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip()
                != "application/json"
            ):
                raise ApiError(400, "MALFORMED_JSON", "Требуется JSON с confirm=true.")
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > settings.import_max_request_bytes:
                    raise ApiError(413, "PAYLOAD_TOO_LARGE", "Запрос превышает допустимый размер.")
            try:
                value = load_json(content.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError) as error:
                raise ApiError(400, "MALFORMED_JSON", "Некорректный JSON.") from error
            try:
                RestoreInput.model_validate(value)
            except ValidationError as error:
                raise ApiError(
                    422, "VALIDATION_ERROR", "Подтвердите восстановление: confirm=true."
                ) from error
        result = await install_demo(
            settings, request.app.state.sessions, request=request, deadline=deadline
        )
    except TimeoutError as error:
        raise ApiError(504, "REQUEST_TIMEOUT", "Время обработки демонабора истекло.") from error
    return RestoreResponse(request_id=UUID(request.state.request_id), **result.model_dump())
