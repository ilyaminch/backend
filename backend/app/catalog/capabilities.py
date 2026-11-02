from typing import Literal
from uuid import UUID

from app.catalog.definitions import CATEGORY_NAMES
from app.contracts.filter import FIELD_RULES, MAX_FILTER_PARAMETERS
from app.contracts.read_api import Contract
from app.core.config import Settings


class FieldCapability(Contract):
    field: str
    type: str
    operations: list[str]
    categories: list[str]
    minimum: int | None = None
    maximum: int | None = None
    values: list[str] | None = None


class CategoryCapability(Contract):
    code: str
    name: str


class QueryCapabilities(Contract):
    schema_version: Literal["1"] = "1"
    operations: list[str]
    categories: list[CategoryCapability]
    fields: list[FieldCapability]
    combination: Literal["AND"] = "AND"
    limits: dict[str, int | float]


class CapabilitiesResponse(QueryCapabilities):
    request_id: UUID


def capabilities(settings: Settings) -> QueryCapabilities:
    return QueryCapabilities(
        operations=["search_objects", "count_by_region"],
        categories=[CategoryCapability(code=c, name=n) for c, n in CATEGORY_NAMES.items()],
        fields=[
            FieldCapability(
                field=field,
                type=kind,
                operations=list(operations),
                categories=[category] if category else list(CATEGORY_NAMES),
                minimum=1
                if field == "rating"
                else (0 if field in {"capacity", "area_m2", "length_m"} else None),
                maximum=5 if field == "rating" else None,
                values=["active", "closed", "unknown"] if field == "status" else None,
            )
            for field, (kind, operations, category) in FIELD_RULES.items()
        ],
        limits={
            "filter_parameters": MAX_FILTER_PARAMETERS,
            "import_file_bytes": settings.import_max_file_bytes,
            "import_request_bytes": settings.import_max_request_bytes,
            "import_features": settings.import_max_features,
            "import_positions": settings.import_max_positions,
            "import_positions_per_geometry": settings.import_max_positions_per_geometry,
            "import_timeout_seconds": settings.import_timeout_seconds,
            "import_lock_timeout_seconds": settings.import_lock_timeout_seconds,
            "message_characters": settings.chat_message_max_characters,
            "state_bytes": settings.chat_state_max_bytes,
            "dialogues_per_auth_session": settings.chat_sessions_per_auth,
            "dialogues_per_process": settings.chat_sessions_total,
            "messages_per_dialogue": settings.chat_messages_per_session,
            "llm_response_bytes": settings.llm_response_max_bytes,
            "request_timeout_seconds": settings.chat_request_timeout_seconds,
        },
    )
