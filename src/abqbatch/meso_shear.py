"""Hashin meso shear batch workflow."""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from abqbatch.abaqus_cmd import AbaqusCommand
from abqbatch.hashes import sha256_file, sha256_text
from abqbatch.meso_compression import (
    MANIFEST_NAME,
    _append_status,
    _case_by_id,
    _case_state_path,
    _find_step_bounds,
    _format_abaqus_constants,
    _material_contract,
    _material_name_pattern,
    _now,
    _read_json,
    _relative_to_case,
    _remove_driver_boundary_blocks,
    _replace_material_block,
    _write_json,
    collect_status,
    estimate_modulus,
    load_manifest,
    parse_node_print_curve,
    parse_part_bbox,
    read_case_state,
    write_case_state,
    write_xlsx,
)
from abqbatch.meso_compression import (
    datacheck_case as _compression_datacheck_case,
)

app = typer.Typer(help="Run selected-JSON Hashin meso shear batches.")

PIPELINE_VERSION = "meso-shear-v1"
DEFAULT_RUN_DIR = Path("runs/jq_shear_hashin_vf053_beta2p2_pa1_0_35inp")
DEFAULT_SMOKE_CASE = "13-12-24"
DEFAULT_ARCHIVE_ROOT = Path(
    r"E:\ZDYF_NBY_abqbatch_archive\jq_shear_hashin_vf053_beta2p2_pa1_0"
)
DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 7200
MAX_CONSECUTIVE_TIMEOUTS = 3

HEAVY_OUTPUT_PATTERNS = [
    "*.odb",
    "*.sim",
    "*.stt",
    "*.mdl",
    "*.prt",
    "*.res",
    "*.dat",
    "*.msg",
    "*.inp",
    "*.sta",
    "*.com",
    "*.log",
    "*.023",
    "*.lck",
    "*.pac",
    "*.sel",
    "*.fil",
    "*.abq",
]

PROP_ORDER = [
    "E11",
    "E22",
    "G12",
    "G23",
    "NU12",
    "NU23",
    "S1T",
    "S1C",
    "S2T",
    "S2C",
    "S12",
    "S23",
    "G1T",
    "G1C",
    "G2T",
    "G2C",
    "ETA",
]


def _manifest_path(run_dir: Path) -> Path:
    return run_dir / MANIFEST_NAME


def _job_name(case_id: str, candidate_id: str, datacheck: bool = False) -> str:
    safe_case = re.sub(r"[^A-Za-z0-9_]+", "_", case_id)
    safe_candidate = re.sub(r"[^A-Za-z0-9_]+", "_", candidate_id)
    base = f"ms_{safe_case}_{safe_candidate}"
    if datacheck:
        base = f"{base}_dc"
    return base[:78]


def _stable_id(path: Path) -> str:
    return path.stem.replace(" ", "_")



def _read_selected_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))

def _extract_material_block(text: str, material_name: str) -> str:
    lines = text.splitlines()
    pattern = _material_name_pattern(material_name)
    start = next((idx for idx, line in enumerate(lines) if pattern.match(line.strip())), None)
    if start is None:
        raise ValueError(f"Material block not found: {material_name}")

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        if stripped.lower().startswith("*material"):
            end = idx
            break
        if stripped.startswith("*"):
            keyword = stripped[1:].split(",", 1)[0].strip().lower()
            if keyword not in {"depvar", "user material", "density", "elastic", "plastic"}:
                end = idx
                break
    return "\n".join(lines[start:end]).rstrip() + "\n"


def _selected_candidate_id(selected: dict[str, Any]) -> str:
    return str(selected.get("selected_candidate") or selected.get("selected_id") or "shear")


def _resolve_selected_path(selected: dict[str, Any], key: str) -> Path:
    raw = (selected.get("run_paths") or {}).get(key)
    if not raw:
        raise FileNotFoundError(f"selected JSON is missing run_paths.{key}")
    path = Path(raw)
    if not path.exists():
        raise FileNotFoundError(f"selected JSON path does not exist: {path}")
    return path


def _warp_weft_card(material_name: str, constants: dict[str, float]) -> str:
    values = [constants[name] for name in PROP_ORDER]
    return "\n".join(
        [
            f"*Material, name={material_name}",
            "*Depvar",
            "     80,",
            "*User Material, constants=17, unsymm",
            _format_abaqus_constants(values),
        ]
    )


def _warp_weft_constants(selected: dict[str, Any]) -> dict[str, float]:
    constants = selected.get("warp_weft_constants") or {}
    missing = [name for name in PROP_ORDER if name not in constants]
    if missing:
        raise ValueError(f"selected JSON missing warp_weft_constants: {missing}")
    return {name: float(constants[name]) for name in PROP_ORDER}


def _matrix_card_from_selected(selected: dict[str, Any]) -> str:
    source_inp = _resolve_selected_path(selected, "inp_path")
    text = source_inp.read_text(encoding="utf-8", errors="ignore")
    return _extract_material_block(text, "MATRIX")


def _patch_materials(text: str, selected: dict[str, Any]) -> str:
    constants = _warp_weft_constants(selected)
    text = _replace_material_block(text, "MATRIX", _matrix_card_from_selected(selected))
    text = _replace_material_block(text, "WARP", _warp_weft_card("WARP", constants))
    text = _replace_material_block(text, "WEFT", _warp_weft_card("WEFT", constants))
    return text


