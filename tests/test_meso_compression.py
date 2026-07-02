from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import pytest

import abqbatch.meso_compression as meso_compression
from abqbatch.meso_compression import (
    CURRENT_SIM,
    REFERENCE_SIM,
    TARGET_ONLY,
    archive_odb_files,
    parse_sim_summary_xlsx,
    parse_strength_targets,
    prepare_batch,
    prepare_calibration_candidate,
    read_xlsx_rows,
    run_calibration_batch,
    select_strength_candidate,
    strength_error_pct,
    write_results_xlsx,
    write_xlsx,
)


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


def test_strength_target_parser_is_target_only_and_marks_exclusions(workspace_tmp: Path) -> None:
    target_xlsx = workspace_tmp / "targets.xlsx"
    write_xlsx(
        target_xlsx,
        {
            "Sheet1": [
                ["group", "dim_a", "dim_b", "ignored", "compression_strength"],
                [0, 12, 24, 999, 300.0],
                [1, 24, 24, 999, 220.0],
                [2, 48, 72, 999, 500.0],
            ]
        },
    )

    targets = parse_strength_targets(target_xlsx)

    assert [row["case_id"] for row in targets] == ["QJ-12-24", "13-24-24", "132-48-72"]
    assert {row["data_role"] for row in targets} == {TARGET_ONLY}
    assert targets[0]["target_strength_mpa"] == 300.0
    assert targets[1]["excluded"] is True
    assert targets[2]["source_inp"].endswith("132-48-72-jq.inp")


def test_sim_summary_parser_outputs_only_actual_sim_points(workspace_tmp: Path) -> None:
    summary_xlsx = workspace_tmp / "summary.xlsx"
    write_xlsx(
        summary_xlsx,
        {
            "all_cases_summary": [
                ["case_id", "status", "job_name", "compressive_strength_mpa"],
                ["13-12-24", "SOLVED", "mc_13_12_24_puckzt_angle_0p8_g1c5", 180.0],
                ["13-24-24", "DATACHECK_FAILED", "mc_13_24_24_puckzt_angle_0p8_g1c5", None],
            ]
        },
    )

    records = parse_sim_summary_xlsx(summary_xlsx, data_role=REFERENCE_SIM)

    assert len(records) == 1
    assert records[0]["data_role"] == REFERENCE_SIM
    assert records[0]["case_id"] == "13-12-24"
    assert records[0]["angle_deg"] == 0.8
    assert records[0]["g1c"] == 5.0
    assert records[0]["compressive_strength_mpa"] == 180.0


def test_interpolator_rejects_target_only_records() -> None:
    target = {
        "data_role": TARGET_ONLY,
        "case_id": "13-12-24",
        "case_key": "13-12-24",
        "target_strength_mpa": 100.0,
    }

    with pytest.raises(ValueError, match="non-simulation"):
        select_strength_candidate(target, [target])


def test_out_of_bounds_reference_can_accept_case() -> None:
    target = {
        "data_role": TARGET_ONLY,
        "case_id": "13-12-24",
        "case_key": "13-12-24",
        "target_strength_mpa": 100.0,
    }
    sim_records = [
        {
            "data_role": REFERENCE_SIM,
            "case_id": "13-12-24",
            "case_key": "13-12-24",
            "angle_deg": 0.8,
            "g1c": 5.0,
            "compressive_strength_mpa": 102.0,
        }
    ]

    decision = select_strength_candidate(target, sim_records)

    assert decision["status"] == "accepted"
    assert decision["accepted_source_role"] == REFERENCE_SIM
    assert decision["accepted_angle_deg"] == 0.8
    assert decision["accepted_g1c"] == 5.0


def test_strength_candidate_uses_simulation_bracket() -> None:
    target = {
        "data_role": TARGET_ONLY,
        "case_id": "13-12-24",
        "case_key": "13-12-24",
        "target_strength_mpa": 100.0,
    }
    sim_records = [
        {
            "data_role": REFERENCE_SIM,
            "case_id": "13-12-24",
            "case_key": "13-12-24",
            "angle_deg": 1.0,
            "g1c": 5.0,
            "compressive_strength_mpa": 120.0,
        },
        {
            "data_role": CURRENT_SIM,
            "case_id": "13-12-24",
            "case_key": "13-12-24",
            "angle_deg": 3.0,
            "g1c": 5.0,
            "compressive_strength_mpa": 80.0,
        },
    ]

    decision = select_strength_candidate(target, sim_records)

    assert decision["status"] == "planned"
    assert decision["reason"] == "same_g1c_strength_bracket"
    assert decision["angle_deg"] == 2.0
    assert decision["g1c"] == 5.0
    assert decision["planned_data_role"] == CURRENT_SIM


