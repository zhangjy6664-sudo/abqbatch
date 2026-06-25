from __future__ import annotations

from pathlib import Path

from abqbatch import db
from abqbatch.inventory import inventory_project


def test_inventory_scans_two_inputs_and_is_repeatable(basic_project: Path) -> None:
    db_path = basic_project / "db" / "state.sqlite"
    rows = inventory_project(
        basic_project,
        Path("raw_inp"),
        Path("configs/cases.csv"),
        db_path,
    )
    rows_again = inventory_project(
        basic_project,
        Path("raw_inp"),
        Path("configs/cases.csv"),
        db_path,
    )

    assert [row["case_id"] for row in rows] == ["C000001", "C000002"]
    assert [row["case_id"] for row in rows_again] == ["C000001", "C000002"]
    assert len(db.list_cases(db_path)) == 2
    assert all(row["source_sha256"] for row in rows)
