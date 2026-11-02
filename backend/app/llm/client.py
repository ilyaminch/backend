"""Transport only. Prompts and interpretation belong to MLChat."""

import asyncio
from time import monotonic
from typing import Literal

import httpx
from pydantic import JsonValue

from app.contracts.read_api import Contract
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.json import load_json


class LLMMessage(Contract):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self.used = False

    async def complete(
        self,
        *,
        messages: list[LLMMessage],
        deadline: float,
        response_schema: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        settings = self.settings
        if not settings.llm_base_url or not settings.llm_model:
            raise ApiError(503, "LLM_UNAVAILABLE", "Локальная модель ещё не настроена.", True)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ApiError(504, "REQUEST_TIMEOUT", "Время ожидания ответа истекло.", True)
        if self.used:
            raise ApiError(502, "LLM_PROTOCOL_ERROR", "Повторная генерация на этом шаге запрещена.")
        self.used = True
        payload: dict[str, JsonValue] = {
            "model": settings.llm_model,
            "messages": [message.model_dump(mode="json") for message in messages],
            "stream": False,
        }
        if settings.llm_temperature is not None:
            payload["temperature"] = settings.llm_temperature
        if settings.llm_response_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif settings.llm_response_mode == "json_schema":
            if response_schema is None:
                raise ApiError(502, "LLM_PROTOCOL_ERROR", "Не задана схема ответа модели.")
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "dialogue_decision",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        headers = {"Accept": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = "Bearer " + settings.llm_api_key.get_secret_value()
        try:
            async with asyncio.timeout_at(deadline):
                async with self.http.stream(
                    "POST",
                    settings.llm_base_url + "/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=httpx.Timeout(remaining),
                ) as response:
                    if response.status_code in {408, 429} or response.status_code >= 500:
                        raise ApiError(503, "LLM_UNAVAILABLE", "Модель временно недоступна.", True)
                    if response.status_code != 200:
                        raise ApiError(502, "LLM_PROTOCOL_ERROR", "Сервис модели отклонил запрос.")
                    media_type = response.headers.get("content-type", "").split(";")[0]
                    if media_type.strip().lower() != "application/json":
                        raise invalid_response()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > settings.llm_response_max_bytes:
                            raise invalid_response()
                    return parse_response(bytes(content))
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ApiError(
                504, "REQUEST_TIMEOUT", "Время ожидания ответа истекло.", True
            ) from error
        except httpx.RequestError as error:
            raise ApiError(503, "LLM_UNAVAILABLE", "Модель временно недоступна.", True) from error


def invalid_response() -> ApiError:
    return ApiError(502, "LLM_INVALID_RESPONSE", "Модель вернула некорректный ответ.")


def parse_response(content: bytes) -> dict[str, JsonValue]:
    try:
        envelope = load_json(content)
        if not isinstance(envelope, dict):
            raise ValueError
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
            raise ValueError
        message = choice.get("message")
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("refusal")
            or message.get("tool_calls")
            or message.get("function_call")
        ):
            raise ValueError
        value = message.get("content")
        if not isinstance(value, str) or not value.strip():
            raise ValueError
        result = load_json(value)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, RecursionError) as error:
        raise invalid_response() from error