def test_strength_error_uses_target_denominator() -> None:
    assert strength_error_pct(target_strength_mpa=200.0, simulated_strength_mpa=170.0) == 15.0


def test_archive_odb_files_moves_odb_and_writes_indexes(workspace_tmp: Path) -> None:
    run_dir = workspace_tmp / "run"
    case_dir = run_dir / "work" / "cases" / "13-12-24"
    case_dir.mkdir(parents=True)
    odb = case_dir / "mc_13_12_24_attempt1.odbprobe"
    odb.write_bytes(b"fake odb bytes")
    archive_root = workspace_tmp / "archive_root"
    index_dir = run_dir / "reports"
    attempt_rows = [
        {
            "job_name": "mc_13_12_24_attempt1",
            "case_id": "13-12-24",
            "attempt_id": "1",
            "angle_deg": "2.0",
            "g1c": "5.0",
        }
    ]

    moved_sources: list[Path] = []

    def fake_move(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        moved_sources.append(src)

    result = archive_odb_files(
        run_dir,
        archive_root=archive_root,
        index_dir=index_dir,
        batch_id="batch_001",
        attempt_rows=attempt_rows,
        stamp="20260701_120000",
        file_glob="*.odbprobe",
        move_file=fake_move,
    )

    assert result["archived_count"] == 1
    assert moved_sources == [odb]
    assert Path(result["d_index"]).exists()
    assert Path(result["e_index"]).exists()
    with Path(result["d_index"]).open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["case_id"] == "13-12-24"
    assert rows[0]["attempt_id"] == "1"
    assert rows[0]["size_bytes"] == str(len(b"fake odb bytes"))
    assert len(rows[0]["sha256"]) == 64
    assert Path(rows[0]["archive_path"]).exists()



def _write_angle_umat(path: Path) -> None:
    path.write_text(
        "      SUBROUTINE UMAT()\n"
        "      ANGLE_INI=0.8/180.*3.141592654\n"
        "C     ANGLE_INI=abs(STATEV(8))/180.*3.141592654\n"
        "      END\n",
        encoding="utf-8",
    )


def test_prepare_calibration_candidate_patches_params_umat_and_source(workspace_tmp: Path) -> None:
    params = _sample_params(workspace_tmp)
    _write_angle_umat(workspace_tmp / "umat.for")
    params_path = workspace_tmp / "params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")
    source = workspace_tmp / "132-48-72-jq.inp"
    source.write_text(
        _sample_inp('*Boundary\n"Constraints Driver Fx", 1, 1, 0.06'),
        encoding="utf-8",
    )
    candidate = {
        "batch_slot": 1,
        "case_id": "132-48-72",
        "case_key": "132-48-72",
        "source_inp": str(source),
        "attempt_id": 1,
        "angle_deg": 2.5,
        "g1c": 80.0,
        "target_strength_mpa": 300.0,
    }

    prepared = prepare_calibration_candidate(
        params_json=params_path,
        candidate=candidate,
        batch_dir=workspace_tmp / "batch",
    )

    manifest = prepared["manifest"]
    assert manifest["cases"][0]["case_id"] == "132-48-72"
    assert manifest["materials"]["WARP"]["constants"]["G1C"] == 80.0
    generated_params = json.loads(Path(prepared["params_json"]).read_text(encoding="utf-8"))
    assert generated_params["candidate_parameters"]["fiber_angle_deg"] == 2.5
    assert generated_params["materials"]["WEFT"]["constants"]["G1C"] == 80.0
    umat_text = Path(generated_params["umat"]["working_copy"]).read_text(encoding="utf-8")
    assert "ANGLE_INI=2.5/180.*3.141592654" in umat_text
    canonical_source = Path(prepared["attempt_dir"]) / "source_inp" / "132-48-72.inp"
    assert canonical_source.exists()


