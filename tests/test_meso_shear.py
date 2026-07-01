from __future__ import annotations

import json
from pathlib import Path

from abqbatch.meso_shear import (
    archive_case_outputs,
    evaluate_shear_curve,
    extract_shear_dat,
    prepare_batch,
)

PROP_VALUES = {
    "E11": 154627.49546800298,
    "E22": 6749.609453460717,
    "G12": 3058.3139141071943,
    "G23": 2320.9043043061315,
    "NU12": 0.2750440318566013,
    "NU23": 0.45408632743937105,
    "S1T": 3159.0188684223463,
    "S1C": 1700.1609737392823,
    "S2T": 133.86274774569725,
    "S2C": 195.67447763258622,
    "S12": 68.92869679099034,
    "S23": 69.78898949368391,
    "G1T": 121.43,
    "G1C": 76.53,
    "G2T": 0.46,
    "G2C": 1.38,
    "ETA": 0.0015,
}


def _matrix_block() -> str:
    return """*Material, name=MATRIX
*Depvar
     11,
*User Material, constants=12
3136.385286, 0.37, 52.12894562, 67.85316409, 113.9768854, 453.1865563, 0.03252151464, 82.39095315,
0.03996741675, 28.48909449, 0.0015, 0.99,
"""


def _fiber_block(name: str) -> str:
    values = list(PROP_VALUES.values())
    return (
        f"*Material, name={name}\n"
        "*Depvar\n"
        "     80,\n"
        "*User Material, constants=17\n"
        + ", ".join(str(value) for value in values[:8])
        + ",\n"
        + ", ".join(str(value) for value in values[8:])
        + ",\n"
    )


def _raw_inp(boundary: str) -> str:
    return f"""*Heading
*Part, name=PART-1
*Node
1, 0., 0., 0.
2, 2., 0., 0.
3, 2., 3., 0.
4, 0., 3., 0.
5, 0., 0., 4.
6, 2., 0., 4.
7, 2., 3., 4.
8, 0., 3., 4.
*End Part
*Assembly, name=ASSEMBLY
*Nset, nset="Constraints Driver Shear_yx"
1
*Nset, nset="Constraints Driver Fx"
2
*End Assembly
** MATERIALS
{_matrix_block()}{_fiber_block("WARP")}{_fiber_block("WEFT")}*Distribution Table, name=PART-1_Table
coord3d, coord3d
*Step, name=Step-1, nlgeom=NO, inc=600
*Static
1., 1., 1e-08, 1.
{boundary}
*End Step
"""


def _selected_json(workspace: Path, reference_inp: Path, umat: Path) -> Path:
    payload = {
        "selected_id": "13_24_24_jq_shear_selected_hashin_vf053_beta2p2_pa1_0",
        "condition": "shear_yx",
        "selected_candidate": "beta_2p2_pa1_0",
        "vf_bundle": 0.53,
        "warp_weft_constants": PROP_VALUES,
        "umat_parameters": {"beta_scale": 2.2, "beta_shear": 5.368e-8, "pa1": 0.0},
        "metrics": {
            "target_shear_modulus_gpa": 2.3813172267255767,
            "target_shear_strength_mpa": 68.52066,
            "target_failure_shear_strain": 0.05037333333333333,
            "compare_max_shear_strain": 0.05,
        },
        "run_paths": {"inp_path": str(reference_inp), "umat_path": str(umat)},
    }
    path = workspace / "selected.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_prepare_batch_patches_shear_deck_and_resumes(workspace_tmp: Path) -> None:
    reference_inp = workspace_tmp / "selected_candidate.inp"
    reference_inp.write_text(
        _raw_inp('*Boundary\n"Constraints Driver Shear_yx", 1, 1, 0.08'),
        encoding="utf-8",
    )
    umat = workspace_tmp / "beta_2p2_pa1_0.for"
    umat.write_text("      SUBROUTINE UMAT()\n      END\n", encoding="utf-8")
    selected = _selected_json(workspace_tmp, reference_inp, umat)
    input_dir = workspace_tmp / "raw"
    input_dir.mkdir()
    (input_dir / "13-12-24.inp").write_text(
        _raw_inp(
            '*Boundary\n"Constraints Driver Fx", 1, 1, -0.04\n'
            '*Boundary\n"Constraints Driver Shear_yx", 1, 1, -8.'
        ),
        encoding="utf-8",
    )

    run_dir = workspace_tmp / "run"
    manifest = prepare_batch(
        selected_json=selected,
        input_dir=input_dir,
        run_dir=run_dir,
        archive_root=workspace_tmp / "archive",
    )
    resumed = prepare_batch(
        selected_json=selected,
        input_dir=input_dir,
        run_dir=run_dir,
        archive_root=workspace_tmp / "archive",
    )

    assert resumed["run_dir"] == manifest["run_dir"]
    case = manifest["cases"][0]
    text = Path(case["working_inp"]).read_text(encoding="utf-8")
    assert "*Step, name=Step-1, nlgeom=NO, inc=100000" in text
    assert "0.01, 1., 1e-10, 0.01" in text
    assert '"Constraints Driver Shear_yx", 1, 1, 0.0800000000' in text
    assert '"Constraints Driver Fx", 1, 1' not in text
    assert (
        '*Node Print, nset="Constraints Driver Shear_yx", frequency=1, summary=NO, totals=YES'
        in text
    )
    assert "*User Material, constants=12" in text
    assert "*User Material, constants=17, unsymm" in text
    assert case["validation"]["ok"]


