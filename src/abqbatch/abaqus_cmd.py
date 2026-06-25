"""Abaqus command construction and runner abstraction."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AbaqusCommand:
    command: str = "abaqus"
    job: str = ""
    input_file: Path | None = None
    oldjob: str | None = None
    cpus: int | None = None
    memory: str | None = None
    scratch: Path | None = None
    interactive: bool = True
    datacheck: bool = False
    recover: bool = False
    user: Path | None = None
    extra_args: list[str] = field(default_factory=list)

    def to_argv(self) -> list[str]:
        """Return argv suitable for subprocess execution."""

        argv = [self.command]
        if self.extra_args:
            argv.extend(str(arg) for arg in self.extra_args)
        if self.job:
            argv.append(f"job={self.job}")
        if self.input_file is not None:
            argv.append(f"input={self.input_file.as_posix()}")
        if self.oldjob:
            argv.append(f"oldjob={self.oldjob}")
        if self.cpus is not None:
            argv.append(f"cpus={self.cpus}")
        if self.memory:
            argv.append(f"memory={self.memory}")
        if self.scratch is not None:
            argv.append(f"scratch={self.scratch.as_posix()}")
        if self.user is not None:
            argv.append(f"user={self.user.as_posix()}")
        if self.datacheck:
            argv.append("datacheck")
        if self.recover:
            argv.append("recover")
        if self.interactive:
            argv.append("interactive")
        return argv

    def to_shell_string(self) -> str:
        """Return the human-readable Abaqus command string."""

        return " ".join(self.to_argv())


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    timed_out: bool = False


class CommandRunner(Protocol):
    """Runner protocol used to isolate Abaqus from tests."""

    def run(
        self,
        argv: list[str],
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        timeout: int | None = None,
    ) -> RunResult:
        """Run a command and write stdout/stderr logs."""


class SubprocessRunner:
    """Run commands through subprocess."""

    def run(
        self,
        argv: list[str],
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        timeout: int | None = None,
    ) -> RunResult:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            stdout_path.write_text(completed.stdout, encoding="utf-8")
            stderr_path.write_text(completed.stderr, encoding="utf-8")
            return RunResult(completed.returncode, stdout_path, stderr_path)
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text((exc.stderr or "") + "\nTIME_LIMIT\n", encoding="utf-8")
            return RunResult(124, stdout_path, stderr_path, timed_out=True)


class FakeRunner:
    """Test runner that writes deterministic logs and optional Abaqus-like files."""

    def __init__(
        self,
        exit_code: int = 0,
        stdout: str = "fake abaqus runner\n",
        stderr: str = "",
        success_text: bool = True,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.success_text = success_text
        self.calls: list[tuple[list[str], Path]] = []

    def run(
        self,
        argv: list[str],
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        timeout: int | None = None,
    ) -> RunResult:
        del timeout
        cwd.mkdir(parents=True, exist_ok=True)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(self.stdout, encoding="utf-8")
        stderr_path.write_text(self.stderr, encoding="utf-8")
        self.calls.append((argv, cwd))
        job = _job_from_argv(argv)
        if job and self.success_text and self.exit_code == 0:
            (cwd / f"{job}.sta").write_text(
                "THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n", encoding="utf-8"
            )
            if "datacheck" not in argv:
                (cwd / f"{job}.odb").write_text("fake odb\n", encoding="utf-8")
        elif job:
            (cwd / f"{job}.msg").write_text(self.stderr or "Abaqus error\n", encoding="utf-8")
        return RunResult(self.exit_code, stdout_path, stderr_path)


def _job_from_argv(argv: list[str]) -> str | None:
    for item in argv:
        if item.startswith("job="):
            return item.split("=", 1)[1]
    return None
