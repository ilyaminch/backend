from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from app.catalog.definitions import LAYERS
from app.contracts.import_api import Text, Timestamp
from app.contracts.read_api import Bigint, Contract

LayerCode = Literal["school", "hospital", "pharmacy", "park", "improvement_zone", "regions"]


class DemoFile(Contract):
    layer_code: LayerCode
    file: Text
    sha256: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def fixed_filename(self) -> Self:
        if self.file != f"{self.layer_code}.geojson":
            raise ValueError("Имя файла должно совпадать с кодом слоя")
        return self


class DemoManifest(Contract):
    schema_version: Literal["1"]
    package_version: Text
    prepared_at: Timestamp
    provenance: Text
    files: Annotated[list[DemoFile], Field(min_length=6, max_length=6)]

    @model_validator(mode="after")
    def all_layers(self) -> Self:
        if {entry.layer_code for entry in self.files} != set(LAYERS):
            raise ValueError("Нужен ровно один файл каждого из шести слоёв")
        return self


class RestoreInput(Contract):
    confirm: StrictBool = Field(json_schema_extra={"const": True})

    @model_validator(mode="after")
    def confirmed(self) -> Self:
        if not self.confirm:
            raise ValueError("Восстановление требует confirm=true")
        return self


class DemoResult(Contract):
    dataset_version: Bigint
    object_count: Literal[500] = 500
    region_count: Literal[4] = 4
    layer_counts: dict[LayerCode, int]
    message: str = "Демонабор загружен. Повторите поиск, чтобы увидеть обновлённые данные."
    suggest_repeat_search: Literal[True] = True


class RestoreResponse(DemoResult):
    request_id: UUID
