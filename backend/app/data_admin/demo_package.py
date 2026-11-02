"""Read a complete, self-contained trusted package before opening a write transaction."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from pydantic import ValidationError

from app.catalog.definitions import LAYERS
from app.contracts.demo import DemoManifest
from app.contracts.import_api import ImportSource
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.json import load_json
from app.data_admin.validation import ImportCollection, parse_layer


def package_invalid(code: str) -> ApiError:
    return ApiError(
        503,
        "DEMO_PACKAGE_INVALID",
        "Установленный демонабор повреждён или не соответствует контракту.",
        details=[{"path": "package", "code": code}],
    )


def check_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise ApiError(504, "REQUEST_TIMEOUT", "Время обработки демонабора истекло.")


def read_file(root: Path, filename: str, max_bytes: int) -> bytes:
    path = root / filename
    # Fixed filenames plus containment checks: no links to external files.
    if path.is_symlink() or path.resolve(strict=True).parent != root:
        raise package_invalid("FILE_PATH")
    if not path.is_file():
        raise package_invalid("FILE_TYPE")
    with path.open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise package_invalid("FILE_TOO_LARGE")
    return content


@dataclass(frozen=True)
class DemoPackage:
    manifest: DemoManifest
    layers: dict[str, ImportCollection]
    sources: list[ImportSource]


def load_package(settings: Settings, deadline: float) -> DemoPackage:
    if settings.demo_package_dir is None:
        raise ApiError(503, "DEMO_PACKAGE_UNAVAILABLE", "Демонстрационный набор не установлен.")
    try:
        root = settings.demo_package_dir.resolve(strict=True)
        check_deadline(deadline)
        raw = read_file(root, "manifest.json", settings.import_max_file_bytes)
        manifest = DemoManifest.model_validate(load_json(raw.decode("utf-8-sig")))
        # Read and hash all six byte sequences before parsing any of them.
        contents: dict[str, bytes] = {}
        for entry in manifest.files:
            check_deadline(deadline)
            content = read_file(root, entry.file, settings.import_max_file_bytes)
            if hashlib.sha256(content).hexdigest() != entry.sha256:
                raise package_invalid("SHA256_MISMATCH")
            contents[entry.layer_code] = content
        layers = {}
        sources: dict[str, ImportSource] = {}
        ids: set[str] = set()
        counts: dict[str, int] = {}
        for code in LAYERS:
            check_deadline(deadline)
            collection = parse_layer(contents[code], code, settings)
            layers[code] = collection
            counts[code] = len(collection.features)
            for source in collection.data_sources:
                if source.code in sources and sources[source.code] != source:
                    raise package_invalid("SOURCE_CONFLICT")
                sources[source.code] = source
            if code != "regions":
                for feature in collection.features:
                    if feature.id in ids:
                        raise package_invalid("DUPLICATE_OBJECT_ID")
                    ids.add(feature.id)
        if (
            sum(counts[code] for code in ("school", "hospital", "pharmacy")) != 350
            or counts["park"] != 100
            or counts["improvement_zone"] != 50
            or counts["regions"] != 4
        ):
            raise package_invalid("DEMO_COUNTS")
        for collection in layers.values():
            for feature in collection.features:
                if feature.properties.source not in sources:
                    raise package_invalid("SOURCE_UNKNOWN")
        check_deadline(deadline)
        return DemoPackage(manifest, layers, list(sources.values()))
    except OSError as error:
        raise ApiError(
            503, "DEMO_PACKAGE_UNAVAILABLE", "Файлы демонстрационного набора недоступны."
        ) from error
    except (ValueError, UnicodeError, RecursionError, ValidationError) as error:
        raise package_invalid("INVALID_MANIFEST") from error
    except ApiError as error:
        if error.code in {"IMPORT_INVALID", "IMPORT_CONFLICT"}:
            raise package_invalid("INVALID_LAYER") from error
        raise
