import asyncio
import copy
import json
from time import monotonic
from uuid import UUID

from fastapi import Request
from pydantic import ValidationError

from app.auth.dependencies import authenticate
from app.catalog.capabilities import capabilities
from app.chat.store import ChatSession, ChatStore, Outcome
from app.contracts.chat import (
    Candidate,
    Clarification,
    ClarificationOutcome,
    ResultOutcome,
    UnsupportedOutcome,
    decision_adapter,
)
from app.contracts.filter import FilterV1
from app.core.errors import ApiError
from app.db.session import snapshot
from app.dialogue.protocol import Dialogue
from app.geo.search import result_message, search
from app.llm.client import LLMClient


def invalid_filter(error: ValidationError) -> ApiError:
    return ApiError(
        422,
        "FILTER_INVALID",
        "Условия поиска не соответствуют допустимому формату.",
        details=[
            {"path": ".".join(map(str, e["loc"])), "code": e["type"]}
            for e in error.errors(include_input=False, include_context=False)
        ],
    )


async def interpret_message(
    request: Request,
    entry: ChatSession,
    client_request_id: UUID,
    text: str,
    store: ChatStore,
    dialogue: Dialogue,
    llm_client: LLMClient,
    deadline: float,
) -> Outcome:
    settings = request.app.state.settings
    try:
        async with asyncio.timeout_at(deadline):
            raw = await dialogue.interpret(
                text=text,
                state_snapshot=copy.deepcopy(entry.state),
                capabilities=capabilities(settings),
                llm_client=LLMClient(settings, llm_client.http),
                deadline=deadline,
            )
            try:
                # Revalidate even model instances: a plugin may have used model_construct.
                decision = decision_adapter.validate_json(
                    json.dumps(raw.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)
                )
                state_json = json.dumps(
                    decision.proposed_state, ensure_ascii=False, allow_nan=False
                ).encode("utf-8")
                if len(state_json) > settings.chat_state_max_bytes:
                    raise ValueError("state limit")
            except (
                ValidationError,
                ValueError,
                TypeError,
                AttributeError,
                RecursionError,
            ) as error:
                raise ApiError(
                    502, "DIALOGUE_INVALID_RESPONSE", "Модуль диалога вернул некорректный ответ."
                ) from error
            outcome: Outcome
            if monotonic() >= deadline:
                raise ApiError(504, "REQUEST_TIMEOUT", "Время ожидания ответа истекло.", True)
            store.ensure_current(entry, client_request_id)
            if isinstance(decision, Candidate):
                try:
                    query = FilterV1.model_validate(decision.filter)
                except ValidationError as error:
                    raise invalid_filter(error) from error
                # No connection is held while the module/LLM is running.
                async with snapshot(request.app.state.sessions) as db:
                    result = await search(db, query)
                outcome = ResultOutcome(
                    message=result_message(result), validated_filter=query, result=result
                )
            elif isinstance(decision, Clarification):
                outcome = ClarificationOutcome(message=decision.message, options=decision.options)
            else:
                outcome = UnsupportedOutcome(message=decision.message)
            # Recheck authorization in a fresh short transaction, never in the
            # old data snapshot. Revoked/expired/disabled accounts cannot commit context.
            async with request.app.state.sessions() as db, db.begin():
                await authenticate(request, db)
            disconnected = await request.is_disconnected()
            if monotonic() >= deadline:
                raise ApiError(504, "REQUEST_TIMEOUT", "Время ожидания ответа истекло.", True)
            # No await between checking the generation and replacing its state.
            store.ensure_current(entry, client_request_id)
            if disconnected:
                raise ApiError(409, "CHAT_SESSION_CLOSED", "Запрос диалога отменён.")
            entry.state = copy.deepcopy(decision.proposed_state)
            return outcome
    except TimeoutError as error:
        raise ApiError(504, "REQUEST_TIMEOUT", "Время ожидания ответа истекло.", True) from error
