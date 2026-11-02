import asyncio
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.auth.dependencies import CurrentUser
from app.chat.service import interpret_message
from app.chat.store import ChatStore
from app.contracts.chat import (
    ClarificationMessage,
    ClarificationOutcome,
    MessageInput,
    MessageResponse,
    ResultMessage,
    ResultOutcome,
    SessionResponse,
    UnsupportedMessage,
)
from app.contracts.errors import ErrorResponse
from app.core.errors import ApiError

router = APIRouter(
    prefix="/chat",
    tags=["Диалог"],
    responses={
        s: {"model": ErrorResponse} for s in (401, 403, 404, 409, 413, 422, 429, 502, 503, 504)
    },
)


@router.post("/sessions", status_code=201, response_model=SessionResponse)
async def create_session(request: Request, principal: CurrentUser) -> SessionResponse:
    store: ChatStore = request.app.state.chat_store
    entry = store.create(principal.session.token_hash, principal.session.expires_at)
    return SessionResponse(request_id=UUID(request.state.request_id), dialogue_id=entry.id)


@router.delete("/sessions/{dialogue_id}", status_code=204)
async def delete_session(dialogue_id: UUID, request: Request, principal: CurrentUser) -> Response:
    store: ChatStore = request.app.state.chat_store
    store.close(store.get(dialogue_id, principal.session.token_hash))
    return Response(status_code=204)


@router.post("/sessions/{dialogue_id}/messages", response_model=MessageResponse)
async def send_message(
    dialogue_id: UUID,
    body: MessageInput,
    request: Request,
    principal: CurrentUser,
) -> MessageResponse:
    settings = request.app.state.settings
    if not body.text.strip():
        raise ApiError(422, "VALIDATION_ERROR", "Введите текст сообщения.")
    if len(body.text) > settings.chat_message_max_characters:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Сообщение превышает допустимый размер.")
    store: ChatStore = request.app.state.chat_store
    entry = store.get(dialogue_id, principal.session.token_hash)
    store.begin(entry, body.client_request_id)
    deadline = monotonic() + settings.chat_request_timeout_seconds
    task = asyncio.create_task(
        interpret_message(
            request,
            entry,
            body.client_request_id,
            body.text,
            store,
            request.app.state.dialogue,
            request.app.state.llm_client,
            deadline,
        )
    )
    entry.task = task
    store.track(task)
    try:
        # A browser abort must cancel the work even when the ASGI server keeps
        # the response handler alive. DELETE/logout never wait for this task.
        while not task.done():
            await asyncio.wait({task}, timeout=min(0.25, max(0, deadline - monotonic())))
            store.ensure_current(entry, body.client_request_id)
            if not task.done() and monotonic() >= deadline:
                task.cancel()
                raise ApiError(504, "REQUEST_TIMEOUT", "Время ожидания ответа истекло.", True)
            if not task.done() and await request.is_disconnected():
                task.cancel()
                raise ApiError(409, "CHAT_REQUEST_CANCELLED", "Запрос диалога отменён.")
        store.ensure_current(entry, body.client_request_id)
        try:
            outcome = task.result()
        except asyncio.CancelledError as error:
            raise ApiError(409, "CHAT_SESSION_CLOSED", "Запрос диалога отменён.") from error
        common = {
            "request_id": UUID(request.state.request_id),
            "dialogue_id": dialogue_id,
            "client_request_id": body.client_request_id,
        }
        if isinstance(outcome, ResultOutcome):
            return ResultMessage(**common, **outcome.model_dump())
        if isinstance(outcome, ClarificationOutcome):
            return ClarificationMessage(**common, **outcome.model_dump())
        return UnsupportedMessage(**common, **outcome.model_dump())
    finally:
        if not task.done():
            task.cancel()
        if entry.task is task:
            entry.task = None
            entry.current_id = None
