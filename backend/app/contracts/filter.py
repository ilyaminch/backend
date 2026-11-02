"""The only accepted filter language, shared by direct search and dialogue."""

import math
import re
from datetime import date
from typing import Annotated, Literal, Self, cast

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from app.contracts.read_api import Contract, validate_bigint

CategoryCode = Literal["school", "hospital", "pharmacy", "park", "improvement_zone"]
FieldCode = Literal[
    "id",
    "name",
    "status",
    "rating",
    "capacity",
    "area_m2",
    "length_m",
    "opening_date",
    "is_accessible",
    "source",
    "source_id",
]
Value = StrictStr | StrictInt | StrictFloat | StrictBool

# Approved 21.10.2026: asyncpg allows 32767 bind parameters; reserve two
# for category and GeoJSON precision. Each typed IN array consumes one slot.
MAX_FILTER_PARAMETERS = 32765

# This registry drives validation and the public capabilities catalogue together.
FIELD_RULES: dict[str, tuple[str, tuple[str, ...], str | None]] = {
    "id": ("bigint_string", ("eq", "in"), None),
    "name": ("string", ("eq", "in", "contains"), None),
    "status": ("status", ("eq", "in"), None),
    "rating": ("number", ("eq", "gt", "gte", "lt", "lte", "between", "in"), None),
    "capacity": ("integer", ("eq", "gt", "gte", "lt", "lte", "between", "in"), None),
    "area_m2": ("number", ("eq", "gt", "gte", "lt", "lte", "between", "in"), "park"),
    "length_m": ("number", ("eq", "gt", "gte", "lt", "lte", "between", "in"), "improvement_zone"),
    "opening_date": ("date", ("eq", "between", "in", "year"), None),
    "is_accessible": ("boolean", ("eq", "in"), None),
    "source": ("string", ("eq", "in", "contains"), None),
    "source_id": ("string", ("eq", "in", "contains"), None),
}


class ScalarCondition(Contract):
    field: FieldCode
    op: Literal["eq", "gt", "gte", "lt", "lte", "contains"]
    value: Value


class BetweenCondition(Contract):
    field: FieldCode
    op: Literal["between"]
    lower: Value
    upper: Value


class InCondition(Contract):
    field: FieldCode
    op: Literal["in"]
    values: Annotated[list[Value], Field(min_length=1)]


class YearCondition(Contract):
    field: Literal["opening_date"]
    op: Literal["year"]
    value: Annotated[StrictInt, Field(ge=1, le=9999)]


Condition = Annotated[
    ScalarCondition | BetweenCondition | InCondition | YearCondition,
    Field(discriminator="op"),
]


def check_value(field: str, value: Value) -> None:
    kind = FIELD_RULES[field][0]
    valid = True
    if kind == "bigint_string":
        validate_bigint(value)
    elif kind in {"string", "status", "date"}:
        valid = type(value) is str
        if valid and kind == "status":
            valid = value in {"active", "closed", "unknown"}
        if valid and kind == "date":
            valid = bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(value)))
            if valid:
                date.fromisoformat(str(value))
    elif kind == "boolean":
        valid = type(value) is bool
    elif kind == "integer":
        valid = type(value) is int and value >= 0
    else:
        valid = type(value) in {int, float}
        if valid:
            numeric = cast(int | float, value)
            valid = numeric >= 0 and (isinstance(numeric, int) or math.isfinite(numeric))
        if valid and field == "rating":
            valid = 1 <= cast(int | float, value) <= 5
    if not valid:
        raise ValueError(f"Недопустимое значение поля {field}")


class FilterV1(Contract):
    schema_version: Literal["1"]
    operation: Literal["search_objects", "count_by_region"]
    category: CategoryCode
    conditions: Annotated[
        list[Condition],
        Field(
            max_length=MAX_FILTER_PARAMETERS,
            description=(
                "Бюджет условий: 32765 параметров; between/year стоят 2, "
                "остальные, включая весь список in, стоят 1."
            ),
        ),
    ]

    @model_validator(mode="after")
    def check_conditions(self) -> Self:
        parameter_count = sum(
            2 if isinstance(condition, (BetweenCondition, YearCondition)) else 1
            for condition in self.conditions
        )
        if parameter_count > MAX_FILTER_PARAMETERS:
            raise ValueError("Превышен общий бюджет параметров условий фильтра")
        for condition in self.conditions:
            _, operations, category = FIELD_RULES[condition.field]
            if condition.op not in operations or (category and category != self.category):
                raise ValueError("Поле или операция неприменимы к выбранной категории")
            if isinstance(condition, YearCondition):
                continue
            if isinstance(condition, BetweenCondition):
                values = [condition.lower, condition.upper]
            elif isinstance(condition, InCondition):
                values = condition.values
            else:
                values = [condition.value]
            for value in values:
                check_value(condition.field, value)
            if isinstance(condition, BetweenCondition):
                if isinstance(condition.lower, str) and isinstance(condition.upper, str):
                    reversed_range = condition.lower > condition.upper
                else:
                    reversed_range = cast(int | float, condition.lower) > cast(
                        int | float, condition.upper
                    )
                if reversed_range:
                    raise ValueError("Нижняя граница превышает верхнюю")
        return self
