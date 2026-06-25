"""Sequential scheduler for datacheck, solve, post, and resume operations."""

from __future__ import annotations

import json
from pathlib import Path

from abqbatch import db
from abqbatch.abaqus_cmd import AbaqusCommand, CommandRunner, FakeRunner, SubprocessRunner
from abqbatch.config import ProjectConfig
from abqbatch.exceptions import AttemptType, FailureType, Status
from abqbatch.hashes import sha256_file
from abqbatch.monitor import classify_failure, detect_success


def recover_interrupted_cases(config: ProjectConfig) -> int:
    """Mark RUNNING cases without abqbatch lock evidence as INTERRUPTED."""

    count = 0
    for case in db.list_cases(config.db_path, {Status.RUNNING.value}):
        lock = config.work_dir / "cases" / case["case_id"] / ".abqbatch.lock"
        if not _lock_active(lock):
            db.update_status(config.db_path, case["case_id"], Status.INTERRUPTED.value)
            count += 1
    return count


def run_datacheck_cases(
    config: ProjectConfig,
    resume: bool = True,
    rerun_failed: bool = False,
    dry_run: bool = False,
    runner: CommandRunner | None = None,
) -> list[str]:
    """Run Abaqus datacheck for eligible cases."""

    del resume
    statuses = {Status.GENERATED.value}
    if rerun_failed:
        statuses.add(Status.DATACHECK_FAILED.value)
    selected = db.list_cases(config.db_path, statuses)
    return [
        _run_abq_case(config, case, AttemptType.DATACHECK, dry_run, runner) for case in selected
    ]


def run_solve_cases(
    config: ProjectConfig,
    resume: bool = True,
    rerun_failed: bool = False,
    dry_run: bool = False,
    max_parallel: int = 1,
    runner: CommandRunner | None = None,
) -> list[str]:
    """Run Abaqus solve for eligible cases."""

    del max_parallel
    if resume:
        recover_interrupted_cases(config)
    statuses = {Status.DATACHECKED.value, Status.QUEUED.value}
    if rerun_failed:
        statuses.add(Status.SOLVE_FAILED.value)
    selected = db.list_cases(config.db_path, statuses)
    return [_run_abq_case(config, case, AttemptType.SOLVE, dry_run, runner) for case in selected]


def run_post_cases(
    config: ProjectConfig,
    resume: bool = True,
    dry_run: bool = False,
    runner: CommandRunner | None = None,
) -> list[str]:
    """Run ODB postprocessing for SOLVED or POST_FAILED cases."""

    del resume
    statuses = {Status.SOLVED.value, Status.POST_FAILED.value}
    selected = db.list_cases(config.db_path, statuses)
    return [_run_post_case(config, case, dry_run, runner) for case in selected]


def _lock_active(lock: Path) -> bool:
    if not lock.exists():
        return False
    try:
        return lock.read_text(encoding="utf-8").strip().lower() != "released"
    except OSError:
        return True


def _release_lock(lock: Path) -> None:
    try:
        lock.unlink(missing_ok=True)
    except PermissionError:
        lock.write_text("released\n", encoding="utf-8")


def _runner(dry_run: bool, runner: CommandRunner | None) -> CommandRunner:
    if runner is not None:
        return runner
    return FakeRunner() if dry_run else SubprocessRunner()