def test_extract_shear_dat_and_metrics(workspace_tmp: Path) -> None:
    inp = workspace_tmp / "case.inp"
    inp.write_text(
        _raw_inp('*Boundary\n"Constraints Driver Shear_yx", 1, 1, 0.08'),
        encoding="utf-8",
    )
    dat = workspace_tmp / "case.dat"
    dat.write_text(
        """
 THE FOLLOWING TABLE IS PRINTED FOR NODES BELONGING TO NODE SET CONSTRAINTS DRIVER SHEAR_YX
     NODE        U1          U2          U3          RF1         RF2         RF3
     TOTAL       0.010       0.0         0.0         240.0       0.0         0.0
 THE FOLLOWING TABLE IS PRINTED FOR NODES BELONGING TO NODE SET CONSTRAINTS DRIVER SHEAR_YX
     NODE        U1          U2          U3          RF1         RF2         RF3
     TOTAL       0.050       0.0         0.0         1440.0      0.0         0.0
 THE FOLLOWING TABLE IS PRINTED FOR NODES BELONGING TO NODE SET CONSTRAINTS DRIVER SHEAR_YX
     NODE        U1          U2          U3          RF1         RF2         RF3
     TOTAL       0.080       0.0         0.0         1200.0      0.0         0.0
""",
        encoding="utf-8",
    )

    summary = extract_shear_dat(
        dat_path=dat,
        inp_path=inp,
        curve_csv=workspace_tmp / "curve.csv",
        summary_json=workspace_tmp / "summary.json",
        settings={
            "target_shear_modulus_gpa": 2.3813172267255767,
            "target_shear_strength_mpa": 68.52066,
            "target_failure_shear_strain": 0.05037333333333333,
            "modulus_window_start": 0.005,
            "modulus_window_end": 0.015,
            "compare_max_shear_strain": 0.05,
        },
    )

    assert summary["curve"]["point_count"] == 3
    assert summary["volume_mm3"] == 24.0
    assert summary["metrics"]["peak_within_0p05"]["shear_stress_mpa"] == 60.0
    assert summary["metrics"]["peak_full_run"]["shear_stress_mpa"] == 60.0
    assert Path(summary["curve_csv"]).exists()


def test_archive_case_outputs_copies_verifies_and_indexes(workspace_tmp: Path) -> None:
    case_dir = workspace_tmp / "run" / "work" / "cases" / "13-12-24"
    case_dir.mkdir(parents=True)
    source = case_dir / "ms_13_12_24_beta.dat"
    source.write_text("heavy dat\n", encoding="utf-8")
    keep = case_dir / "input"
    keep.mkdir()
    (keep / "working.inp").write_text("keep\n", encoding="utf-8")
    manifest = {
        "run_dir": str(workspace_tmp / "run"),
        "settings": {"archive_root": str(workspace_tmp / "archive")},
    }
    case = {"case_id": "13-12-24", "case_dir": str(case_dir), "job_name": "ms_13"}

    payload = archive_case_outputs(manifest, case, reason="unit_test", remove_source=False)

    assert payload["archived_count"] == 1
    assert source.exists()
    assert Path(payload["files"][0]["archive_path"]).exists()
    assert (keep / "working.inp").exists()
    assert Path(payload["index_csv"]).exists()
    assert Path(payload["index_json"]).exists()



def test_archive_case_outputs_keeps_collision_versions(workspace_tmp: Path) -> None:
    case_dir = workspace_tmp / "run" / "work" / "cases" / "13-12-24"
    case_dir.mkdir(parents=True)
    source = case_dir / "same.dat"
    source.write_text("new dat\n", encoding="utf-8")
    archive_case_dir = workspace_tmp / "archive" / "run" / "13-12-24"
    archive_case_dir.mkdir(parents=True)
    existing = archive_case_dir / "same.dat"
    existing.write_text("old dat\n", encoding="utf-8")
    manifest = {
        "run_dir": str(workspace_tmp / "run"),
        "settings": {"archive_root": str(workspace_tmp / "archive")},
    }
    case = {"case_id": "13-12-24", "case_dir": str(case_dir), "job_name": "ms_13"}

    payload = archive_case_outputs(manifest, case, reason="unit_test", remove_source=False)

    assert payload["archived_count"] == 1
    archived = Path(payload["files"][0]["archive_path"])
    assert archived.exists()
    assert archived.name != "same.dat"
    assert existing.read_text(encoding="utf-8") == "old dat\n"

def test_evaluate_shear_curve_keeps_failed_partial_metrics() -> None:
    metrics = evaluate_shear_curve(
        [(0.01, 20.0), (0.04, 55.0)],
        target_shear_modulus_gpa=2.0,
        target_shear_strength_mpa=60.0,
        target_failure_shear_strain=0.05,
        modulus_window_start=0.005,
        modulus_window_end=0.015,
        compare_max_shear_strain=0.05,
    )

    assert metrics["point_count"] == 2
    assert metrics["peak_within_0p05"]["shear_stress_mpa"] == 55.0
    assert metrics["strength_error_mpa"] == -5.0
