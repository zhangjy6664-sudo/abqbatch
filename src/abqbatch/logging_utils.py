"""Logging configuration helpers."""

from __future__ import annotations

import logging
from pathlib import Path


class ContextFormatter(logging.Formatter):
    """Formatter that fills missing case/stage/status fields."""

    def format(self, record: logging.LogRecord) -> str:
        for name in ("case_id", "stage", "status"):
            if not hasattr(record, name):
                setattr(record, name, "-")
        return super().format(record)


def setup_logging(project_root: Path, verbose: bool = False) -> logging.Logger:
    """Configure console, pipeline, and error logs for a project."""

    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("abqbatch")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(ContextFormatter("%(levelname)s %(message)s"))

    detail = logging.FileHandler(logs_dir / "pipeline.log", encoding="utf-8")
    detail.setLevel(logging.DEBUG)
    detail.setFormatter(
        ContextFormatter(
            "%(asctime)s %(levelname)s case=%(case_id)s stage=%(stage)s "
            "status=%(status)s %(message)s"
        )
    )

    errors = logging.FileHandler(logs_dir / "errors.log", encoding="utf-8")
    errors.setLevel(logging.ERROR)
    errors.setFormatter(detail.formatter)

    logger.addHandler(console)
    logger.addHandler(detail)
    logger.addHandler(errors)
    return logger
