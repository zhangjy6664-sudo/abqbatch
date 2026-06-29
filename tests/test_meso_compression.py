from __future__ import annotations

import json
import zipfile
from pathlib import Path

from abqbatch.meso_compression import prepare_batch, write_results_xlsx


def _sample_params(root: Path) -> dict:
    constants_17 = {
        "E11": 243346.5988,
        "E22": 10713.31207,
        "G12": 7556.915715,
        "G23": 4016.987077,
        "NU12": 0.2239885504,
        "NU23": 0.3334973654,
        "S1T": 4969.713326,
        "S1C": 2660.380818,
        "S2T": 152.3870166,
        "S2C": 192.3833568,
        "S12": 82.00330081,
        "S23": 82.01530451,
        "G1T": 121.43,
        "G1C": 5.0,
        "G2T": 0.46,
        "G2C": 1.38,
        "ETA": 0.0015,
    }
    order_17 = list(constants_17)
    return {
        "project_root": str(root),
        "candidate_id": "puckzt_angle_0p8_g1c5",
        "purpose": "test",
        "model": {},
        "candidate_parameters": {"fiber_angle_deg": 0.8, "g1c": 5.0},
        "umat": {
            "working_copy": "umat.for",
            "fiber_branch_requirement": {"NPROPS_min": 17, "NSTATV_min": 40},
            "matrix_branch_requirement": {"NPROPS_min": 12, "NSTATV_min": 11},
        },
        "materials": {
            "MATRIX": {
                "depvar": 11,
                "constants_order": [
                    "E",
                    "NU",
                    "SY0",
                    "QSAT",
                    "BEXP",
                    "HLIN",
                    "EK0T",
                    "BDT",
                    "EK0C",
                    "BDC",
                    "ETAD",
                    "DMAX",
                ],
                "constants": {
                    "E": 3136.385286,
                    "NU": 0.37,
                    "SY0": 52.12894562,
                    "QSAT": 67.85316409,
                    "BEXP": 113.9768854,
                    "HLIN": 453.1865563,
                    "EK0T": 0.03252151464,
                    "BDT": 82.39095315,
                    "EK0C": 0.03996741675,
                    "BDC": 28.48909449,
                    "ETAD": 0.0015,
                    "DMAX": 0.99,
                },
            },
            "WARP": {"depvar": 80, "constants_order": order_17, "constants": constants_17},
            "WEFT": {"depvar": 80, "constants_order": order_17, "constants": constants_17},
        },
        "step_and_load": {
            "name": "Step-1",
            "nlgeom": "NO",
            "max_increments": 2000,
            "static": {"initial": 0.005, "total": 1.0, "minimum": 1e-8, "maximum": 0.005},
            "compression_bc": {
                "node_set": "Constraints Driver Fx",
                "dof_start": 1,
                "dof_end": 1,
                "value": -0.035,
            },
        },
        "outputs_and_postprocess": {
            "dat_node_print": {
                "node_set": "Constraints Driver Fx",
                "frequency": 1,
                "variables": ["U", "RF"],
                "totals": "YES",
            },
            "modulus_window_strain": {"start": 0.0021, "end": 0.0052},
        },
        "early_stop_and_job": {
            "abaqus_cmd": r"C:\Users\11843\codex_abaqus.cmd",
            "cpus": 16,
            "drop_fraction": 0.02,
            "poll_seconds": 15.0,
            "min_points": 10,
            "min_peak_mpa": 100.0,
        },
    }


def _sample_inp(step_boundary: str) -> str:
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
*Nset, nset="Constraints Driver Fx"
1
*Nset, nset="Master Node 1"
2
*End Assembly
** MATERIALS
** MATRIX-OLD
*Material, name=MATRIX
*Depvar
     50,
*User Material, constants=7
3550., 0.33, 95.5, 171.9, 1., 1., 0.0015
** WARP-OLD
*Material, name=WARP
*Depvar
     80,
*User Material, constants=17
250050., 12346.9, 8174.91, 7743.88, 0.275475, 0.323947, 3937.5, 2050.37
67.9711, 137.125, 71.9392, 53.1, 121.43, 76.53, 0.46, 1.38
0.0015,
** WEFT-OLD
*Material, name=WEFT
*Depvar
     80,
*User Material, constants=17
208375., 12346.9, 8174.91, 7743.88, 0.275475, 0.323947, 3937.5, 2050.37
67.9711, 137.125, 71.9392, 53.1, 121.43, 76.53, 0.46, 1.38
0.0015,
*Distribution Table, name=PART-1_DISCFIELD-1_Table
coord3d, coord3d
*Boundary
"Master Node 1", 1, 1
*Boundary
"Master Node 1", 2, 2
*Boundary
"Master Node 1", 3, 3
*Step, name=Step-1, nlgeom=NO, inc=600
*Static
1., 1., 1e-08, 1.
{step_boundary}
*Output, history
*Node Output, nset="Constraints Driver Fx"
RF1, U1
*End Step
"""


def test_prepare_batch_patches_materials_step_bc_and_node_print(workspace_tmp: Path) -> None:
    params = _sample_params(workspace_tmp)
    (workspace_tmp / "umat.for").write_text(
        "      SUBROUTINE UMAT()\n      END\n",
        encoding="utf-8",
    )
    params_path = workspace_tmp / "params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")
    input_dir = workspace_tmp / "raw"
    input_dir.mkdir()
    (input_dir / "13-12-24.inp").write_text(
        _sample_inp('*Boundary\n"Constraints Driver Fx", 1, 1, 0.06'),
        encoding="utf-8",
    )
    (input_dir / "13-48-24.inp").write_text(
        _sample_inp('*Boundary\n"Constraints Driver Shear_yx", 1, 1, 0.06'),
        encoding="utf-8",
    )

    manifest = prepare_batch(
        params_json=params_path,
        input_dir=input_dir,
        run_dir=workspace_tmp / "run",
    )

    assert manifest["case_count"] == 2
    for case in manifest["cases"]:
        text = Path(case["working_inp"]).read_text(encoding="utf-8")
        assert "*User Material, constants=12" in text
        assert "3550., 0.33, 95.5" not in text
        assert "*Step, name=Step-1, nlgeom=NO, inc=2000" in text
        assert "0.005, 1., 1e-08, 0.005" in text
        assert '"Constraints Driver Fx", 1, 1, -0.0350000000' in text
        assert '"Constraints Driver Shear_yx", 1, 1' not in text
        assert '*Node Print, nset="Constraints Driver Fx", frequency=1, totals=YES' in text
        assert case["validation"]["ok"]


def test_results_xlsx_is_written(workspace_tmp: Path) -> None:
    params = _sample_params(workspace_tmp)
    (workspace_tmp / "umat.for").write_text(
        "      SUBROUTINE UMAT()\n      END\n",
        encoding="utf-8",
    )
    params_path = workspace_tmp / "params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")
    input_dir = workspace_tmp / "raw"
    input_dir.mkdir()
    (input_dir / "13-12-24.inp").write_text(
        _sample_inp('*Boundary\n"Constraints Driver Fx", 1, 1, 0.06'),
        encoding="utf-8",
    )
    run_dir = workspace_tmp / "run"
    prepare_batch(params_json=params_path, input_dir=input_dir, run_dir=run_dir)

    xlsx = write_results_xlsx(run_dir)

    assert xlsx.exists()
    with zipfile.ZipFile(xlsx) as archive:
        names = set(archive.namelist())
    assert "xl/workbook.xml" in names
    assert "xl/worksheets/sheet1.xml" in names
