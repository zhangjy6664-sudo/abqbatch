from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def workspace_tmp(request: pytest.FixtureRequest) -> Path:
    safe_name = request.node.name.replace("/", "_").replace("\\", "_").replace(":", "_")
    target = ROOT / "test_runs" / f"{safe_name}_{uuid.uuid4().hex}"
    target.mkdir(parents=True, exist_ok=False)
    return target


@pytest.fixture()
def basic_project(workspace_tmp: Path) -> Path:
    target = workspace_tmp / "basic_project"
    shutil.copytree(ROOT / "examples" / "basic_project", target)
    return target
