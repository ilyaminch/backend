"""Adapt the delivered 28+4 verification set to the strict import format; no DB writes."""

import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/verification"


def main() -> None:
    reference = json.loads((SOURCE / "source-reference.json").read_text(encoding="utf-8"))
    for name in ("objects.geojson", "regions.geojson", "sources.json"):
        digest = hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
        if reference["files"].get(f"data/verification/{name}") != digest:
            raise ValueError(f"Verification source changed: {name}; review delivery first")
    objects = json.loads((SOURCE / "objects.geojson").read_text(encoding="utf-8-sig"))
    regions = json.loads((SOURCE / "regions.geojson").read_text(encoding="utf-8-sig"))
    sources = json.loads((SOURCE / "sources.json").read_text(encoding="utf-8-sig"))
    expected = {
        "school": 6,
        "hospital": 5,
        "pharmacy": 8,
        "park": 6,
        "improvement_zone": 3,
    }
    counts = Counter(f["properties"]["category_code"] for f in objects["features"])
    if counts != expected or len(regions["features"]) != 4:
        raise ValueError("Unexpected verification dataset composition")
    if len({f["properties"]["id"] for f in objects["features"]}) != 28:
        raise ValueError("Duplicate object IDs")
    output = SOURCE / "imports"
    output.mkdir(exist_ok=True)
    for code in (*expected, "regions"):
        selected = (
            regions["features"]
            if code == "regions"
            else [f for f in objects["features"] if f["properties"]["category_code"] == code]
        )
        features = []
        for feature in selected:
            properties = dict(feature["properties"])
            identifier = properties.pop("id")
            if code == "regions":
                # Backend uses short fixed names; retain original names/codes in source files.
                properties["name"] = properties["name"].removesuffix(" район")
                properties.pop("region_code")
            features.append(
                {
                    "type": "Feature",
                    "id": identifier,
                    "geometry": feature["geometry"],
                    "properties": properties,
                }
            )
        used = {f["properties"]["source"] for f in features}
        collection = {
            "type": "FeatureCollection",
            "schema_version": "1",
            "data_sources": [s for s in sources if s["code"] in used],
            "features": features,
        }
        (output / f"{code}.geojson").write_text(
            json.dumps(collection, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"{code}: {len(features)} features")


if __name__ == "__main__":
    main()