def patch_shear_step_and_load(
    text: str,
    *,
    step_name: str = "Step-1",
    load_value: float = 0.08,
) -> str:
    lines = text.splitlines()
    step_start, step_end = _find_step_bounds(lines, step_name)
    lines[step_start] = f"*Step, name={step_name}, nlgeom=NO, inc=100000"

    static_idx = next(
        (
            idx
            for idx in range(step_start + 1, step_end)
            if lines[idx].strip().lower().startswith("*static")
        ),
        None,
    )
    static_line = "0.01, 1., 1e-10, 0.01"
    if static_idx is None:
        lines[step_start + 1 : step_start + 1] = ["*Static", static_line]
    else:
        data_idx = static_idx + 1
        while data_idx < step_end and not lines[data_idx].strip():
            data_idx += 1
        if data_idx >= step_end or lines[data_idx].strip().startswith("*"):
            lines.insert(static_idx + 1, static_line)
        else:
            lines[data_idx] = static_line

    step_start, step_end = _find_step_bounds(lines, step_name)
    lines = _remove_driver_boundary_blocks(lines, step_start, step_end)
    step_start, step_end = _find_step_bounds(lines, step_name)
    static_idx = next(
        idx
        for idx in range(step_start + 1, step_end)
        if lines[idx].strip().lower().startswith("*static")
    )
    lines[static_idx + 2 : static_idx + 2] = [
        "** AUTO MESO SHEAR LOAD",
        "*Boundary",
        f'"Constraints Driver Shear_yx", 1, 1, {load_value:.10f}',
    ]
    return "\n".join(lines) + "\n"


def ensure_shear_node_print(text: str, *, step_name: str = "Step-1") -> tuple[str, bool]:
    pattern = re.compile(
        r'^\*Node Print,\s*nset\s*=\s*"?Constraints Driver Shear_yx"?\b',
        re.IGNORECASE | re.MULTILINE,
    )
    if pattern.search(text):
        return text, False

    lines = text.splitlines()
    _, step_end = _find_step_bounds(lines, step_name)
    insert = [
        "**",
        "** DAT NODE PRINT FOR SHEAR DRIVER CURVE",
        '*Node Print, nset="Constraints Driver Shear_yx", frequency=1, summary=NO, totals=YES',
        "U, RF",
    ]
    lines[step_end:step_end] = insert
    return "\n".join(lines) + "\n", True


