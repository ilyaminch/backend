"""Общие данные и управляемый Dialogue для интеграционных API-тестов."""

import asyncio
import copy
import hashlib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog.capabilities import QueryCapabilities
from app.contracts.chat import Candidate, DialogueDecision, DialogueState
from app.db.models import AuthSession
from app.llm.client import LLMClient

ORIGIN = "http://localhost:8000"
PASSWORD = "test-only-password"
API = "/api/v1"


def search_filter(object_ids: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "operation": "search_objects",
        "category": "school",
        "conditions": [{"field": "id", "op": "in", "values": object_ids}],
    }


def candidate(object_ids: list[str], **state: Any) -> Candidate:
    return Candidate(kind="candidate", filter=search_filter(object_ids), proposed_state=state)


class ControlledDialogue:
    def __init__(self, decision: DialogueDecision) -> None:
        self.decision = decision
        self.calls: list[DialogueState] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.returned = asyncio.Event()
        self.hold = False
        self.finish_after_cancel = False

    async def interpret(
        self,
        *,
        text: str,
        state_snapshot: DialogueState,
        capabilities: QueryCapabilities,
        llm_client: LLMClient,
        deadline: float,
    ) -> DialogueDecision:
        decision = copy.deepcopy(self.decision)
        self.calls.append(copy.deepcopy(state_snapshot))
        self.started.set()
        if self.hold:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if not self.finish_after_cancel:
                    raise
                # Проверяем защиту от поздней записи даже у модуля,
                # который получил отмену, но продолжил обработку.
                await self.release.wait()
        self.returned.set()
        return decision


@dataclass(repr=False)
class ApiContext:
    app: FastAPI
    dialogue: ControlledDialogue
    stack: AsyncExitStack
    user_ids: dict[str, UUID]
    logins: dict[str, str]
    object_ids: list[str]
    region_id: str
    sessions: async_sessionmaker[AsyncSession] = field(repr=False)

    async def client(self) -> httpx.AsyncClient:
        return await self.stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=ORIGIN,
                headers={"Origin": ORIGIN},
            )
        )

    async def login(self, name: str = "user") -> httpx.AsyncClient:
        client = await self.client()
        response = await client.post(
            f"{API}/auth/login", json={"login": self.logins[name], "password": PASSWORD}
        )
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client

    async def session(self, client: httpx.AsyncClient) -> AuthSession:
        digest = hashlib.sha256(client.cookies["gis_session"].encode()).hexdigest()
        async with self.sessions() as db:
            session = await db.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
            assert session is not None
            return session

    async def create_dialogue(self, client: httpx.AsyncClient) -> UUID:
        response = await client.post(f"{API}/chat/sessions")
        assert response.status_code == 201
        dialogue_id = UUID(response.json()["dialogue_id"])
        assert_headers(response)
        return dialogue_id

    async def message(
        self, client: httpx.AsyncClient, dialogue_id: UUID, request_id: str | None = None
    ) -> httpx.Response:
        return await client.post(
            f"{API}/chat/sessions/{dialogue_id}/messages",
            json={"text": "Покажи школы", "client_request_id": request_id or str(uuid4())},
        )

    @asynccontextmanager
    async def pending_message(
        self, client: httpx.AsyncClient, dialogue_id: UUID, request_id: str
    ) -> AsyncIterator[asyncio.Task[httpx.Response]]:
        self.dialogue.hold = True
        task = asyncio.create_task(self.message(client, dialogue_id, request_id))
        try:
            await asyncio.wait_for(self.dialogue.started.wait(), timeout=5)
            yield task
        finally:
            self.dialogue.release.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=5)
            else:
                task.result()


def assert_headers(response: httpx.Response) -> None:
    assert response.headers["Cache-Control"] == "no-store"
    request_id = response.headers["X-Request-ID"]
    UUID(request_id)
    if response.status_code != 204:
        assert response.json()["request_id"] == request_id


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert_headers(response)
    body = response.json()
    assert body["error"]["code"] == code
    assert body["map_action"] == "preserve"
    assert "result" not in body
