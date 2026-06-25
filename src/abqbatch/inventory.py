"""Inventory raw Abaqus input files into cases.csv and SQLite."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from abqbatch import db
from abqbatch.config import project_relative
from abqbatch.exceptions import Status
from abqbatch.hashes import sha256_file

CASE_COLUMNS = [
    "case_id",
    "source_inp",
    "material_profile",
    "load_profile",
    "post_profile",
    "cpus",
    "memory",
    "priority",
]


def read_cases_csv(path: Path) -> list[dict[str, str]]:
    """Read cases.csv if it exists."""

    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def write_cases_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write cases.csv using the public case columns."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CASE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in CASE_COLUMNS})


def _next_case_id(index: int) -> str:
    return f"C{index:06d}"


def scan_inp_files(project_root: Path, input_dir: Path) -> list[Path]:
    """Return raw .inp files sorted by relative path."""

    root = project_root.resolve()
    search_dir = input_dir if input_dir.is_absolute() else root / input_dir
    return sorted(path for path in search_dir.rglob("*.inp") if path.is_file())


def inventory_project(
    project_root: Path,
    input_dir: Path,
    output_csv: Path,
    db_path: Path,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Scan input files, write cases.csv, and upsert discovered cases."""

    root = project_root.resolve()
    output = output_csv if output_csv.is_absolute() else root / output_csv
    existing_rows = [] if refresh else read_cases_csv(output)
    by_source = {row["source_inp"]: row for row in existing_rows if row.get("source_inp")}
    next_index = len(existing_rows) + 1

    rows: list[dict[str, Any]] = []
    for inp_path in scan_inp_files(root, input_dir):
        source = project_relative(inp_path, root)
        previous = by_source.get(source)
        if previous:
            row: dict[str, Any] = dict(previous)
        else:
            row = {
                "case_id": _next_case_id(next_index),
                "source_inp": source,
                "material_profile": "steel_E210GPa",
                "load_profile": "disp_ramp_y",
                "post_profile": "default",
                "cpus": "8",
                "memory": "90%",
                "priority": str(next_index * 10),
            }
            next_index += 1
        row["source_sha256"] = sha256_file(inp_path)
        rows.append(row)

    write_cases_csv(output, rows)
    db.init_db(db_path)
    for row in rows:
        db.upsert_case(
            db_path,
            {
                "case_id": row["case_id"],
                "source_inp": row["source_inp"],
                "source_sha256": row["source_sha256"],
                "material_profile": row.get("material_profile"),
                "load_profile": row.get("load_profile"),
                "post_profile": row.get("post_profile", "default"),
                "job_name": row["case_id"],
                "status": Status.DISCOVERED.value,
                "priority": int(row.get("priority") or 100),
            },
        )
    return rows