def patch_inp_text(text: str, selected: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    text = _patch_materials(text, selected)
    text = patch_shear_step_and_load(text)
    text, inserted = ensure_shear_node_print(text)
    return text, {"driver_node_print_inserted": inserted}


def validate_prepared_inp(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    errors: list[str] = []
    contracts = {
        material: _material_contract(text, material)
        for material in ("MATRIX", "WARP", "WEFT")
    }
    expected = {"MATRIX": (11, 12), "WARP": (40, 17), "WEFT": (40, 17)}
    for material, contract in contracts.items():
        if not contract.get("found"):
            errors.append(f"Missing material: {material}")
            continue
        min_depvar, nprops = expected[material]
        if int(contract.get("depvar") or 0) < min_depvar:
            errors.append(f"{material} Depvar below {min_depvar}")
        if contract.get("constants_declared") != nprops:
            errors.append(f"{material} constants declaration is not {nprops}")
        if contract.get("constants_found") != nprops:
            errors.append(f"{material} constants data count is not {nprops}")

    expected_bc = '"Constraints Driver Shear_yx", 1, 1, 0.0800000000'
    if "*Step, name=Step-1, nlgeom=NO, inc=100000" not in text:
        errors.append("Step-1 line was not patched for shear")
    if "0.01, 1., 1e-10, 0.01" not in text:
        errors.append("Static line was not patched for shear")
    if expected_bc not in text:
        errors.append("Shear_yx boundary condition was not patched")
    if '*Node Print, nset="Constraints Driver Shear_yx"' not in text:
        errors.append("DAT shear node print is missing")
    return {
        "ok": not errors,
        "errors": errors,
        "contracts": contracts,
        "checked_at": _now(),
    }


def _copy_env(case_dir: Path) -> str | None:
    source = Path(r"C:\Users\11843\abaqus_v6.env")
    if not source.exists():
        return None
    target = case_dir / "abaqus_v6.env"
    shutil.copy2(source, target)
    return str(target)


def prepare_batch(
    *,
    selected_json: Path,
    input_dir: Path,
    run_dir: Path,
    archive_root: Path = DEFAULT_ARCHIVE_ROOT,
    force: bool = False,
) -> dict[str, Any]:
    selected = _read_selected_json(selected_json)
    if str(selected.get("condition")).lower() != "shear_yx":
        raise ValueError(f"selected JSON condition is not shear_yx: {selected.get('condition')}")

    run_dir = run_dir.resolve()
    input_dir = input_dir.resolve()
    if _manifest_path(run_dir).exists() and not force:
        return load_manifest(run_dir)

    umat_source = _resolve_selected_path(selected, "umat_path")
    source_inps = sorted(input_dir.glob("*.inp"))
    if not source_inps:
        raise FileNotFoundError(f"No .inp files found under {input_dir}")

    candidate_id = _selected_candidate_id(selected)
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw_inp"
    selected_snapshot = run_dir / "selected_parameters_snapshot.json"
    shutil.copy2(selected_json, selected_snapshot)

    cases: list[dict[str, Any]] = []
    for source in source_inps:
        case_id = _stable_id(source)
        case_dir = run_dir / "work" / "cases" / case_id
        input_work_dir = case_dir / "input"
        input_work_dir.mkdir(parents=True, exist_ok=True)

        raw_copy = raw_dir / source.name
        raw_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, raw_copy)

        original = input_work_dir / "original.inp"
        working = input_work_dir / "working.inp"
        umat_working = input_work_dir / umat_source.name
        shutil.copy2(source, original)
        shutil.copy2(umat_source, umat_working)

        patched, patch_meta = patch_inp_text(
            original.read_text(encoding="utf-8", errors="ignore"),
            selected,
        )
        working.write_text(patched, encoding="utf-8", newline="")
        env_copy = _copy_env(case_dir)
        validation = validate_prepared_inp(working)
        _write_json(case_dir / "validation.json", validation)
        lx, ly, lz, volume = parse_part_bbox(working)

        case = {
            "case_id": case_id,
            "source_inp": str(source),
            "source_sha256": sha256_file(source),
            "raw_copy": str(raw_copy),
            "case_dir": str(case_dir),
            "original_inp": str(original),
            "working_inp": str(working),
            "working_inp_sha256": sha256_file(working),
            "umat": str(umat_working),
            "umat_sha256": sha256_file(umat_working),
            "abaqus_env": env_copy,
            "job_name": _job_name(case_id, candidate_id),
            "datacheck_job_name": _job_name(case_id, candidate_id, datacheck=True),
            "curve_csv": str(case_dir / "post" / f"{case_id}_shear_curve.csv"),
            "summary_json": str(case_dir / "post" / f"{case_id}_shear_summary.json"),
            "analysis_json": str(case_dir / "post" / f"{case_id}_analysis.json"),
            "bbox_mm": {"lx": lx, "ly": ly, "lz": lz},
            "volume_mm3": volume,
            "patch": patch_meta,
            "validation": validation,
        }
        cases.append(case)
        write_case_state(
            case,
            {
                "case_id": case_id,
                "status": "PREPARED" if validation["ok"] else "PREPARE_FAILED",
                "validation_ok": validation["ok"],
                "validation_errors": validation["errors"],
                "created_at": _now(),
            },
        )

    settings = {
        "abaqus_cmd": r"C:\Users\11843\codex_abaqus.cmd",
        "cpus": 16,
        "archive_root": str(Path(archive_root)),
        "analysis_timeout_seconds": DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
        "max_consecutive_timeouts": MAX_CONSECUTIVE_TIMEOUTS,
        "modulus_window_start": 0.005,
        "modulus_window_end": 0.015,
        "compare_max_shear_strain": float(
            (selected.get("metrics") or {}).get("compare_max_shear_strain", 0.05)
        ),
        "target_shear_modulus_gpa": (selected.get("metrics") or {}).get(
            "target_shear_modulus_gpa"
        ),
        "target_shear_strength_mpa": (selected.get("metrics") or {}).get(
            "target_shear_strength_mpa"
        ),
        "target_failure_shear_strain": (selected.get("metrics") or {}).get(
            "target_failure_shear_strain"
        ),
    }
    manifest = {
        "pipeline_version": PIPELINE_VERSION,
        "created_at": _now(),
        "run_dir": str(run_dir),
        "selected_json": str(selected_json.resolve()),
        "selected_snapshot": str(selected_snapshot),
        "selected_sha256": sha256_file(selected_json),
        "selected_payload_sha256": sha256_text(json.dumps(selected, sort_keys=True, default=str)),
        "selected_id": selected.get("selected_id"),
        "candidate_id": candidate_id,
        "condition": selected.get("condition"),
        "vf_bundle": selected.get("vf_bundle"),
        "source_input_dir": str(input_dir),
        "case_count": len(cases),
        "settings": settings,
        "warp_weft_constants": _warp_weft_constants(selected),
        "umat_parameters": selected.get("umat_parameters") or {},
        "umat_source": str(umat_source),
        "umat_source_sha256": sha256_file(umat_source),
        "reference_inp": str(_resolve_selected_path(selected, "inp_path")),
        "reference_inp_sha256": sha256_file(_resolve_selected_path(selected, "inp_path")),
        "cases": cases,
    }
    _write_json(_manifest_path(run_dir), manifest)
    _append_status(run_dir, {"event": "prepare", "case_count": len(cases)})
    return manifest


def _abaqus_command(
    manifest: dict[str, Any],
    case: dict[str, Any],
    *,
    datacheck: bool,
) -> AbaqusCommand:
    job = case["datacheck_job_name"] if datacheck else case["job_name"]
    return AbaqusCommand(
        command=manifest["settings"]["abaqus_cmd"],
        job=job,
        input_file=_relative_to_case(case, case["working_inp"]),
        cpus=int(manifest["settings"]["cpus"]),
        user=_relative_to_case(case, case["umat"]),
        interactive=True,
        datacheck=datacheck,
    )



def _archive_index_paths(run_dir: Path) -> tuple[Path, Path]:
    reports = Path(run_dir) / "reports"
    return reports / "archive_index.csv", reports / "archive_index.json"


def _read_archive_rows(run_dir: Path) -> list[dict[str, Any]]:
    _, json_path = _archive_index_paths(run_dir)
    if not json_path.exists():
        return []
    payload = _read_json(json_path)
    return list(payload.get("files", []))


def _write_archive_rows(run_dir: Path, rows: list[dict[str, Any]]) -> None:
    csv_path, json_path = _archive_index_paths(run_dir)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "run_dir",
        "case_id",
        "job_name",
        "reason",
        "source_path",
        "archive_path",
        "extension",
        "bytes",
        "sha256",
        "archived_at",
        "source_removed",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in fieldnames})
    _write_json(
        json_path,
        {
            "run_dir": str(Path(run_dir).resolve()),
            "file_count": len(rows),
            "total_bytes": sum(int(row.get("bytes") or 0) for row in rows),
            "files": rows,
            "updated_at": _now(),
        },
    )


