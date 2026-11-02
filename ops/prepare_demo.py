"""Prepare the trusted 500+4 import package from a separate geodata delivery; no DB writes."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ("school", "hospital", "pharmacy", "park", "improvement_zone")


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def prepare(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    manifest_bytes = (source / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    for group in ("sha256", "input_sha256"):
        for name, expected in manifest[group].items():
            path = (source / name).resolve(strict=True)
            if (
                not path.is_relative_to(source)
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise ValueError(f"Source checksum/path mismatch: {name}")
    objects = json.loads((source / "objects.geojson").read_bytes())["features"]
    by_id = {f["properties"]["id"]: f for f in objects}
    counts = Counter(f["properties"]["category_code"] for f in objects)
    if (
        len(objects) != 500
        or len(by_id) != 500
        or set(counts) != set(CATEGORIES)
        or sum(counts[code] for code in CATEGORIES[:3]) != 350
        or counts["park"] != 100
        or counts["improvement_zone"] != 50
        or dict(counts) != manifest["statistics"]["category_counts"]
    ):
        raise ValueError("Expected 350 points, 100 parks, 50 lines and unique IDs")
    small = json.loads((source / "inputs/verification/objects.geojson").read_bytes())["features"]
    if len(small) != 28 or any(by_id.get(f["properties"]["id"]) != f for f in small):
        raise ValueError("Original verification objects changed")
    if (source / "regions.geojson").read_bytes() != (
        source / "inputs/verification/regions.geojson"
    ).read_bytes():
        raise ValueError("Original regions changed")
    sources = json.loads((source / "sources.json").read_bytes())
    files = []
    contents = {}
    for code in (*CATEGORIES, "regions"):
        filename = "regions.geojson" if code == "regions" else f"layers/{code}.geojson"
        selected = json.loads((source / filename).read_bytes())["features"]
        if code != "regions" and {f["properties"]["id"]: f for f in selected} != {
            key: f for key, f in by_id.items() if f["properties"]["category_code"] == code
        }:
            raise ValueError(f"Layer differs from combined objects: {code}")
        features = []
        for item in selected:
            properties = dict(item["properties"])
            identifier = properties.pop("id")
            if code == "regions":
                properties["name"] = properties["name"].removesuffix(" район")
                properties.pop("region_code")
            features.append(
                dict(
                    type="Feature", id=identifier, geometry=item["geometry"], properties=properties
                )
            )
        used = {f["properties"]["source"] for f in features}
        content = encode(
            dict(
                type="FeatureCollection",
                schema_version="1",
                data_sources=[s for s in sources if s["code"] in used],
                features=features,
            )
        )
        contents[f"{code}.geojson"] = content
        files.append(
            dict(
                layer_code=code, file=f"{code}.geojson", sha256=hashlib.sha256(content).hexdigest()
            )
        )
    provenance = (
        f"Supplied {manifest['version']}; source manifest SHA256 "
        f"{hashlib.sha256(manifest_bytes).hexdigest()}. "
        "OpenStreetMap contributors, ODbL 1.0, https://www.openstreetmap.org/copyright. "
        "Synthetic fields retained; ID placement and region names adapted for backend import."
    )
    prepared_at = datetime.now(UTC).isoformat()
    existing_path = destination / "manifest.json"
    if existing_path.exists():
        existing = json.loads(existing_path.read_bytes())
        if existing["package_version"] == manifest["version"]:
            if existing["files"] != files or existing["provenance"] != provenance:
                raise ValueError(
                    "Same package version has different content; review/version delivery first"
                )
            prepared_at = existing["prepared_at"]
    contents["manifest.json"] = encode(
        dict(
            schema_version="1",
            package_version=manifest["version"],
            prepared_at=prepared_at,
            provenance=provenance,
            files=files,
        )
    )
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        (destination / name).write_bytes(content)
    print(
        f"Prepared {manifest['version']}: {dict(counts)}, four regions; sources remain outside Git"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT.parent / "datasets/tomsk-demo-1")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/demo/package")
    args = parser.parse_args()
    prepare(args.source_dir, args.output_dir)


if __name__ == "__main__":
    main()