def test_run_calibration_batch_updates_history_and_archives(
    workspace_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = _sample_params(workspace_tmp)
    _write_angle_umat(workspace_tmp / "umat.for")
    params_path = workspace_tmp / "params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")
    input_source = workspace_tmp / "13-12-24.inp"
    input_source.write_text(
        _sample_inp('*Boundary\n"Constraints Driver Fx", 1, 1, 0.06'),
        encoding="utf-8",
    )
    candidate = {
        "batch_slot": 1,
        "case_id": "13-12-24",
        "case_key": "13-12-24",
        "source_inp": str(input_source),
        "attempt_id": 1,
        "angle_deg": 2.0,
        "g1c": 40.0,
        "target_strength_mpa": 200.0,
    }

    def fake_datacheck(run_dir: Path, *, case_ids=None, dry_run: bool = False):
        del case_ids, dry_run
        manifest = meso_compression.load_manifest(run_dir)
        case = manifest["cases"][0]
        state = meso_compression.read_case_state(case)
        state["status"] = "DATACHECKED"
        meso_compression.write_case_state(case, state)
        return [state]

    def fake_analysis(run_dir: Path, *, case_ids=None, dry_run: bool = False):
        del case_ids, dry_run
        manifest = meso_compression.load_manifest(run_dir)
        case = manifest["cases"][0]
        summary = {
            "metrics": {"compressive_strength_mpa": 185.0},
            "curve": {"point_count": 12},
        }
        meso_compression._write_json(Path(case["summary_json"]), summary)
        state = meso_compression.read_case_state(case)
        state.update({"status": "SOLVED", "metrics": summary["metrics"], "curve": summary["curve"]})
        meso_compression.write_case_state(case, state)
        return [state]

    archive_calls: list[dict] = []

    def fake_archive(run_dir: Path, **kwargs):
        archive_calls.append({"run_dir": run_dir, **kwargs})
        archive_root = kwargs["archive_root"]
        index_dir = kwargs["index_dir"]
        return {
            "archive_dir": archive_root / "fake",
            "d_index": index_dir / "fake.csv",
            "e_index": archive_root / "fake" / "fake.csv",
            "archived_count": 0,
        }

    monkeypatch.setattr(meso_compression, "datacheck_cases", fake_datacheck)
    monkeypatch.setattr(meso_compression, "analysis_cases", fake_analysis)
    monkeypatch.setattr(meso_compression, "archive_odb_files", fake_archive)

    history_csv = workspace_tmp / "history.csv"
    result = run_calibration_batch(
        params_json=params_path,
        candidates=[candidate],
        output_root=workspace_tmp / "calibration",
        batch_id="batch_001",
        history_csv=history_csv,
        archive_root=workspace_tmp / "archive",
    )

    assert result["history_rows"][0]["data_role"] == CURRENT_SIM
    assert result["history_rows"][0]["compressive_strength_mpa"] == 185.0
    assert result["history_rows"][0]["error_pct"] == 7.5
    assert result["history_rows"][0]["accepted"] is True
    assert archive_calls[0]["batch_id"] == "batch_001"
    archived_jobs = {row["job_name"] for row in archive_calls[0]["attempt_rows"]}
    assert any(job.startswith("mc_13_12_24_cal_a2") for job in archived_jobs)
    assert any(job.endswith("_dc") for job in archived_jobs)
    with history_csv.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["data_role"] == CURRENT_SIM
    assert rows[0]["case_id"] == "13-12-24"
    assert rows[0]["compressive_strength_mpa"] == "185.0"


