from __future__ import annotations

from pathlib import Path

from abqbatch.config import load_project
from abqbatch.validators import validate_inp_file


def test_missing_include_reports_error(workspace_tmp: Path) -> None:
    inp = workspace_tmp / "bad.inp"
    inp.write_text("*Heading\n*Include, input=missing.inc\n", encoding="utf-8")

    issues = validate_inp_file(inp, project_root=workspace_tmp)

    assert any(issue.code == "INCLUDE_MISSING" and issue.severity == "ERROR" for issue in issues)


def test_unclosed_step_reports_error(workspace_tmp: Path) -> None:
    inp = workspace_tmp / "bad.inp"
    inp.write_text("*Heading\n*Step, name=Step-1\n*Static\n", encoding="utf-8")

    issues = validate_inp_file(inp)

    assert any(issue.code == "UNCLOSED_BLOCK" for issue in issues)


def test_missing_section_material_reports_error(workspace_tmp: Path) -> None:
    inp = workspace_tmp / "bad.inp"
    inp.write_text(
        "*Heading\n*Part, name=P\n*Solid Section, elset=E, material=Missing\n*End Part\n",
        encoding="utf-8",
    )

    issues = validate_inp_file(inp)

    assert any(issue.code == "SECTION_MATERIAL_MISSING" for issue in issues)


def test_example_inp_has_no_errors(basic_project: Path) -> None:
    cfg = load_project(basic_project)
    issues = validate_inp_file(
        basic_project / "raw_inp" / "model_001.inp",
        "C000001",
        basic_project,
        "steel_E210GPa",
        "disp_ramp_y",
        "default",
        cfg.materials,
        cfg.loads,
        cfg.postprocess,
    )

    assert not [issue for issue in issues if issue.severity == "ERROR"]
