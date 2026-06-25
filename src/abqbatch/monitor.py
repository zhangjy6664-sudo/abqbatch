"""Abaqus output monitoring and failure classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from abqbatch.exceptions import FailureType


@dataclass(frozen=True)
class FailureInfo:
    failure_type: str
    failure_signature: str
    last_step: str | None = None
    last_increment: str | None = None


KEYWORDS: list[tuple[FailureType, list[str]]] = [
    (
        FailureType.LICENSE_ERROR,
        ["License", "Abaqus Error: License", "No licenses available"],
    ),
    (
        FailureType.CONVERGENCE_ERROR,
        [
            "Too many attempts made for this increment",
            "Time increment required is less than the minimum specified",
            "Too many increments needed to complete the step",
        ],
    ),
    (
        FailureType.NUMERICAL_ERROR,
        [
            "excessively distorted",
            "negative eigenvalues",
            "zero pivot",
            "numerical singularity",
            "deformation is too large",
        ],
    ),
    (
        FailureType.INPUT_ERROR,
        [
            "Abaqus/Analysis Input File Processor exited with an error",
            "Unknown assembly set",
            "has not been defined",
            "Unknown element set",
            "Unknown node set",
        ],
    ),
    (
        FailureType.USER_SUBROUTINE_ERROR,
        ["user subroutine", "UMAT", "VUMAT", "compilation", "standardU.dll", "explicitU.dll"],
    ),
]

SUCCESS_MARKERS = [
    "THE ANALYSIS HAS COMPLETED SUCCESSFULLY",
    "COMPLETED",
]


def has_lock_file(workdir: Path, job_name: str) -> bool:
    """Return whether Abaqus lock evidence exists for a job."""

    return (workdir / f"{job_name}.lck").exists()


def _combined_output(workdir: Path, job_name: str) -> str:
    parts: list[str] = []
    for suffix in (".sta", ".msg", ".dat", ".log"):
        path = workdir / f"{job_name}{suffix}"
        if path.exists():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def detect_success(workdir: Path, job_name: str) -> bool:
    """Detect successful completion using logs and lock-file absence."""

    if has_lock_file(workdir, job_name):
        return False
    text = _combined_output(workdir, job_name)
    upper = text.upper()
    if "ERROR" in upper and "COMPLETED SUCCESSFULLY" not in upper:
        return False
    job_marker = f"ABAQUS JOB {job_name} COMPLETED".upper()
    return any(marker in upper for marker in SUCCESS_MARKERS) or job_marker in upper


def classify_failure(workdir: Path, job_name: str, exit_code: int) -> FailureInfo:
    """Classify a failed Abaqus attempt by scanning common output files."""

    text = _combined_output(workdir, job_name)
    if exit_code == 124:
        failure_type = FailureType.TIME_LIMIT
        signature = "Command timed out"
    else:
        failure_type = FailureType.UNKNOWN_ERROR
        signature = "Unknown Abaqus failure"
        lower = text.lower()
        for candidate, phrases in KEYWORDS:
            for phrase in phrases:
                if phrase.lower() in lower:
                    failure_type = candidate
                    signature = phrase
                    break
            if failure_type != FailureType.UNKNOWN_ERROR:
                break
    last_step, last_increment = parse_last_increment(workdir / f"{job_name}.sta")
    return FailureInfo(failure_type.value, signature, last_step, last_increment)


def parse_last_increment(sta_path: Path) -> tuple[str | None, str | None]:
    """Extract the last step and increment numbers from a .sta file."""

    if not sta_path.exists():
        return None, None
    last_step: str | None = None
    last_increment: str | None = None
    pattern = re.compile(r"^\s*(\d+)\s+(\d+)\s+")
    for line in sta_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            last_step, last_increment = match.group(1), match.group(2)
    return last_step, last_increment
