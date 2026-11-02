"""Single-event-loop, single-worker transient dialogue storage."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.contracts.chat import (
    ClarificationOutcome,
    DialogueState,
    ResultOutcome,
    UnsupportedOutcome,
)
from app.core.config import Settings
from app.core.errors import ApiError

Outcome = ResultOutcome | ClarificationOutcome | UnsupportedOutcome


@dataclass
class ChatSession:
    id: UUID
    owner: str
    expires_at: datetime
    state: DialogueState = field(default_factory=dict)
    used_ids: set[UUID] = field(default_factory=set)
    current_id: UUID | None = None
    task: asyncio.Task[Outcome] | None = None
    closed: bool = False


class ChatStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions: dict[UUID, ChatSession] = {}
        self.tasks: set[asyncio.Task[Outcome]] = set()

    def track(self, task: asyncio.Task[Outcome]) -> None:
        self.tasks.add(task)
        task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task[Outcome]) -> None:
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Consume failures even after the HTTP client disconnects.

    def cleanup(self) -> None:
        now = datetime.now(UTC)
        for entry in list(self.sessions.values()):
            if entry.expires_at <= now:
                self.close(entry)

    def create(self, owner: str, expires_at: datetime) -> ChatSession:
        self.cleanup()
        if len(self.sessions) >= self.settings.chat_sessions_total:
            raise ApiError(503, "CHAT_CAPACITY_REACHED", "Чат временно перегружен.", True)
        if (
            sum(e.owner == owner for e in self.sessions.values())
            >= self.settings.chat_sessions_per_auth
        ):
            raise ApiError(
                429, "RATE_LIMITED", "Достигнут предел открытых диалогов. Закройте старые."
            )
        entry = ChatSession(uuid4(), owner, expires_at)
        self.sessions[entry.id] = entry
        return entry

    def get(self, dialogue_id: UUID, owner: str) -> ChatSession:
        self.cleanup()
        entry = self.sessions.get(dialogue_id)
        if entry is None or entry.owner != owner or entry.closed:
            raise ApiError(404, "CHAT_SESSION_NOT_FOUND", "Диалог не найден. Начните новый.")
        return entry

    def begin(self, entry: ChatSession, client_request_id: UUID) -> None:
        if client_request_id in entry.used_ids:
            raise ApiError(409, "REQUEST_ALREADY_USED", "Это сообщение уже было отправлено.")
        if entry.current_id is not None:
            raise ApiError(409, "CHAT_BUSY", "Дождитесь завершения предыдущего сообщения.")
        if len(entry.used_ids) >= self.settings.chat_messages_per_session:
            raise ApiError(429, "RATE_LIMITED", "Достигнут предел сообщений. Начните новый диалог.")
        entry.used_ids.add(client_request_id)
        entry.current_id = client_request_id

    def ensure_current(self, entry: ChatSession, request_id: UUID) -> None:
        # Expiry remains an authentication failure even if cleanup has already
        # removed the entry. DELETE/logout with a live authorization stay 409.
        if entry.expires_at <= datetime.now(UTC):
            raise ApiError(401, "SESSION_EXPIRED", "Срок входа истёк. Войдите снова.")
        if (
            entry.closed
            or self.sessions.get(entry.id) is not entry
            or entry.current_id != request_id
        ):
            raise ApiError(409, "CHAT_SESSION_CLOSED", "Запрос диалога отменён.")

    def close(self, entry: ChatSession) -> None:
        entry.closed = True
        entry.state.clear()
        entry.used_ids.clear()
        entry.current_id = None
        self.sessions.pop(entry.id, None)
        if entry.task:
            entry.task.cancel()

    def close_owner(self, owner: str) -> None:
        for entry in list(self.sessions.values()):
            if entry.owner == owner:
                self.close(entry)

    def shutdown(self) -> None:
        for entry in list(self.sessions.values()):
            self.close(entry)
        for task in self.tasks:
            task.cancel()

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(30)
            self.cleanup()
