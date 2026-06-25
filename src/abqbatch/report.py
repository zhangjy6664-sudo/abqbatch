"""Generate CSV and HTML batch reports from SQLite state."""

from __future__ import annotations

import csv
import html
from collections import Counter
from pathlib import Path

from abqbatch import db

SUMMARY_COLUMNS = [
    "case_id",
    "status",
    "source_inp",
    "material_profile",
    "load_profile",
    "post_profile",
    "source_sha256",
    "generated_sha256",
    "failure_type",
    "failure_signature",
    "start_time",
    "end_time",
    "max_mises",
    "max_peeq",
]

FAILED_COLUMNS = [
    "case_id",
    "status",
    "failure_type",
    "failure_signature",
    "last_step",
    "last_increment",
    "workdir",
]


def generate_reports(db_path: Path, reports_dir: Path) -> dict[str, Path]:
    """Generate summary, failure, split failure, and HTML reports."""

    reports_dir.mkdir(parents=True, exist_ok=True)
    cases = db.list_cases(db_path)
    attempts = db.latest_attempts(db_path)
    results = db.result_map(db_path)

    summary_rows = []
    failed_rows = []
    for case in cases:
        attempt = attempts.get(case["case_id"], {})
        metrics = results.get(case["case_id"], {})
        summary_rows.append(
            {
                "case_id": case["case_id"],
                "status": case["status"],
                "source_inp": case["source_inp"],
                "material_profile": case.get("material_profile"),
                "load_profile": case.get("load_profile"),
                "post_profile": case.get("post_profile"),
                "source_sha256": case.get("source_sha256"),
                "generated_sha256": case.get("generated_sha256"),
                "failure_type": attempt.get("failure_type"),
                "failure_signature": attempt.get("failure_signature"),
                "start_time": attempt.get("start_time"),
                "end_time": attempt.get("end_time"),
                "max_mises": metrics.get("max_mises"),
                "max_peeq": metrics.get("max_peeq"),
            }
        )
        if str(case["status"]).endswith("FAILED") or case["status"] == "INTERRUPTED":
            failed_rows.append(
                {
                    "case_id": case["case_id"],
                    "status": case["status"],
                    "failure_type": attempt.get("failure_type"),
                    "failure_signature": attempt.get("failure_signature"),
                    "last_step": attempt.get("last_step"),
                    "last_increment": attempt.get("last_increment"),
                    "workdir": attempt.get("workdir"),
                }
            )

    paths = {
        "summary": reports_dir / "summary.csv",
        "completed": reports_dir / "completed_cases.csv",
        "failed": reports_dir / "failed_cases.csv",
        "datacheck_failures": reports_dir / "datacheck_failures.csv",
        "solve_failures": reports_dir / "solve_failures.csv",
        "post_failures": reports_dir / "post_failures.csv",
        "html": reports_dir / "batch_report.html",
    }
    _write_csv(paths["summary"], SUMMARY_COLUMNS, summary_rows)
    _write_csv(
        paths["completed"],
        SUMMARY_COLUMNS,
        [row for row in summary_rows if row["status"] == "POST_DONE"],
    )
    _write_csv(paths["failed"], FAILED_COLUMNS, failed_rows)
    _write_csv(
        paths["datacheck_failures"],
        FAILED_COLUMNS,
        [row for row in failed_rows if row["status"] == "DATACHECK_FAILED"],
    )
    _write_csv(
        paths["solve_failures"],
        FAILED_COLUMNS,
        [row for row in failed_rows if row["status"] == "SOLVE_FAILED"],
    )
    _write_csv(
        paths["post_failures"],
        FAILED_COLUMNS,
        [row for row in failed_rows if row["status"] == "POST_FAILED"],
    )
    paths["html"].write_text(_render_html(summary_rows), encoding="utf-8")
    return paths


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _render_html(rows: list[dict[str, object]]) -> str:
    total = len(rows)
    completed = sum(1 for row in rows if row["status"] == "POST_DONE")
    failed = sum(1 for row in rows if str(row["status"]).endswith("FAILED"))
    failure_counts = Counter(
        str(row.get("failure_type") or "") for row in rows if row.get("failure_type")
    )
    body_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(row['case_id']))}</td>"
        f"<td>{html.escape(str(row['status']))}</td>"
        f"<td>{html.escape(str(row.get('material_profile') or ''))}</td>"
        f"<td>{html.escape(str(row.get('load_profile') or ''))}</td>"
        f"<td>{html.escape(str(row.get('max_mises') or ''))}</td>"
        f"<td>{html.escape(str(row.get('max_peeq') or ''))}</td>"
        "</tr>"
        for row in rows
    )
    failure_items = "".join(
        f"<li>{html.escape(kind)}: {count}</li>" for kind, count in sorted(failure_counts.items())
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>abqbatch report</title></head>
<body>
<h1>abqbatch report</h1>
<p>Total cases: {total}</p>
<p>Completed cases: {completed}</p>
<p>Failed cases: {failed}</p>
<h2>Failure types</h2>
<ul>{failure_items}</ul>
<h2>Cases</h2>
<table border="1" cellspacing="0" cellpadding="4">
<thead><tr><th>case_id</th><th>status</th><th>material</th><th>load</th><th>max_mises</th><th>max_peeq</th></tr></thead>
<tbody>{body_rows}</tbody>
</table>
</body>
</html>
"""
