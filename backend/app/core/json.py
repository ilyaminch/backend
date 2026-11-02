import json
import math
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        # Escaped lone surrogates must not reach error paths or JSON responses.
        key.encode("utf-8")
        if key in result:
            raise ValueError("Повторяющийся ключ JSON")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Недопустимая числовая константа JSON")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Число JSON выходит за конечный диапазон")
    return number


def load_json(content: str | bytes) -> Any:
    return json.loads(
        content,
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
    )
