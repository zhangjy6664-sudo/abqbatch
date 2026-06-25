from __future__ import annotations

from pathlib import Path

import yaml

from abqbatch.config import load_project
from abqbatch.generator import generate_project


def test_generator_writes_expected_files_and_is_idempotent(basic_project: Path) -> None:
    cfg = load_project(basic_project)

    generated = generate_project(cfg)
    first_sha = generated[0].generated_sha256
    generated_again = generate_project(cfg)

    case_dir = basic_project / "work" / "cases" / "C000001"
    input_dir = case_dir / "input"
    assert (input_dir / "original.inp").exists()
    assert (input_dir / "generated.inp").exists()
    assert (input_dir / "material.inc").exists()
    assert (input_dir / "load.inc").exists()
    assert (input_dir / "restart.inc").exists()
    assert (input_dir / "output.inc").exists()
    meta = yaml.safe_load((case_dir / "meta.yml").read_text(encoding="utf-8"))
    assert meta["artifacts"]["generated_inp"]["sha256"]
    assert generated_again[0].generated_sha256 == first_sha
