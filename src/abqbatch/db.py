"""SQLite state storage for abqbatch."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from abqbatch.exceptions import Status


def utc_now() -> str:
    """Return a compact UTC timestamp in ISO 8601 format."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with row access by column name."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = TRUNCATE")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Run SQLite operations in a transaction."""

    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    """Create all state tables if they do not already exist."""

    with transaction(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                source_inp TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                generated_inp TEXT,
                generated_sha256 TEXT,
                material_profile TEXT,
                load_profile TEXT,
                post_profile TEXT,
                job_name TEXT,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 100,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                attempt_type TEXT,
                command TEXT,
                workdir TEXT,
                abaqus_version TEXT,
                start_time TEXT,
                end_time TEXT,
                exit_code INTEGER,
                status TEXT,
                failure_type TEXT,
                failure_signature TEXT,
                last_step TEXT,
                last_increment TEXT,
                FOREIGN KEY(case_id) REFERENCES cases(case_id)
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                attempt_id INTEGER,
                kind TEXT,
                path TEXT NOT NULL,
                sha256 TEXT,
                created_at TEXT,
                FOREIGN KEY(case_id) REFERENCES cases(case_id),
                FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
            );

            CREATE TABLE IF NOT EXISTS results (
                result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                metric_value REAL,
                unit TEXT,
                source TEXT,
                created_at TEXT,
                FOREIGN KEY(case_id) REFERENCES cases(case_id)
            );
            """
        )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def upsert_case(db_path: Path, case_data: dict[str, Any]) -> None:
    """Insert or update a case while preserving created_at."""

    now = utc_now()
    status = case_data.get("status", Status.DISCOVERED.value)
    with transaction(db_path) as conn:
        existing = conn.execute(
            "SELECT created_at FROM cases WHERE case_id = ?", (case_data["case_id"],)
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        conn.execute(
            """
            INSERT INTO cases (
                case_id, source_inp, source_sha256, generated_inp, generated_sha256,
                material_profile, load_profile, post_profile, job_name, status,
                priority, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                source_inp=excluded.source_inp,
                source_sha256=excluded.source_sha256,
                generated_inp=COALESCE(excluded.generated_inp, cases.generated_inp),
                generated_sha256=COALESCE(excluded.generated_sha256, cases.generated_sha256),
                material_profile=excluded.material_profile,
                load_profile=excluded.load_profile,
                post_profile=excluded.post_profile,
                job_name=excluded.job_name,
                status=excluded.status,
                priority=excluded.priority,
                updated_at=excluded.updated_at
            """,
            (
                case_data["case_id"],
                case_data["source_inp"],
                case_data["source_sha256"],
                case_data.get("generated_inp"),
                case_data.get("generated_sha256"),
                case_data.get("material_profile"),
                case_data.get("load_profile"),
                case_data.get("post_profile", "default"),
                case_data.get("job_name", case_data["case_id"]),
                status,
                int(case_data.get("priority", 100)),
                created_at,
                now,
            ),
        )


def get_case(db_path: Path, case_id: str) -> dict[str, Any] | None:
    """Return one case by id."""

    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    return _row_to_dict(row)


def list_cases(db_path: Path, statuses: set[str] | None = None) -> list[dict[str, Any]]:
    """Return cases ordered by priority and case_id."""

    with connect(db_path) as conn:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT * FROM cases WHERE status IN ({placeholders}) ORDER BY priority, case_id",
                tuple(statuses),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cases ORDER BY priority, case_id").fetchall()
    return [dict(row) for row in rows]


def update_status(db_path: Path, case_id: str, status: str, **fields: Any) -> None:
    """Update case status and optional case columns."""

    allowed = {
        "generated_inp",
        "generated_sha256",
        "job_name",
        "material_profile",
        "load_profile",
        "post_profile",
    }
    assignments = ["status = ?", "updated_at = ?"]
    values: list[Any] = [status, utc_now()]
    for key, value in fields.items():
        if key in allowed:
            assignments.append(f"{key} = ?")
            values.append(value)
    values.append(case_id)
    with transaction(db_path) as conn:
        conn.execute(f"UPDATE cases SET {', '.join(assignments)} WHERE case_id = ?", values)


def create_attempt(
    db_path: Path,
    case_id: str,
    attempt_type: str,
    command: str,
    workdir: Path,
    abaqus_version: str | None = None,
) -> int:
    """Create a new attempt and return its id."""

    now = utc_now()
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no FROM attempts WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        attempt_no = int(row["next_no"])
        cur = conn.execute(
            """
            INSERT INTO attempts (
                case_id, attempt_no, attempt_type, command, workdir, abaqus_version,
                start_time, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                attempt_no,
                attempt_type,
                command,
                str(workdir),
                abaqus_version,
                now,
                "RUNNING",
            ),
        )
        return int(cur.lastrowid)


def finish_attempt(
    db_path: Path,
    attempt_id: int,
    exit_code: int,
    status: str,
    failure_type: str | None = None,
    failure_signature: str | None = None,
    last_step: str | None = None,
    last_increment: str | None = None,
) -> None:
    """Finish an attempt and record its outcome."""

    with transaction(db_path) as conn:
        conn.execute(
            """
            UPDATE attempts
            SET end_time = ?, exit_code = ?, status = ?, failure_type = ?,
                failure_signature = ?, last_step = ?, last_increment = ?
            WHERE attempt_id = ?
            """,
            (
                utc_now(),
                exit_code,
                status,
                failure_type,
                failure_signature,
                last_step,
                last_increment,
                attempt_id,
            ),
        )


def add_artifact(
    db_path: Path,
    case_id: str,
    attempt_id: int | None,
    kind: str,
    path: Path,
    sha256: str | None = None,
) -> None:
    """Record a generated artifact."""

    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO artifacts (case_id, attempt_id, kind, path, sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (case_id, attempt_id, kind, str(path), sha256, utc_now()),
        )


def add_result(
    db_path: Path,
    case_id: str,
    metric_name: str,
    metric_value: float | None,
    unit: str | None = None,
    source: str | None = None,
) -> None:
    """Record a scalar postprocessing result."""

    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO results (case_id, metric_name, metric_value, unit, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (case_id, metric_name, metric_value, unit, source, utc_now()),
        )


def status_counts(db_path: Path) -> dict[str, int]:
    """Return case counts by status."""

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM cases GROUP BY status"
        ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def latest_attempts(db_path: Path) -> dict[str, dict[str, Any]]:
    """Return the latest attempt row for each case."""

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT a.*
            FROM attempts a
            INNER JOIN (
                SELECT case_id, MAX(attempt_no) AS attempt_no
                FROM attempts
                GROUP BY case_id
            ) m ON a.case_id = m.case_id AND a.attempt_no = m.attempt_no
            """
        ).fetchall()
    return {row["case_id"]: dict(row) for row in rows}


def result_map(db_path: Path) -> dict[str, dict[str, float | None]]:
    """Return scalar results indexed by case and metric name."""

    with connect(db_path) as conn:
        rows = conn.execute("SELECT case_id, metric_name, metric_value FROM results").fetchall()
    results: dict[str, dict[str, float | None]] = {}
    for row in rows:
        results.setdefault(row["case_id"], {})[row["metric_name"]] = row["metric_value"]
    return results