def test_calibrate_strength_refreshes_next_candidates_after_execution(
    workspace_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_xlsx = workspace_tmp / "targets.xlsx"
    write_xlsx(
        target_xlsx,
        {
            "Sheet1": [
                ["group", "dim_a", "dim_b", "ignored", "compression_strength"],
                [1, 12, 12, 999, 100.0],
                [1, 12, 24, 999, 200.0],
            ]
        },
    )
    params_path = workspace_tmp / "params.json"
    params_path.write_text("{}", encoding="utf-8")

    def fake_run_calibration_batch(**kwargs):
        candidate = kwargs["candidates"][0]
        assert candidate["case_id"] == "13-12-12"
        return {
            "batch_id": kwargs["batch_id"],
            "history_rows": [
                {
                    "data_role": CURRENT_SIM,
                    "batch_id": kwargs["batch_id"],
                    "case_id": "13-12-12",
                    "case_key": "13-12-12",
                    "attempt_id": candidate["attempt_id"],
                    "angle_deg": candidate["angle_deg"],
                    "g1c": candidate["g1c"],
                    "target_strength_mpa": 100.0,
                    "compressive_strength_mpa": 100.0,
                    "error_pct": 0.0,
                    "accepted": True,
                    "status": "SOLVED",
                }
            ],
            "archive": None,
        }

    monkeypatch.setattr(meso_compression, "run_calibration_batch", fake_run_calibration_batch)

    output_root = workspace_tmp / "calibration"
    meso_compression.calibrate_strength_command(
        target_xlsx=target_xlsx,
        reference_summary_xlsx=None,
        params_json=params_path,
        current_summary_xlsx=None,
        current_history_csv=workspace_tmp / "history.csv",
        output_root=output_root,
        case_id=None,
        batch_size=1,
        max_new_attempts=3,
        max_batches=1,
        execute=True,
        dry_run=False,
        force=False,
        batch_id="batch_001",
        archive_root=workspace_tmp / "archive",
        no_archive=True,
    )

    with (output_root / "next_candidates.csv").open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["case_id"] == "13-12-24"
    manifest = json.loads((output_root / "calibration_manifest.json").read_text(encoding="utf-8"))
    assert manifest["plan_phase"] == "post_execution_next_batch"
    assert manifest["last_executed_batch_id"] == "batch_001"


def test_calibration_summary_xlsx_includes_status_and_archive_sheets(workspace_tmp: Path) -> None:
    output_root = workspace_tmp / "calibration"
    archive_index_dir = output_root / "archive_indexes"
    archive_index_dir.mkdir(parents=True)
    archive_index = archive_index_dir / "batch_001_odb_archive_index.csv"
    with archive_index.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "archived_at",
                "batch_id",
                "case_id",
                "attempt_id",
                "angle_deg",
                "g1c",
                "original_path",
                "archive_path",
                "size_bytes",
                "sha256",
                "job_name",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "archived_at": "2026-07-02T10:00:00",
                "batch_id": "batch_001",
                "case_id": "13-12-12",
                "attempt_id": "1",
                "angle_deg": "3.0",
                "g1c": "5.0",
                "original_path": r"D:\work\job.odb",
                "archive_path": r"E:\archive\job.odb",
                "size_bytes": "128",
                "sha256": "a" * 64,
                "job_name": "job",
            }
        )
    plan = {
        "created_at": "2026-07-02T10:00:00",
        "pipeline_version": "test",
        "target_count": 3,
        "active_target_count": 2,
        "simulation_point_count": 2,
        "batch_size": 1,
        "candidate_count": 1,
        "batch_id": "batch_002",
        "plan_phase": "pre_execution",
        "last_executed_batch_id": "batch_001",
        "candidates": [
            {
                "batch_slot": 1,
                "case_id": "13-12-24",
                "attempt_id": 1,
                "angle_deg": 3.0,
                "g1c": 5.0,
                "target_strength_mpa": 200.0,
                "planned_data_role": CURRENT_SIM,
                "reason": "explore",
                "source_inp": "13-inp/13-12-24.inp",
            }
        ],
        "decisions": [
            {
                "status": "accepted",
                "case_id": "13-12-12",
                "target_strength_mpa": 100.0,
                "best_error_pct": 2.0,
                "accepted_source_role": REFERENCE_SIM,
                "accepted_angle_deg": 0.8,
                "accepted_g1c": 5.0,
                "reason": "simulation_within_error_limit",
            },
            {
                "status": "planned",
                "case_id": "13-12-24",
                "target_strength_mpa": 200.0,
                "attempt_id": 1,
                "angle_deg": 3.0,
                "g1c": 5.0,
                "reason": "explore",
            },
            {
                "status": "max_attempts_reached",
                "case_id": "13-12-36",
                "target_strength_mpa": 300.0,
                "current_attempts": 3,
                "best_error_pct": 20.0,
                "reason": "max_new_attempts_reached_without_acceptance",
            },
        ],
    }
    targets = [
        {"data_role": TARGET_ONLY, "case_id": "13-12-12", "excluded": False},
        {"data_role": TARGET_ONLY, "case_id": "13-12-24", "excluded": False},
        {"data_role": TARGET_ONLY, "case_id": "13-24-24", "excluded": True},
    ]
    sim_records = [
        {"data_role": REFERENCE_SIM, "case_id": "13-12-12", "compressive_strength_mpa": 102.0},
        {"data_role": CURRENT_SIM, "case_id": "13-12-24", "compressive_strength_mpa": 180.0},
    ]

    paths = meso_compression.write_calibration_outputs(output_root, plan, targets, sim_records)

    overview = dict(read_xlsx_rows(paths["xlsx"], sheet_name="overview")[1:])
    assert overview["accepted_cases"] == 1
    assert overview["planned_cases"] == 1
    assert overview["max_attempts_reached_cases"] == 1
    assert overview["archive_odb_count"] == 1
    assert overview["archive_total_bytes"] == 128
    status_rows = read_xlsx_rows(paths["xlsx"], sheet_name="status_summary")
    assert ["decision_status", "max_attempts_reached", 1] in status_rows
    assert ["simulation_data_role", CURRENT_SIM, 1] in status_rows
    assert read_xlsx_rows(paths["xlsx"], sheet_name="max_attempts")[1][0] == "13-12-36"
    archive_summary = read_xlsx_rows(paths["xlsx"], sheet_name="archive_summary")
    assert archive_summary[0][-1] == "archive_dir"
    assert archive_summary[1][0] == "batch_001"
    assert archive_summary[1][1] == 1
    assert archive_summary[1][4] == 128

