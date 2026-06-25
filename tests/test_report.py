from __future__ import annotations

from pathlib import Path

from abqbatch.config import load_project
from abqbatch.generator import generate_project
from abqbatch.report import generate_reports
from abqbatch.scheduler import run_datacheck_cases, run_post_cases, run_solve_cases


def test_report_files_are_generated(basic_project: Path) -> None:
    cfg = load_project(basic_project)
    generate_project(cfg)
    run_datacheck_cases(cfg, dry_run=True)
    run_solve_cases(cfg, dry_run=True)
    run_post_cases(cfg, dry_run=True)

    paths = generate_reports(cfg.db_path, cfg.reports_dir)

    assert paths["summary"].exists()
    assert paths["failed"].exists()
    assert paths["html"].exists()
    assert "C000001" in paths["summary"].read_text(encoding="utf-8")
    assert "abqbatch report" in paths["html"].read_text(encoding="utf-8")
