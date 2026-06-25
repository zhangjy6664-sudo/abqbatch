"""ODB postprocessing script intended for Abaqus Python.

This script intentionally uses only the Python standard library plus Abaqus'
odbAccess module. It also exits cleanly in normal Python with a clear log.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import mean


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--odb", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--out", required=True)
    return parser.parse_args(argv)


def write_missing_odbaccess(out_dir: Path, exc: Exception) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "post.log").write_text(
        f"odbAccess is not available. Run this script with Abaqus Python.\nOriginal error: {exc}\n",
        encoding="utf-8",
    )
    return 2


def reduce_values(values: list[float], reduction: str) -> float:
    if reduction == "max":
        return max(values)
    if reduction == "min":
        return min(values)
    if reduction == "mean":
        return mean(values)
    raise ValueError(f"Unsupported reduction: {reduction}")


def value_with_invariant(value: object, invariant: str | None) -> float:
    if invariant is None:
        return float(getattr(value, "data", value))
    if hasattr(value, "mises") and invariant == "Mises":
        return float(value.mises)
    attr = {
        "MaxPrincipal": "maxPrincipal",
        "MinPrincipal": "minPrincipal",
        "Magnitude": "magnitude",
    }.get(invariant)
    if attr and hasattr(value, attr):
        return float(getattr(value, attr))
    raise ValueError(f"Invariant is not available: {invariant}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    odb_path = Path(args.odb)
    spec_path = Path(args.spec)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        from odbAccess import openOdb  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - exercised in normal Python
        return write_missing_odbaccess(out_dir, exc)

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    case_id = spec.get("case_id", "")
    profile = spec.get("profile") or {}
    metrics: dict[str, dict[str, object]] = {}
    history_rows: list[dict[str, object]] = []
    field_rows: list[dict[str, object]] = []
    errors: list[str] = []

    odb = openOdb(str(odb_path), readOnly=True)
    try:
        for item in profile.get("history", []):
            step = odb.steps[item["step"]]
            region = step.historyRegions[item["region"]]
            for variable in (item.get("x"), item.get("y")):
                if variable not in region.historyOutputs:
                    errors.append(f"Missing history output: {variable}")
                    continue
                for time, value in region.historyOutputs[variable].data:
                    history_rows.append(
                        {
                            "case_id": case_id,
                            "step": item["step"],
                            "region": item["region"],
                            "variable": variable,
                            "time": time,
                            "value": value,
                        }
                    )

        for item in profile.get("field", []):
            step = odb.steps[item["step"]]
            frame = (
                step.frames[-1] if item.get("frame") == "last" else step.frames[int(item["frame"])]
            )
            output = frame.fieldOutputs[item["variable"]]
            instance = odb.rootAssembly.instances.get(item.get("instance"))
            if instance is not None and item.get("element_set"):
                output = output.getSubset(region=instance.elementSets[item["element_set"]])
            values = [value_with_invariant(value, item.get("invariant")) for value in output.values]
            reduced = reduce_values(values, item.get("reduction", "max"))
            source = f"field:{item['variable']}:{item.get('invariant') or ''}".rstrip(":")
            metrics[item["name"]] = {"value": reduced, "unit": None, "source": source}
            field_rows.append(
                {
                    "case_id": case_id,
                    "name": item["name"],
                    "value": reduced,
                    "unit": "",
                    "source": source,
                }
            )
    finally:
        odb.close()

    with (out_dir / "metrics.json").open("w", encoding="utf-8") as stream:
        json.dump({"case_id": case_id, "metrics": metrics, "errors": errors}, stream, indent=2)

    with (out_dir / "history_curves.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["case_id", "step", "region", "variable", "time", "value"],
        )
        writer.writeheader()
        writer.writerows(history_rows)

    with (out_dir / "field_summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["case_id", "name", "value", "unit", "source"])
        writer.writeheader()
        writer.writerows(field_rows)

    (out_dir / "post.log").write_text("\n".join(errors) + "\n", encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
