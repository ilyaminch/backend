from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, StrictStr, TypeAdapter

from app.contracts.filter import FilterV1
from app.contracts.read_api import Contract, SearchResult

DialogueState = dict[str, JsonValue]


class Option(Contract):
    value: StrictStr
    label: StrictStr


class Candidate(Contract):
    kind: Literal["candidate"]
    # Independent validation takes place at the execution boundary.
    filter: dict[str, JsonValue]
    proposed_state: DialogueState


class Clarification(Contract):
    kind: Literal["clarification"]
    message: Annotated[StrictStr, Field(min_length=1)]
    options: list[Option]
    proposed_state: DialogueState


class Unsupported(Contract):
    kind: Literal["unsupported"]
    message: Annotated[StrictStr, Field(min_length=1)]
    proposed_state: DialogueState


DialogueDecision = Annotated[Candidate | Clarification | Unsupported, Field(discriminator="kind")]
decision_adapter: TypeAdapter[DialogueDecision] = TypeAdapter(DialogueDecision)


class MessageInput(Contract):
    client_request_id: UUID
    text: Annotated[StrictStr, Field(min_length=1)]


class SessionResponse(Contract):
    request_id: UUID
    dialogue_id: UUID


class ResultOutcome(Contract):
    outcome: Literal["result"] = "result"
    message: str
    validated_filter: FilterV1
    result: SearchResult


class ClarificationOutcome(Contract):
    outcome: Literal["clarification"] = "clarification"
    message: str
    options: list[Option]
    result: None = None
    map_action: Literal["preserve"] = "preserve"


class UnsupportedOutcome(Contract):
    outcome: Literal["unsupported"] = "unsupported"
    message: str
    result: None = None
    map_action: Literal["preserve"] = "preserve"


class ResultMessage(ResultOutcome):
    request_id: UUID
    dialogue_id: UUID
    client_request_id: UUID


class ClarificationMessage(ClarificationOutcome):
    request_id: UUID
    dialogue_id: UUID
    client_request_id: UUID


class UnsupportedMessage(UnsupportedOutcome):
    request_id: UUID
    dialogue_id: UUID
    client_request_id: UUID


MessageResponse = Annotated[
    ResultMessage | ClarificationMessage | UnsupportedMessage, Field(discriminator="outcome")
]
