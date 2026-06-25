from __future__ import annotations

from pathlib import Path

from abqbatch import db
from abqbatch.abaqus_cmd import FakeRunner
from abqbatch.config import load_project
from abqbatch.exceptions import Status
from abqbatch.generator import generate_project
from abqbatch.scheduler import (
    recover_interrupted_cases,
    run_datacheck_cases,
    run_post_cases,
    run_solve_cases,
)


def test_generated_enters_datacheck_and_fake_success(basic_project: Path) -> None:
    cfg = load_project(basic_project)
    generate_project(cfg)

    processed = run_datacheck_cases(cfg, dry_run=True)

    assert processed == ["C000001", "C000002"]
    assert db.get_case(cfg.db_path, "C000001")["status"] == Status.DATACHECKED.value


def test_solve_fake_success_and_post_dry_run(basic_project: Path) -> None:
    cfg = load_project(basic_project)
    generate_project(cfg)
    run_datacheck_cases(cfg, dry_run=True)

    run_solve_cases(cfg, dry_run=True)
    run_post_cases(cfg, dry_run=True)

    assert db.get_case(cfg.db_path, "C000001")["status"] == Status.POST_DONE.value
    assert (basic_project / "work" / "cases" / "C000001" / "post" / "metrics.json").exists()


def test_post_done_is_skipped_and_solved_enters_post(basic_project: Path) -> None:
    cfg = load_project(basic_project)
    generate_project(cfg)
    db.update_status(cfg.db_path, "C000001", Status.POST_DONE.value)
    db.update_status(cfg.db_path, "C000002", Status.SOLVED.value)
    (basic_project / "work" / "cases" / "C000002" / "C000002.odb").write_text(
        "odb", encoding="utf-8"
    )

    processed = run_post_cases(cfg, dry_run=True)

    assert processed == ["C000002"]


def test_running_without_lock_becomes_interrupted(basic_project: Path) -> None:
    cfg = load_project(basic_project)
    generate_project(cfg)
    db.update_status(cfg.db_path, "C000001", Status.RUNNING.value)

    assert recover_interrupted_cases(cfg) == 1
    assert db.get_case(cfg.db_path, "C000001")["status"] == Status.INTERRUPTED.value


def test_fake_runner_failure_marks_datacheck_failed(basic_project: Path) -> None:
    cfg = load_project(basic_project)
    generate_project(cfg)

    run_datacheck_cases(cfg, runner=FakeRunner(exit_code=1, stderr="Unknown node set"))

    assert db.get_case(cfg.db_path, "C000001")["status"] == Status.DATACHECK_FAILED.value
