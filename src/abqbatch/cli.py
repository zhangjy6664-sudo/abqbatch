"""Command line interface for abqbatch."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from abqbatch import db
from abqbatch.abaqus_cmd import AbaqusCommand
from abqbatch.config import init_project as create_project
from abqbatch.config import load_project
from abqbatch.exceptions import AttemptType, Status
from abqbatch.generator import generate_project
from abqbatch.hashes import sha256_file
from abqbatch.inventory import inventory_project
from abqbatch.logging_utils import setup_logging
from abqbatch.meso_compression import app as meso_compression_app
from abqbatch.meso_shear import app as meso_shear_app
from abqbatch.report import generate_reports
from abqbatch.restart import (
    build_explicit_recover_command,
    build_standard_restart_command,
    mark_recover_ready,
    mark_restart_ready,
)
from abqbatch.scheduler import run_datacheck_cases, run_post_cases, run_solve_cases
from abqbatch.validators import validate_project

app = typer.Typer(help="Batch Abaqus INP generation and execution pipeline.")
app.add_typer(meso_compression_app, name="meso-compression")
app.add_typer(meso_shear_app, name="meso-shear")

@app.command("init")
def init_command(
    path: Path = typer.Option(Path("."), "--path", help="Project directory to initialize."),
    force: bool = typer.Option(False, "--force", help="Overwrite default config files."),
) -> None:
    """Initialize an abqbatch project directory."""

    created = create_project(path, force=force)
    cfg = load_project(path)
    db.init_db(cfg.db_path)
    typer.echo(f"Initialized {path.resolve()} ({len(created)} paths/files touched)")


@app.command("inventory")
def inventory_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
    input_dir: Path = typer.Option(Path("raw_inp"), "--input", help="Raw INP directory."),
    output: Path = typer.Option(Path("configs/cases.csv"), "--output", help="Output cases.csv."),
    refresh: bool = typer.Option(False, "--refresh", help="Regenerate case ids from scratch."),
) -> None:
    """Scan raw INP files and register cases."""

    cfg = load_project(project)
    setup_logging(cfg.root)
    rows = inventory_project(cfg.root, input_dir, output, cfg.db_path, refresh=refresh)
    typer.echo(f"Registered {len(rows)} cases")


@app.command("validate")
def validate_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
    static: bool = typer.Option(False, "--static", help="Run static validation."),
) -> None:
    """Validate source INP files and selected config profiles."""

    cfg = load_project(project)
    setup_logging(cfg.root)
    issues = validate_project(cfg, static=static)
    errors = sum(1 for issue in issues if issue.severity == "ERROR")
    warnings = sum(1 for issue in issues if issue.severity == "WARNING")
    typer.echo(f"Validation complete: {errors} errors, {warnings} warnings")
    if errors:
        raise typer.Exit(1)


@app.command("generate")
def generate_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
) -> None:
    """Generate per-case derived input files."""

    cfg = load_project(project)
    setup_logging(cfg.root)
    generated = generate_project(cfg)
    typer.echo(f"Generated {len(generated)} cases")


@app.command("datacheck")
def datacheck_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
    resume: bool = typer.Option(False, "--resume", help="Resume eligible cases."),
    rerun_failed: bool = typer.Option(False, "--rerun-failed", help="Retry failed datachecks."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use fake runner; do not call Abaqus."),
) -> None:
    """Run Abaqus datacheck for generated cases."""

    cfg = load_project(project)
    setup_logging(cfg.root)
    cases = run_datacheck_cases(cfg, resume=resume, rerun_failed=rerun_failed, dry_run=dry_run)
    typer.echo(f"Datacheck processed {len(cases)} cases")


@app.command("run")
def run_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
    resume: bool = typer.Option(False, "--resume", help="Resume eligible cases."),
    rerun_failed: bool = typer.Option(False, "--rerun-failed", help="Retry failed solves."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use fake runner; do not call Abaqus."),
    max_parallel: int = typer.Option(1, "--max-parallel", help="Maximum parallel jobs."),
) -> None:
    """Run Abaqus solves for datachecked cases."""

    cfg = load_project(project)
    setup_logging(cfg.root)
    cases = run_solve_cases(
        cfg,
        resume=resume,
        rerun_failed=rerun_failed,
        dry_run=dry_run,
        max_parallel=max_parallel,
    )
    typer.echo(f"Solve processed {len(cases)} cases")


@app.command("post")
def post_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
    resume: bool = typer.Option(False, "--resume", help="Resume postprocessing."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Write fake post outputs."),
) -> None:
    """Postprocess solved cases."""

    cfg = load_project(project)
    setup_logging(cfg.root)
    cases = run_post_cases(cfg, resume=resume, dry_run=dry_run)
    typer.echo(f"Postprocessed {len(cases)} cases")


@app.command("report")
def report_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
) -> None:
    """Generate batch reports."""

    cfg = load_project(project)
    setup_logging(cfg.root)
    paths = generate_reports(cfg.db_path, cfg.reports_dir)
    typer.echo(f"Report written to {paths['summary']}")


@app.command("status")
def status_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
    case_id: str | None = typer.Option(None, "--case", help="Show one case."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show case status counts or one case."""

    cfg = load_project(project)
    payload = db.get_case(cfg.db_path, case_id) if case_id else db.status_counts(cfg.db_path)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        if isinstance(payload, dict):
            for key, value in payload.items():
                typer.echo(f"{key}: {value}")
        else:
            typer.echo(payload)


@app.command("restart")
def restart_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
    case_id: str = typer.Option(..., "--case", help="Case id."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only record the restart command."),
) -> None:
    """Generate and record a Standard restart command."""

    del dry_run
    cfg = load_project(project)
    case = db.get_case(cfg.db_path, case_id)
    if case is None:
        raise typer.BadParameter(f"Unknown case: {case_id}")
    oldjob = case.get("job_name") or case_id
    newjob = f"{case_id}_restart_001"
    command = build_standard_restart_command(case_id, oldjob, newjob)
    _record_aux_command(cfg, case_id, AttemptType.RESTART.value, command)
    mark_restart_ready(cfg.db_path, case_id)
    typer.echo(command.to_shell_string())


@app.command("recover")
def recover_command(
    project: Path = typer.Option(Path("."), "--project", help="Project root."),
    case_id: str = typer.Option(..., "--case", help="Case id."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only record the recover command."),
) -> None:
    """Generate and record an Explicit recover command."""

    del dry_run
    cfg = load_project(project)
    case = db.get_case(cfg.db_path, case_id)
    if case is None:
        raise typer.BadParameter(f"Unknown case: {case_id}")
    command = build_explicit_recover_command(case.get("job_name") or case_id)
    _record_aux_command(cfg, case_id, AttemptType.RECOVER.value, command)
    mark_recover_ready(cfg.db_path, case_id)
    typer.echo(command.to_shell_string())


def _record_aux_command(cfg, case_id: str, attempt_type: str, command: AbaqusCommand) -> None:
    case_dir = cfg.work_dir / "cases" / case_id
    attempt_dir = case_dir / attempt_type.lower()
    attempt_dir.mkdir(parents=True, exist_ok=True)
    command_path = attempt_dir / "command.txt"
    command_path.write_text(command.to_shell_string() + "\n", encoding="utf-8")
    attempt_id = db.create_attempt(
        cfg.db_path, case_id, attempt_type, command.to_shell_string(), attempt_dir
    )
    db.finish_attempt(cfg.db_path, attempt_id, 0, Status.RESTART_READY.value)
    db.add_artifact(
        cfg.db_path, case_id, attempt_id, "command", command_path, sha256_file(command_path)
    )
