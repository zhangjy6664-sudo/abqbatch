from __future__ import annotations

from pathlib import Path

from abqbatch import db
from abqbatch.exceptions import Status


def test_db_case_attempt_artifact_and_result(workspace_tmp: Path) -> None:
    db_path = workspace_tmp / "state.sqlite"
    db.init_db(db_path)
    db.upsert_case(
        db_path,
        {
            "case_id": "C000001",
            "source_inp": "raw_inp/a.inp",
            "source_sha256": "abc",
            "status": Status.DISCOVERED.value,
        },
    )
    db.update_status(db_path, "C000001", Status.GENERATED.value)
    attempt_id = db.create_attempt(db_path, "C000001", "SOLVE", "abaqus job=C000001", workspace_tmp)
    db.finish_attempt(db_path, attempt_id, 0, Status.SOLVED.value)
    artifact = workspace_tmp / "a.txt"
    artifact.write_text("x", encoding="utf-8")
    db.add_artifact(db_path, "C000001", attempt_id, "text", artifact, "hash")
    db.add_result(db_path, "C000001", "max_mises", 1.0)

    assert db.get_case(db_path, "C000001")["status"] == Status.GENERATED.value
    assert db.status_counts(db_path)[Status.GENERATED.value] == 1
    assert db.result_map(db_path)["C000001"]["max_mises"] == 1.0
