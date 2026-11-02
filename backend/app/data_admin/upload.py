from pathlib import PurePath

from fastapi import Request
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.types import Message

from app.core.config import Settings
from app.core.errors import ApiError


def too_large() -> ApiError:
    return ApiError(413, "PAYLOAD_TOO_LARGE", "Файл или запрос превышает допустимый размер.")


async def read_upload(request: Request, settings: Settings) -> bytes:
    """Called only after AdminUser/Origin/CSRF; count actual streamed bytes."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "multipart/form-data":
        raise ApiError(400, "MALFORMED_MULTIPART", "Требуется multipart-запрос с одним файлом.")
    if "content-length" in request.headers:
        try:
            size = int(request.headers["content-length"])
        except ValueError as error:
            raise ApiError(400, "MALFORMED_MULTIPART", "Некорректный размер запроса.") from error
        if size < 0:
            raise ApiError(400, "MALFORMED_MULTIPART", "Некорректный размер запроса.")
        if size > settings.import_max_request_bytes:
            raise too_large()
    received = 0

    async def bounded_receive() -> Message:
        nonlocal received
        message = await request.receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > settings.import_max_request_bytes:
                # Starlette closes partial spooled uploads on MultiPartException.
                raise MultiPartException("Multipart request exceeds the limit")
        return message

    bounded = Request(request.scope, receive=bounded_receive)
    try:
        async with bounded.form(max_files=1, max_fields=0) as form:
            entries = form.multi_items()
            if len(entries) != 1 or entries[0][0] != "file":
                raise ApiError(400, "MALFORMED_MULTIPART", "Ожидается единственное поле file.")
            upload = entries[0][1]
            if not isinstance(upload, UploadFile) or not upload.filename:
                raise ApiError(400, "MALFORMED_MULTIPART", "Ожидается файл в поле file.")
            # Filename is used only to check the suffix, never as a storage path.
            if PurePath(upload.filename).suffix.lower() not in {".json", ".geojson"}:
                raise ApiError(422, "IMPORT_INVALID", "Допустимы файлы .json и .geojson.")
            content = bytearray()
            while chunk := await upload.read(65536):
                content.extend(chunk)
                if len(content) > settings.import_max_file_bytes:
                    raise too_large()
            return bytes(content)
    except HTTPException as error:
        if received > settings.import_max_request_bytes:
            raise too_large() from error
        raise ApiError(400, "MALFORMED_MULTIPART", "Некорректный multipart-запрос.") from error
