"""Postprocessing orchestration helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from abqbatch.config import ProjectConfig


def post_script_path() -> Path:
    """Return the packaged Abaqus Python postprocessing script path."""

    return Path(__file__).parent / "abaqus_scripts" / "post_odb.py"


def build_post_spec(
    config: ProjectConfig,
    case: dict[str, Any],
    post_dir: Path,
    profile_name: str,
) -> Path:
    """Write the selected postprocess profile as JSON for Abaqus Python."""

    profiles = config.postprocess.get("profiles") or {}
    profile = profiles.get(profile_name)
    if profile is None:
        raise KeyError(f"Unknown postprocess profile: {profile_name}")
    spec = {"case_id": case["case_id"], "profile": profile}
    spec_path = post_dir / "post_spec.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec_path


def build_post_command(config: ProjectConfig, odb: Path, spec: Path, out_dir: Path) -> list[str]:
    """Build the Abaqus Python postprocessing command."""

    command = (config.project.get("abaqus") or {}).get("command", "abaqus")
    return [
        command,
        "python",
        str(post_script_path()),
        "--odb",
        str(odb),
        "--spec",
        str(spec),
        "--out",
        str(out_dir),
    ]


def read_metrics(metrics_path: Path) -> dict[str, dict[str, Any]]:
    """Read metrics.json and return the metrics mapping."""

    if not metrics_path.exists():
        return {}
    data = json.loads(metrics_path.read_text(encoding="utf-8"))
    return data.get("metrics") or {}
