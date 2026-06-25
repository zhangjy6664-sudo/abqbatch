"""Shared exceptions and enums for the abqbatch pipeline."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Small Python 3.10 compatible replacement for enum.StrEnum."""

    def __str__(self) -> str:
        return self.value


class AbqBatchError(Exception):
    """Base exception for abqbatch errors."""


class ConfigError(AbqBatchError):
    """Raised when project configuration is invalid."""


class ValidationError(AbqBatchError):
    """Raised for unrecoverable validation failures."""


class Status(StrEnum):
    DISCOVERED = "DISCOVERED"
    STATIC_CHECKED = "STATIC_CHECKED"
    STATIC_FAILED = "STATIC_FAILED"
    GENERATED = "GENERATED"
    DATACHECKED = "DATACHECKED"
    DATACHECK_FAILED = "DATACHECK_FAILED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SOLVED = "SOLVED"
    SOLVE_FAILED = "SOLVE_FAILED"
    POST_DONE = "POST_DONE"
    POST_FAILED = "POST_FAILED"
    INTERRUPTED = "INTERRUPTED"
    RESTART_READY = "RESTART_READY"
    RESTART_FAILED = "RESTART_FAILED"
    RECOVER_READY = "RECOVER_READY"
    RECOVER_FAILED = "RECOVER_FAILED"
    SKIPPED = "SKIPPED"
    ARCHIVED = "ARCHIVED"


class FailureType(StrEnum):
    INPUT_ERROR = "INPUT_ERROR"
    LICENSE_ERROR = "LICENSE_ERROR"
    DATACHECK_ERROR = "DATACHECK_ERROR"
    CONVERGENCE_ERROR = "CONVERGENCE_ERROR"
    TIME_LIMIT = "TIME_LIMIT"
    NUMERICAL_ERROR = "NUMERICAL_ERROR"
    USER_SUBROUTINE_ERROR = "USER_SUBROUTINE_ERROR"
    POST_ERROR = "POST_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class AttemptType(StrEnum):
    DATACHECK = "DATACHECK"
    SOLVE = "SOLVE"
    POST = "POST"
    RESTART = "RESTART"
    RECOVER = "RECOVER"