def _run_abq_case(
    config: ProjectConfig,
    case: dict[str, str],
    attempt_type: AttemptType,
    dry_run: bool,
    runner: CommandRunner | None,
) -> str:
    case_id = case["case_id"]
    case_dir = config.work_dir / "cases" / case_id
    attempt_dir = case_dir / "attempts" / attempt_type.value.lower()
    attempt_dir.mkdir(parents=True, exist_ok=True)
    lock = case_dir / ".abqbatch.lock"
    lock.write_text(attempt_type.value, encoding="utf-8")
    abaqus_cfg = config.project.get("abaqus") or {}
    command = AbaqusCommand(
        command=abaqus_cfg.get("command", "abaqus"),
        job=case.get("job_name") or case_id,
        input_file=Path("input/generated.inp"),
        cpus=int(case.get("cpus") or abaqus_cfg.get("cpus") or 1)
        if attempt_type == AttemptType.SOLVE
        else None,
        memory=case.get("memory") or abaqus_cfg.get("memory"),
        scratch=Path(abaqus_cfg["scratch"]) if abaqus_cfg.get("scratch") else None,
        interactive=bool(abaqus_cfg.get("interactive", True)),
        datacheck=attempt_type == AttemptType.DATACHECK,
    )
    command_path = attempt_dir / "command.txt"
    command_path.write_text(command.to_shell_string() + "\n", encoding="utf-8")
    attempt_id = db.create_attempt(
        config.db_path,
        case_id,
        attempt_type.value,
        command.to_shell_string(),
        case_dir,
    )
    db.add_artifact(
        config.db_path, case_id, attempt_id, "command", command_path, sha256_file(command_path)
    )
    try:
        if attempt_type == AttemptType.SOLVE:
            db.update_status(config.db_path, case_id, Status.RUNNING.value)
        result = _runner(dry_run, runner).run(
            command.to_argv(),
            case_dir,
            attempt_dir / "stdout.log",
            attempt_dir / "stderr.log",
            timeout=(config.project.get("runner") or {}).get("timeout_seconds"),
        )
        success = result.exit_code == 0 and detect_success(case_dir, command.job)
        if success:
            new_status = (
                Status.DATACHECKED.value
                if attempt_type == AttemptType.DATACHECK
                else Status.SOLVED.value
            )
            db.finish_attempt(config.db_path, attempt_id, result.exit_code, new_status)
            db.update_status(config.db_path, case_id, new_status)
        else:
            failure = classify_failure(case_dir, command.job, result.exit_code)
            if attempt_type == AttemptType.DATACHECK:
                failure_type = FailureType.DATACHECK_ERROR.value
                new_status = Status.DATACHECK_FAILED.value
            else:
                failure_type = failure.failure_type
                new_status = Status.SOLVE_FAILED.value
            db.finish_attempt(
                config.db_path,
                attempt_id,
                result.exit_code,
                new_status,
                failure_type,
                failure.failure_signature,
                failure.last_step,
                failure.last_increment,
            )
            db.update_status(config.db_path, case_id, new_status)
        for path in (result.stdout_path, result.stderr_path):
            db.add_artifact(config.db_path, case_id, attempt_id, path.name, path, sha256_file(path))
        odb = case_dir / f"{command.job}.odb"
        if odb.exists():
            db.add_artifact(config.db_path, case_id, attempt_id, "odb", odb, sha256_file(odb))
    finally:
        _release_lock(lock)
    return case_id


def _run_post_case(
    config: ProjectConfig,
    case: dict[str, str],
    dry_run: bool,
    runner: CommandRunner | None,
) -> str:
    from abqbatch.post import build_post_command, build_post_spec, read_metrics

    case_id = case["case_id"]
    case_dir = config.work_dir / "cases" / case_id
    post_dir = case_dir / "post"
    post_dir.mkdir(parents=True, exist_ok=True)
    job_name = case.get("job_name") or case_id
    odb_path = case_dir / f"{job_name}.odb"
    profile_name = case.get("post_profile") or "default"
    spec_path = build_post_spec(config, case, post_dir, profile_name)
    command = build_post_command(config, odb_path, spec_path, post_dir)
    command_path = post_dir / "command.txt"
    command_path.write_text(" ".join(command) + "\n", encoding="utf-8")
    attempt_id = db.create_attempt(
        config.db_path, case_id, AttemptType.POST.value, " ".join(command), post_dir
    )
    db.add_artifact(
        config.db_path, case_id, attempt_id, "post_command", command_path, sha256_file(command_path)
    )

    if dry_run:
        metrics = {
            "case_id": case_id,
            "metrics": {
                "max_mises": {"value": 0.0, "unit": None, "source": "dry-run"},
                "max_peeq": {"value": 0.0, "unit": None, "source": "dry-run"},
            },
        }
        (post_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        (post_dir / "history_curves.csv").write_text(
            "case_id,step,region,variable,time,value\n", encoding="utf-8"
        )
        (post_dir / "field_summary.csv").write_text(
            "case_id,name,value,unit,source\n", encoding="utf-8"
        )
        (post_dir / "post.log").write_text("dry-run postprocessing\n", encoding="utf-8")
        exit_code = 0
    elif not odb_path.exists():
        (post_dir / "post.log").write_text(f"ODB does not exist: {odb_path}\n", encoding="utf-8")
        exit_code = 2
    else:
        result = _runner(False, runner).run(
            command,
            post_dir,
            post_dir / "stdout.log",
            post_dir / "stderr.log",
            timeout=(config.project.get("runner") or {}).get("timeout_seconds"),
        )
        exit_code = result.exit_code

    if exit_code == 0:
        for metric_name, payload in read_metrics(post_dir / "metrics.json").items():
            db.add_result(
                config.db_path,
                case_id,
                metric_name,
                payload.get("value"),
                payload.get("unit"),
                payload.get("source"),
            )
        db.finish_attempt(config.db_path, attempt_id, exit_code, Status.POST_DONE.value)
        db.update_status(config.db_path, case_id, Status.POST_DONE.value)
    else:
        db.finish_attempt(
            config.db_path,
            attempt_id,
            exit_code,
            Status.POST_FAILED.value,
            FailureType.POST_ERROR.value,
            "Postprocessing failed",
        )
        db.update_status(config.db_path, case_id, Status.POST_FAILED.value)
    for artifact in ("metrics.json", "history_curves.csv", "field_summary.csv", "post.log"):
        path = post_dir / artifact
        if path.exists():
            db.add_artifact(config.db_path, case_id, attempt_id, artifact, path, sha256_file(path))
    return case_id