def _heavy_output_files(case_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in HEAVY_OUTPUT_PATTERNS:
        files.extend(path for path in case_dir.glob(pattern) if path.is_file())
    return sorted(set(files), key=lambda item: item.name.lower())



def _collision_archive_path(dest: Path, source_hash: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for index in range(1, 1000):
        if index == 1:
            suffix = f".{stamp}_{source_hash[:8]}"
        else:
            suffix = f".{stamp}_{source_hash[:8]}_{index:03d}"
        candidate = dest.with_name(f"{dest.stem}{suffix}{dest.suffix}")
        if not candidate.exists() or sha256_file(candidate) == source_hash:
            return candidate
    raise RuntimeError(f"Could not allocate archive collision path for {dest}")

def archive_case_outputs(
    manifest: dict[str, Any],
    case: dict[str, Any],
    *,
    reason: str,
    archive_root: Path | None = None,
    remove_source: bool = True,
) -> dict[str, Any]:
    run_dir = Path(manifest["run_dir"])
    case_dir = Path(case["case_dir"])
    archive_root = Path(
        archive_root or manifest["settings"].get("archive_root") or DEFAULT_ARCHIVE_ROOT
    )
    archive_case_dir = archive_root / run_dir.name / case["case_id"]
    archive_case_dir.mkdir(parents=True, exist_ok=True)

    existing_rows = _read_archive_rows(run_dir)
    by_source = {row.get("source_path"): row for row in existing_rows}
    new_rows: list[dict[str, Any]] = []
    for source in _heavy_output_files(case_dir):
        source_hash = sha256_file(source)
        size = source.stat().st_size
        dest = archive_case_dir / source.name
        if dest.exists() and sha256_file(dest) != source_hash:
            dest = _collision_archive_path(dest, source_hash)
        if dest.exists():
            dest_hash = sha256_file(dest)
            if dest_hash != source_hash:
                raise RuntimeError(f"Archive collision with different hash: {dest}")
        else:
            shutil.copy2(source, dest)
            dest_hash = sha256_file(dest)
            if dest_hash != source_hash:
                raise RuntimeError(f"Archive SHA256 mismatch after copy: {source} -> {dest}")
        source_removed = False
        if remove_source:
            source.unlink()
            source_removed = True
        row = {
            "run_dir": str(run_dir.resolve()),
            "case_id": case["case_id"],
            "job_name": case.get("job_name"),
            "reason": reason,
            "source_path": str(source),
            "archive_path": str(dest),
            "extension": source.suffix.lower(),
            "bytes": size,
            "sha256": source_hash,
            "archived_at": _now(),
            "source_removed": source_removed,
        }
        by_source[str(source)] = row
        new_rows.append(row)

    rows = sorted(
        by_source.values(),
        key=lambda item: (str(item.get("case_id")), str(item.get("source_path"))),
    )
    _write_archive_rows(run_dir, rows)
    return {
        "case_id": case["case_id"],
        "reason": reason,
        "archive_root": str(archive_root),
        "archive_case_dir": str(archive_case_dir),
        "archived_count": len(new_rows),
        "archived_bytes": sum(int(row["bytes"]) for row in new_rows),
        "index_csv": str(_archive_index_paths(run_dir)[0]),
        "index_json": str(_archive_index_paths(run_dir)[1]),
        "files": new_rows,
    }


def _terminate_job(abaqus_cmd: str, job_name: str, work_dir: Path) -> dict[str, Any]:
    command = [abaqus_cmd, "terminate", f"job={job_name}"]
    started = _now()
    completed = subprocess.run(command, cwd=work_dir, check=False)
    return {
        "command": command,
        "started_at": started,
        "finished_at": _now(),
        "returncode": int(completed.returncode),
    }


def _run_analysis_subprocess(
    command: AbaqusCommand,
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    terminate: dict[str, Any] | None = None
    timed_out = False
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(command.to_argv(), cwd=cwd, stdout=stdout, stderr=stderr)
        while process.poll() is None:
            if time.monotonic() - started > timeout_seconds:
                timed_out = True
                terminate = _terminate_job(command.command, command.job, cwd)
                break
            time.sleep(10.0)
        if timed_out:
            try:
                process.wait(timeout=900)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            process.wait()
    return {
        "exit_code": int(process.returncode) if process.returncode is not None else None,
        "timed_out": timed_out,
        "terminate": terminate,
        "elapsed_seconds": time.monotonic() - started,
    }
def write_shear_curve_csv(path: str | Path, points: list[tuple[float, float]]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["shear_strain_engineering", "shear_stress_mpa"])
        writer.writerows(points)
    return output


def stress_at(points: list[tuple[float, float]], x_target: float) -> float | None:
    if not points:
        return None
    sorted_points = sorted(points)
    if x_target <= sorted_points[0][0]:
        return sorted_points[0][1]
    for (x0, y0), (x1, y1) in zip(sorted_points, sorted_points[1:], strict=False):
        if x0 <= x_target <= x1:
            if abs(x1 - x0) <= 1.0e-24:
                return y1
            ratio = (x_target - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    return sorted_points[-1][1]


def evaluate_shear_curve(
    points: list[tuple[float, float]],
    *,
    target_shear_modulus_gpa: float | None,
    target_shear_strength_mpa: float | None,
    target_failure_shear_strain: float | None,
    modulus_window_start: float,
    modulus_window_end: float,
    compare_max_shear_strain: float,
) -> dict[str, Any]:
    modulus = estimate_modulus(
        points,
        window_start=modulus_window_start,
        window_end=modulus_window_end,
    )
    compare_points = [
        point for point in points if point[0] <= float(compare_max_shear_strain) + 1.0e-12
    ]
    full_peak = max(points, key=lambda item: item[1]) if points else (None, None)
    compare_peak = max(compare_points, key=lambda item: item[1]) if compare_points else (None, None)
    peak_gamma, peak_tau = compare_peak

    strength_error = (
        None
        if target_shear_strength_mpa is None or peak_tau is None
        else peak_tau - target_shear_strength_mpa
    )
    strength_relative_error = (
        None
        if strength_error is None or target_shear_strength_mpa is None
        else abs(strength_error) / max(abs(target_shear_strength_mpa), 1.0e-12)
    )
    failure_strain_error = (
        None
        if target_failure_shear_strain is None or peak_gamma is None
        else peak_gamma - target_failure_shear_strain
    )
    modulus_gpa = modulus.get("modulus_gpa")
    modulus_relative_error = (
        None
        if modulus_gpa is None or target_shear_modulus_gpa is None
        else abs(float(modulus_gpa) - target_shear_modulus_gpa)
        / max(abs(target_shear_modulus_gpa), 1.0e-12)
    )
    score: float | None = None
    if (
        strength_relative_error is not None
        and failure_strain_error is not None
        and modulus_relative_error is not None
    ):
        score = strength_relative_error + abs(failure_strain_error) / 0.002 + modulus_relative_error

    return {
        "point_count": len(points),
        "target_shear_modulus_gpa": target_shear_modulus_gpa,
        "target_shear_strength_mpa": target_shear_strength_mpa,
        "target_failure_shear_strain": target_failure_shear_strain,
        "modulus_fit": modulus,
        "modulus_gpa": modulus_gpa,
        "modulus_relative_error": modulus_relative_error,
        "peak_within_0p05": {
            "shear_strain": peak_gamma,
            "shear_stress_mpa": peak_tau,
        },
        "peak_full_run": {
            "shear_strain": full_peak[0],
            "shear_stress_mpa": full_peak[1],
        },
        "stress_at_0p05_mpa": stress_at(points, compare_max_shear_strain),
        "strength_error_mpa": strength_error,
        "strength_relative_error": strength_relative_error,
        "failure_strain_error": failure_strain_error,
        "score": score,
        "compare_max_shear_strain": compare_max_shear_strain,
    }


def extract_shear_dat(
    *,
    dat_path: str | Path,
    inp_path: str | Path,
    curve_csv: str | Path,
    summary_json: str | Path,
    settings: dict[str, Any],
) -> dict[str, Any]:
    lx, ly, lz, volume = parse_part_bbox(inp_path)
    points = parse_node_print_curve(
        dat_path,
        ["Constraints Driver Shear_yx"],
        volume,
        strain_multiplier=1.0,
        u_component=1,
        rf_component=1,
    )
    curve_path = str(write_shear_curve_csv(curve_csv, points))
    metrics = evaluate_shear_curve(
        points,
        target_shear_modulus_gpa=_optional_float(settings.get("target_shear_modulus_gpa")),
        target_shear_strength_mpa=_optional_float(settings.get("target_shear_strength_mpa")),
        target_failure_shear_strain=_optional_float(settings.get("target_failure_shear_strain")),
        modulus_window_start=float(settings["modulus_window_start"]),
        modulus_window_end=float(settings["modulus_window_end"]),
        compare_max_shear_strain=float(settings["compare_max_shear_strain"]),
    )
    summary = {
        "dat_path": str(Path(dat_path).resolve()),
        "inp_path": str(Path(inp_path).resolve()),
        "curve_csv": curve_path,
        "bbox_mm": {"lx": lx, "ly": ly, "lz": lz},
        "volume_mm3": volume,
        "curve": {
            "point_count": len(points),
            "last_shear_strain": points[-1][0] if points else None,
            "last_shear_stress_mpa": points[-1][1] if points else None,
        },
        "metrics": metrics,
    }
    if not points:
        summary["warning"] = "No shear driver curve points were extracted from the DAT file."
    _write_json(Path(summary_json), summary)
    return summary


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def analysis_case(
    manifest: dict[str, Any],
    case: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    state = read_case_state(case)
    if state.get("status") == "SOLVED":
        return state
    if state.get("status") != "DATACHECKED":
        state["analysis_skipped_reason"] = "case is not DATACHECKED"
        write_case_state(case, state)
        return state

    case_dir = Path(case["case_dir"])
    command = _abaqus_command(manifest, case, datacheck=False)
    analysis_dir = case_dir / "attempts" / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    command_path = analysis_dir / "command.txt"
    command_path.write_text(command.to_shell_string() + "\n", encoding="utf-8")

    run_result: dict[str, Any]
    if dry_run:
        run_result = {"exit_code": 0, "timed_out": False, "elapsed_seconds": 0.0}
        summary = {
            "curve": {"point_count": 1, "last_shear_strain": 0.08, "last_shear_stress_mpa": 1.0},
            "metrics": {"point_count": 1},
        }
    else:
        run_result = _run_analysis_subprocess(
            command,
            cwd=case_dir,
            stdout_path=analysis_dir / "stdout.log",
            stderr_path=analysis_dir / "stderr.log",
            timeout_seconds=int(
                manifest["settings"].get(
                    "analysis_timeout_seconds", DEFAULT_ANALYSIS_TIMEOUT_SECONDS
                )
            ),
        )
        summary = extract_shear_dat(
            dat_path=case_dir / f"{command.job}.dat",
            inp_path=case["working_inp"],
            curve_csv=case["curve_csv"],
            summary_json=case["summary_json"],
            settings=manifest["settings"],
        )

    payload = {
        "job_name": command.job,
        "command": command.to_shell_string(),
        "command_path": str(command_path),
        **run_result,
        "summary": summary,
        "finished_at": _now(),
    }
    _write_json(Path(case["analysis_json"]), payload)
    point_count = int((summary.get("curve") or {}).get("point_count") or 0)
    if run_result.get("timed_out"):
        status = "ANALYSIS_TIMEOUT"
    elif int(run_result.get("exit_code") or 0) == 0 and point_count > 0:
        status = "SOLVED"
    else:
        status = "ANALYSIS_FAILED"

    state.update(
        {
            "status": status,
            "analysis": payload,
            "metrics": summary.get("metrics", {}),
            "curve": summary.get("curve", {}),
        }
    )
    if not dry_run:
        try:
            state["archive"] = archive_case_outputs(manifest, case, reason=status.lower())
        except Exception as exc:
            state["status"] = "ARCHIVE_FAILED"
            state["archive_error"] = str(exc)
            write_case_state(case, state)
            raise
    write_case_state(case, state)
    return state


def datacheck_case(
    manifest: dict[str, Any],
    case: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    state = read_case_state(case)
    if state.get("status") in {"DATACHECKED", "SOLVED"}:
        return state
    state = _compression_datacheck_case(manifest, case, dry_run=dry_run)
    if state.get("status") == "DATACHECK_FAILED" and not dry_run:
        try:
            state["archive"] = archive_case_outputs(
                manifest, case, reason="datacheck_failed"
            )
        except Exception as exc:
            state["status"] = "ARCHIVE_FAILED"
            state["archive_error"] = str(exc)
            write_case_state(case, state)
            raise
        write_case_state(case, state)
    return state

def datacheck_cases(
    run_dir: Path,
    *,
    case_ids: list[str] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    manifest = load_manifest(run_dir)
    cases = (
        [_case_by_id(manifest, case_id) for case_id in case_ids]
        if case_ids
        else list(manifest["cases"])
    )
    states = []
    for case in cases:
        state = datacheck_case(manifest, case, dry_run=dry_run)
        _append_status(
            run_dir,
            {
                "event": "datacheck",
                "case_id": case["case_id"],
                "status": state.get("status"),
            },
        )
        states.append(state)
    return states


def analysis_cases(
    run_dir: Path,
    *,
    case_ids: list[str] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    manifest = load_manifest(run_dir)
    cases = (
        [_case_by_id(manifest, case_id) for case_id in case_ids]
        if case_ids
        else list(manifest["cases"])
    )
    states = []
    consecutive_timeouts = 0
    max_timeouts = int(
        manifest["settings"].get("max_consecutive_timeouts", MAX_CONSECUTIVE_TIMEOUTS)
    )
    for case in cases:
        state = analysis_case(manifest, case, dry_run=dry_run)
        status = state.get("status")
        _append_status(
            run_dir,
            {
                "event": "analysis",
                "case_id": case["case_id"],
                "status": status,
            },
        )
        states.append(state)
        if status == "ANALYSIS_TIMEOUT":
            consecutive_timeouts += 1
            if not dry_run and consecutive_timeouts >= max_timeouts:
                raise RuntimeError(f"Stopped after {consecutive_timeouts} consecutive timeouts")
        else:
            consecutive_timeouts = 0
    return states


def _summary_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        state = read_case_state(case)
        summary_path = Path(case["summary_json"])
        summary = _read_json(summary_path) if summary_path.exists() else {}
        metrics = summary.get("metrics") or state.get("metrics") or {}
        curve = summary.get("curve") or state.get("curve") or {}
        peak_compare = metrics.get("peak_within_0p05") or {}
        peak_full = metrics.get("peak_full_run") or {}
        analysis = state.get("analysis") or {}
        archive = state.get("archive") or {}
        bbox = case["bbox_mm"]
        rows.append(
            {
                "case_id": case["case_id"],
                "status": state.get("status"),
                "source_inp": case["source_inp"],
                "raw_copy": case["raw_copy"],
                "original_inp": case["original_inp"],
                "working_inp": case["working_inp"],
                "job_name": case["job_name"],
                "datacheck_job_name": case["datacheck_job_name"],
                "datacheck_success": (state.get("datacheck") or {}).get("success"),
                "analysis_returncode": analysis.get("exit_code"),
                "analysis_timed_out": analysis.get("timed_out"),
                "archive_case_dir": archive.get("archive_case_dir"),
                "archive_index_csv": archive.get("index_csv"),
                "archived_count": archive.get("archived_count"),
                "archived_bytes": archive.get("archived_bytes"),
                "archive_error": state.get("archive_error"),
                "bbox_lx_mm": bbox["lx"],
                "bbox_ly_mm": bbox["ly"],
                "bbox_lz_mm": bbox["lz"],
                "volume_mm3": case["volume_mm3"],
                "shear_modulus_gpa": metrics.get("modulus_gpa"),
                "target_shear_modulus_gpa": metrics.get("target_shear_modulus_gpa"),
                "modulus_relative_error": metrics.get("modulus_relative_error"),
                "peak_shear_stress_mpa_within_0p05": peak_compare.get("shear_stress_mpa"),
                "peak_shear_strain_within_0p05": peak_compare.get("shear_strain"),
                "stress_at_0p05_mpa": metrics.get("stress_at_0p05_mpa"),
                "target_shear_strength_mpa": metrics.get("target_shear_strength_mpa"),
                "strength_error_mpa": metrics.get("strength_error_mpa"),
                "strength_relative_error": metrics.get("strength_relative_error"),
                "target_failure_shear_strain": metrics.get("target_failure_shear_strain"),
                "failure_strain_error": metrics.get("failure_strain_error"),
                "peak_shear_stress_mpa_full": peak_full.get("shear_stress_mpa"),
                "peak_shear_strain_full": peak_full.get("shear_strain"),
                "score": metrics.get("score"),
                "point_count": curve.get("point_count") or metrics.get("point_count"),
                "last_shear_strain": curve.get("last_shear_strain"),
                "last_shear_stress_mpa": curve.get("last_shear_stress_mpa"),
                "curve_csv": case["curve_csv"],
                "summary_json": case["summary_json"],
                "state_json": str(_case_state_path(case)),
            }
        )
    return rows


def _failures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("status")) not in {"PREPARED", "DATACHECKED", "SOLVED"}
    ]


def write_results_xlsx(run_dir: Path, output: Path | None = None) -> Path:
    manifest = load_manifest(run_dir)
    rows = _summary_rows(manifest)
    headers = list(rows[0]) if rows else ["case_id", "status"]
    cases_sheet = [headers] + [[row.get(header) for header in headers] for row in rows]
    failures = _failures(rows)
    failures_sheet = [headers] + [[row.get(header) for header in headers] for row in failures]
    provenance = [
        ["key", "value"],
        ["pipeline_version", manifest["pipeline_version"]],
        ["created_at", manifest["created_at"]],
        ["run_dir", manifest["run_dir"]],
        ["selected_json", manifest["selected_json"]],
        ["selected_sha256", manifest["selected_sha256"]],
        ["selected_id", manifest.get("selected_id")],
        ["candidate_id", manifest["candidate_id"]],
        ["condition", manifest.get("condition")],
        ["vf_bundle", manifest.get("vf_bundle")],
        ["source_input_dir", manifest["source_input_dir"]],
        ["case_count", manifest["case_count"]],
        ["umat_source", manifest["umat_source"]],
        ["umat_source_sha256", manifest["umat_source_sha256"]],
        ["reference_inp", manifest["reference_inp"]],
        ["reference_inp_sha256", manifest["reference_inp_sha256"]],
        ["abaqus_cmd", manifest["settings"]["abaqus_cmd"]],
        ["cpus", manifest["settings"]["cpus"]],
        ["compare_max_shear_strain", manifest["settings"]["compare_max_shear_strain"]],
    ]
    output = output or (Path(run_dir) / "reports" / "meso_shear_summary.xlsx")
    return write_xlsx(
        output,
        {
            "cases_summary": cases_sheet,
            "failures": failures_sheet,
            "provenance": provenance,
        },
    )


def archive_existing_outputs(
    run_dir: Path,
    archive_root: Path = DEFAULT_ARCHIVE_ROOT,
) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    archived: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        payload = archive_case_outputs(
            manifest,
            case,
            reason="manual_archive",
            archive_root=archive_root,
        )
        state = read_case_state(case)
        state["archive"] = payload
        write_case_state(case, state)
        archived.append(payload)
    return {
        "run_dir": str(Path(run_dir).resolve()),
        "archive_root": str(archive_root),
        "case_count": len(archived),
        "archived_count": sum(int(item["archived_count"]) for item in archived),
        "archived_bytes": sum(int(item["archived_bytes"]) for item in archived),
        "cases": archived,
        "finished_at": _now(),
    }


def write_combined_results_xlsx(run_dirs: list[Path], output: Path) -> Path:
    rows: list[dict[str, Any]] = []
    provenance: list[list[Any]] = [["key", "value"]]
    for run_dir in run_dirs:
        manifest = load_manifest(run_dir)
        for row in _summary_rows(manifest):
            rows.append({"batch_run": Path(run_dir).name, **row})
        provenance.extend(
            [
                [f"{Path(run_dir).name}:run_dir", str(Path(run_dir).resolve())],
                [f"{Path(run_dir).name}:case_count", manifest.get("case_count")],
                [f"{Path(run_dir).name}:source_input_dir", manifest.get("source_input_dir")],
            ]
        )
    headers = list(rows[0]) if rows else ["batch_run", "case_id", "status"]
    cases_sheet = [headers] + [[row.get(header) for header in headers] for row in rows]
    failures = _failures(rows)
    failures_sheet = [headers] + [[row.get(header) for header in headers] for row in failures]
    return write_xlsx(
        output,
        {
            "cases_summary": cases_sheet,
            "failures": failures_sheet,
            "provenance": provenance,
        },
    )


def write_combined_archive_index(run_dirs: list[Path], output: Path) -> Path:
    rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        for row in _read_archive_rows(run_dir):
            rows.append({"batch_run": Path(run_dir).name, **row})
    output.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        output.write_text("batch_run\n", encoding="utf-8")
        return output
    fieldnames = list(rows[0])
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output


@app.command("prepare")
def prepare_command(
    selected_json: Annotated[Path, typer.Option(help="Selected shear parameter JSON.")],
    input_dir: Annotated[Path, typer.Option(help="Directory of raw INPs.")] = Path("13-inp"),
    run_dir: Annotated[Path, typer.Option(help="Batch run directory.")] = DEFAULT_RUN_DIR,
    archive_root: Annotated[
        Path,
        typer.Option(help="Archive root for Abaqus heavy output files."),
    ] = DEFAULT_ARCHIVE_ROOT,
    force: Annotated[
        bool,
        typer.Option(help="Overwrite manifest and generated files."),
    ] = False,
) -> None:
    """Prepare patched shear cases from a selected parameter JSON."""

    manifest = prepare_batch(
        selected_json=selected_json,
        input_dir=input_dir,
        run_dir=run_dir,
        archive_root=archive_root,
        force=force,
    )
    failed = sum(1 for case in manifest["cases"] if not case["validation"]["ok"])
    typer.echo(f"Prepared {manifest['case_count']} cases in {manifest['run_dir']}")
    if failed:
        typer.echo(f"Validation failed for {failed} cases")
        raise typer.Exit(1)


@app.command("status")
def status_command(
    run_dir: Annotated[Path, typer.Option(help="Batch run directory.")] = DEFAULT_RUN_DIR,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show meso shear batch status."""

    payload = collect_status(run_dir)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    for status, count in sorted(payload["counts"].items()):
        typer.echo(f"{status}: {count}")


@app.command("smoke")
def smoke_command(
    run_dir: Annotated[Path, typer.Option(help="Batch run directory.")] = DEFAULT_RUN_DIR,
    case_id: Annotated[str, typer.Option("--case", help="Smoke case id.")] = DEFAULT_SMOKE_CASE,
    dry_run: Annotated[
        bool,
        typer.Option(help="Record command without Abaqus."),
    ] = False,
) -> None:
    """Run one smoke datacheck."""

    states = datacheck_cases(run_dir, case_ids=[case_id], dry_run=dry_run)
    status = states[0].get("status")
    typer.echo(f"Smoke datacheck {case_id}: {status}")
    if status != "DATACHECKED":
        raise typer.Exit(1)


@app.command("datacheck")
def datacheck_command(
    run_dir: Annotated[Path, typer.Option(help="Batch run directory.")] = DEFAULT_RUN_DIR,
    dry_run: Annotated[
        bool,
        typer.Option(help="Record commands without Abaqus."),
    ] = False,
) -> None:
    """Run datacheck for all prepared shear cases."""

    states = datacheck_cases(run_dir, dry_run=dry_run)
    failed = sum(1 for state in states if state.get("status") != "DATACHECKED")
    typer.echo(f"Datacheck processed {len(states)} cases, failed={failed}")


@app.command("run")
def run_command(
    run_dir: Annotated[Path, typer.Option(help="Batch run directory.")] = DEFAULT_RUN_DIR,
    dry_run: Annotated[
        bool,
        typer.Option(help="Record commands without Abaqus."),
    ] = False,
) -> None:
    """Run all datachecked shear cases serially."""

    states = analysis_cases(run_dir, dry_run=dry_run)
    failed = sum(1 for state in states if state.get("status") != "SOLVED")
    typer.echo(f"Analysis processed {len(states)} cases, failed={failed}")


@app.command("report-xlsx")
def report_xlsx_command(
    run_dir: Annotated[Path, typer.Option(help="Batch run directory.")] = DEFAULT_RUN_DIR,
    output: Annotated[
        Path | None,
        typer.Option(help="Output .xlsx path."),
    ] = None,
) -> None:
    """Write an XLSX summary of shear scalar metrics and curve paths."""

    path = write_results_xlsx(run_dir, output=output)
    typer.echo(str(path))



@app.command("archive-heavy")
def archive_heavy_command(
    run_dir: Annotated[Path, typer.Option(help="Batch run directory.")] = DEFAULT_RUN_DIR,
    archive_root: Annotated[
        Path,
        typer.Option(help="Archive root for Abaqus heavy output files."),
    ] = DEFAULT_ARCHIVE_ROOT,
) -> None:
    """Move existing Abaqus heavy files to E/archive with SHA256 verification."""

    payload = archive_existing_outputs(run_dir, archive_root=archive_root)
    typer.echo(
        f"Archived {payload['archived_count']} files, "
        f"{payload['archived_bytes'] / (1024 * 1024):.1f} MiB"
    )


@app.command("report-all-xlsx")
def report_all_xlsx_command(
    run_dir: Annotated[
        list[Path],
        typer.Option("--run-dir", help="Batch run directory. Repeat for multiple runs."),
    ],
    output: Annotated[Path, typer.Option(help="Output combined .xlsx path.")],
    archive_index_output: Annotated[
        Path | None,
        typer.Option(help="Output combined archive CSV path."),
    ] = None,
) -> None:
    """Write a combined XLSX summary and optional archive index for multiple runs."""

    path = write_combined_results_xlsx(run_dir, output)
    typer.echo(str(path))
    if archive_index_output is not None:
        typer.echo(str(write_combined_archive_index(run_dir, archive_index_output)))
