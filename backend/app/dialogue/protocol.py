"""Integration boundary owned by backend; semantics implemented by MLChat."""

from importlib import import_module
from typing import Protocol, cast

from app.catalog.capabilities import QueryCapabilities
from app.contracts.chat import DialogueDecision, DialogueState
from app.core.errors import ApiError
from app.llm.client import LLMClient


class Dialogue(Protocol):
    async def interpret(
        self,
        *,
        text: str,
        state_snapshot: DialogueState,
        capabilities: QueryCapabilities,
        llm_client: LLMClient,
        deadline: float,
    ) -> DialogueDecision: ...


class UnavailableDialogue:
    async def interpret(
        self,
        *,
        text: str,
        state_snapshot: DialogueState,
        capabilities: QueryCapabilities,
        llm_client: LLMClient,
        deadline: float,
    ) -> DialogueDecision:
        raise ApiError(503, "DIALOGUE_UNAVAILABLE", "Модуль диалога ещё не подключён.", True)


def load_dialogue(factory_path: str | None) -> Dialogue:
    if not factory_path:
        return UnavailableDialogue()
    module_name, separator, factory_name = factory_path.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("DIALOGUE_FACTORY должен иметь формат package.module:factory")
    # Only trusted server configuration can select installed executable code.
    factory = getattr(import_module(module_name), factory_name)
    dialogue = factory()
    if not callable(getattr(dialogue, "interpret", None)):
        raise TypeError("Модуль диалога должен реализовать async interpret")
    return cast(Dialogue, dialogue)
