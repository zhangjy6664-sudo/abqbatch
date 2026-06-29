"""Puck-ZT meso compression batch workflow."""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import subprocess
import time
import zipfile
from collections.abc import Iterable
from datetime import datetime
from html import escape as xml_escape
from pathlib import Path
from typing import Annotated, Any

import typer

from abqbatch.abaqus_cmd import AbaqusCommand
from abqbatch.hashes import sha256_file, sha256_text
from abqbatch.monitor import detect_success

app = typer.Typer(help="Run JSON-driven Puck-ZT meso compression batches.")

PIPELINE_VERSION = "meso-compression-v1"
DEFAULT_RUN_DIR = Path("runs/puckzt_angle_0p8_g1c5_compression_35inp")
DEFAULT_SMOKE_CASE = "13-12-24"
MANIFEST_NAME = "manifest.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _resolve_project_path(params: dict[str, Any], raw_path: str | Path | None) -> Path | None:
    if raw_path is None:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    project_root = Path(params["project_root"])
    return project_root / path


def _stable_id(path: Path) -> str:
    return path.stem.replace(" ", "_")


def _job_name(case_id: str, candidate_id: str, datacheck: bool = False) -> str:
    cid = re.sub(r"[^A-Za-z0-9_]+", "_", candidate_id)
    base = f"mc_{case_id.replace('-', '_')}_{cid}"
    if datacheck:
        base = f"{base}_dc"
    return base[:78]


def _format_float(value: float) -> str:
    return f"{float(value):.10g}"


def _format_abaqus_constants(values: Iterable[float], per_line: int = 8) -> str:
    tokens = [_format_float(float(value)) for value in values]
    lines: list[str] = []
    for index in range(0, len(tokens), per_line):
        chunk = tokens[index : index + per_line]
        lines.append(", ".join(chunk) + ",")
    return "\n".join(lines)


def _material_card(material_name: str, material: dict[str, Any]) -> str:
    constants = material["constants"]
    values = [constants[name] for name in material["constants_order"]]
    return "\n".join(
        [
            f"*Material, name={material_name}",
            "*Depvar",
            f"     {int(material['depvar'])},",
            f"*User Material, constants={len(values)}",
            _format_abaqus_constants(values),
        ]
    )


def _material_name_pattern(material_name: str) -> re.Pattern[str]:
    return re.compile(
        rf'^\*Material,\s*name\s*=\s*"?{re.escape(material_name)}"?\s*$',
        re.IGNORECASE,
    )


def _replace_material_block(text: str, material_name: str, replacement: str) -> str:
    lines = text.splitlines()
    pattern = _material_name_pattern(material_name)
    start = next((idx for idx, line in enumerate(lines) if pattern.match(line.strip())), None)
    if start is None:
        raise ValueError(f"Material block not found: {material_name}")

    allowed_subkeywords = {"depvar", "user material", "density", "elastic", "plastic"}
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
            if keyword not in allowed_subkeywords:
                end = idx
                break

    new_lines = lines[:start] + replacement.rstrip("\n").splitlines() + lines[end:]
    return "\n".join(new_lines) + "\n"


def build_material_cards(params: dict[str, Any]) -> dict[str, str]:
    materials = params["materials"]
    return {
        name: _material_card(name, materials[name])
        for name in ("MATRIX", "WARP", "WEFT")
    }


def patch_materials(text: str, params: dict[str, Any]) -> str:
    for material_name, card in build_material_cards(params).items():
        text = _replace_material_block(text, material_name, card)
    return text


def _find_step_bounds(lines: list[str], step_name: str) -> tuple[int, int]:
    step_pattern = re.compile(
        rf"^\*Step,\s*name\s*=\s*{re.escape(step_name)}(?:\s*,.*)?$",
        re.IGNORECASE,
    )
    step_start = next(
        (idx for idx, line in enumerate(lines) if step_pattern.match(line.strip())),
        None,
    )
    if step_start is None:
        raise ValueError(f"Step not found: {step_name}")
    step_end = next(
        (
            idx
            for idx in range(step_start + 1, len(lines))
            if lines[idx].strip().lower().startswith("*end step")
        ),
        None,
    )
    if step_end is None:
        raise ValueError(f"Step has no *End Step: {step_name}")
    return step_start, step_end


def _is_driver_boundary_block(block_lines: list[str]) -> bool:
    return any(line.strip().startswith('"Constraints Driver') for line in block_lines)


def _remove_driver_boundary_blocks(lines: list[str], step_start: int, step_end: int) -> list[str]:
    patched: list[str] = []
    idx = 0
    while idx < len(lines):
        if step_start <= idx < step_end and lines[idx].strip().lower().startswith("*boundary"):
            block_end = idx + 1
            while block_end < step_end and not lines[block_end].strip().startswith("*"):
                block_end += 1
            block_lines = lines[idx:block_end]
            if _is_driver_boundary_block(block_lines):
                idx = block_end
                continue
        patched.append(lines[idx])
        idx += 1
    return patched


def patch_step_and_load(text: str, params: dict[str, Any]) -> str:
    settings = params["step_and_load"]
    step_name = settings["name"]
    static = settings["static"]
    compression_bc = settings["compression_bc"]
    node_set = compression_bc["node_set"]
    value = float(compression_bc["value"])
    lines = text.splitlines()
    step_start, step_end = _find_step_bounds(lines, step_name)

    lines[step_start] = (
        f"*Step, name={step_name}, nlgeom={settings['nlgeom']}, "
        f"inc={int(settings['max_increments'])}"
    )

    static_idx = next(
        (
            idx
            for idx in range(step_start + 1, step_end)
            if lines[idx].strip().lower().startswith("*static")
        ),
        None,
    )
    static_line = (
        f"{_format_float(static['initial'])}, 1., "
        f"{_format_float(static['minimum'])}, {_format_float(static['maximum'])}"
    )
    if static_idx is None:
        insert_at = step_start + 1
        lines[insert_at:insert_at] = ["*Static", static_line]
        static_idx = insert_at
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
    insert_at = static_idx + 2
    lines[insert_at:insert_at] = [
        "** AUTO MESO COMPRESSION LOAD",
        "*Boundary",
        f'"{node_set}", {int(compression_bc["dof_start"])}, '
        f'{int(compression_bc["dof_end"])}, {value:.10f}',
    ]
    return "\n".join(lines) + "\n"


def ensure_driver_node_print(text: str, params: dict[str, Any]) -> tuple[str, bool]:
    node_print = params["outputs_and_postprocess"]["dat_node_print"]
    node_set = node_print["node_set"]
    lower = text.lower()
    if "*node print" in lower and node_set.lower() in lower:
        return text, False
    lines = text.splitlines()
    _, step_end = _find_step_bounds(lines, params["step_and_load"]["name"])
    variables = ", ".join(node_print["variables"])
    insert = [
        "**",
        "** DAT NODE PRINT FOR EARLY-STOP DRIVER CURVE",
        (
            f'*Node Print, nset="{node_set}", '
            f'frequency={int(node_print["frequency"])}, totals={node_print["totals"]}'
        ),
        variables,
    ]
    lines[step_end:step_end] = insert
    return "\n".join(lines) + "\n", True


def patch_inp_text(text: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    text = patch_materials(text, params)
    text = patch_step_and_load(text, params)
    text, node_print_inserted = ensure_driver_node_print(text, params)
    return text, {"driver_node_print_inserted": node_print_inserted}


def _material_contract(text: str, material_name: str) -> dict[str, Any]:
    lines = text.splitlines()
    pattern = _material_name_pattern(material_name)
    start = next((idx for idx, line in enumerate(lines) if pattern.match(line.strip())), None)
    if start is None:
        return {"found": False}
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.lower().startswith("*material"):
            end = idx
            break
        if stripped.startswith("*") and not re.match(
            r"^\*(Depvar|User Material)\b", stripped, re.IGNORECASE
        ):
            end = idx
            break
    block = lines[start:end]
    depvar: int | None = None
    constants_declared: int | None = None
    values: list[float] = []
    for idx, line in enumerate(block):
        if line.strip().lower().startswith("*depvar") and idx + 1 < len(block):
            found = re.findall(r"[-+]?\d+", block[idx + 1])
            depvar = int(found[0]) if found else None
        user_match = re.match(
            r"^\*User Material,\s*constants\s*=\s*(\d+)",
            line.strip(),
            re.IGNORECASE,
        )
        if user_match:
            constants_declared = int(user_match.group(1))
            for data in block[idx + 1 :]:
                if data.strip().startswith("*"):
                    break
                for token in data.replace(",", " ").split():
                    values.append(float(token))
            break
    return {
        "found": True,
        "depvar": depvar,
        "constants_declared": constants_declared,
        "constants_found": len(values),
    }


def validate_prepared_inp(path: Path, params: dict[str, Any]) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    errors: list[str] = []
    contracts = {
        material: _material_contract(text, material)
        for material in ("MATRIX", "WARP", "WEFT")
    }
    expected = {
        "MATRIX": (
            int(params["umat"]["matrix_branch_requirement"]["NSTATV_min"]),
            int(params["umat"]["matrix_branch_requirement"]["NPROPS_min"]),
        ),
        "WARP": (
            int(params["umat"]["fiber_branch_requirement"]["NSTATV_min"]),
            int(params["umat"]["fiber_branch_requirement"]["NPROPS_min"]),
        ),
        "WEFT": (
            int(params["umat"]["fiber_branch_requirement"]["NSTATV_min"]),
            int(params["umat"]["fiber_branch_requirement"]["NPROPS_min"]),
        ),
    }
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

    settings = params["step_and_load"]
    expected_step = (
        f"*Step, name={settings['name']}, nlgeom={settings['nlgeom']}, "
        f"inc={int(settings['max_increments'])}"
    )
    if expected_step not in text:
        errors.append("Step-1 line was not patched")
    static = settings["static"]
    expected_static = (
        f"{_format_float(static['initial'])}, 1., "
        f"{_format_float(static['minimum'])}, {_format_float(static['maximum'])}"
    )
    if expected_static not in text:
        errors.append("Static line was not patched")
    bc = settings["compression_bc"]
    expected_bc = (
        f'"{bc["node_set"]}", {int(bc["dof_start"])}, '
        f'{int(bc["dof_end"])}, {float(bc["value"]):.10f}'
    )
    if expected_bc not in text:
        errors.append("Compression boundary condition was not patched")
    if "*Node Print" not in text or bc["node_set"] not in text:
        errors.append("DAT node print is missing")
    return {
        "ok": not errors,
        "errors": errors,
        "contracts": contracts,
        "checked_at": _now(),
    }


def parse_part_bbox(inp_path: str | Path) -> tuple[float, float, float, float]:
    in_part = False
    in_node_block = False
    coords: list[tuple[float, float, float]] = []
    for raw_line in Path(inp_path).read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = raw_line.strip()
        upper = stripped.upper()
        if upper.startswith("*PART,"):
            in_part = True
            in_node_block = False
            continue
        if not in_part:
            continue
        if upper == "*NODE":
            in_node_block = True
            continue
        if in_node_block and stripped.startswith("*"):
            break
        if in_node_block and stripped:
            parts = [item.strip() for item in stripped.split(",")]
            if len(parts) >= 4:
                try:
                    coords.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    continue
    if not coords:
        raise RuntimeError(f"Could not parse part node coordinates from {inp_path}")
    xs = [item[0] for item in coords]
    ys = [item[1] for item in coords]
    zs = [item[2] for item in coords]
    lx = max(xs) - min(xs)
    ly = max(ys) - min(ys)
    lz = max(zs) - min(zs)
    return lx, ly, lz, lx * ly * lz


def _normalize_fragment(fragment: str) -> str:
    return re.sub(r"\s+", " ", fragment.strip().upper())


def parse_node_print_curve(
    dat_path: str | Path,
    marker_fragments: Iterable[str],
    stress_scale: float,
    *,
    strain_multiplier: float = 1.0,
    u_component: int = 1,
    rf_component: int = 1,
) -> list[tuple[float, float]]:
    dat_path = Path(dat_path)
    if not dat_path.exists():
        return []
    fragments = [_normalize_fragment(item) for item in marker_fragments]
    float_regex = re.compile(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?")
    u_index = {1: 1, 2: 2, 3: 3}[int(u_component)]
    rf_index = {1: 4, 2: 5, 3: 6}[int(rf_component)]
    total_u_index = {1: 0, 2: 1, 3: 2}[int(u_component)]
    total_rf_index = {1: 3, 2: 4, 3: 5}[int(rf_component)]
    points: list[tuple[float, float]] = []
    awaiting_numeric = False
    for raw_line in dat_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = _normalize_fragment(line)
        if (
            "THE FOLLOWING TABLE IS PRINTED FOR NODES BELONGING TO NODE SET" in upper
            and any(fragment in upper for fragment in fragments)
        ):
            awaiting_numeric = True
            continue
        if not awaiting_numeric:
            continue
        if line.startswith("*") or ("RF" in upper and "U" in upper):
            continue
        if "MAXIMUM" in upper or "MINIMUM" in upper or "NODE FOOT" in upper:
            continue
        if upper.startswith("TOTAL"):
            values = [float(token) for token in float_regex.findall(line)]
            if len(values) >= 6:
                epsilon = abs(values[total_u_index]) * float(strain_multiplier)
                sigma = abs(values[total_rf_index]) / float(stress_scale)
                points.append((epsilon, sigma))
                awaiting_numeric = False
            continue
        if not re.match(r"^\d+\s", line):
            continue
        values = [float(token) for token in float_regex.findall(line)]
        if len(values) >= 7:
            epsilon = abs(values[u_index]) * float(strain_multiplier)
            sigma = abs(values[rf_index]) / float(stress_scale)
            points.append((epsilon, sigma))
            awaiting_numeric = False

    deduped: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for epsilon, sigma in points:
        key = (round(epsilon, 12), round(sigma, 12))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((epsilon, sigma))
    deduped.sort(key=lambda item: item[0])
    return deduped


def evaluate_curve(points: list[tuple[float, float]]) -> dict[str, Any]:
    evaluation: dict[str, Any] = {
        "point_count": len(points),
        "strength_mpa": None,
        "failure_strain_engineering": None,
        "peak_index": None,
        "post_peak_count": 0,
        "last_strain_engineering": None,
        "last_stress_mpa": None,
        "drop_ratio": None,
    }
    if not points:
        return evaluation
    sigmas = [item[1] for item in points]
    peak_sigma = max(sigmas)
    peak_index = sigmas.index(peak_sigma)
    evaluation.update(
        {
            "strength_mpa": peak_sigma,
            "failure_strain_engineering": points[peak_index][0],
            "peak_index": peak_index,
            "post_peak_count": len(points) - peak_index - 1,
            "last_strain_engineering": points[-1][0],
            "last_stress_mpa": points[-1][1],
            "drop_ratio": points[-1][1] / peak_sigma if peak_sigma > 0.0 else None,
        }
    )
    return evaluation


def _linear_regression_slope(
    points: list[tuple[float, float]],
) -> tuple[float | None, float | None, float | None]:
    if len(points) < 2:
        return None, None, None
    xs = [item[0] for item in points]
    ys = [item[1] for item in points]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom <= 1.0e-24:
        return None, None, None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=False)) / denom
    intercept = y_mean - slope * x_mean
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=False))
    r2 = 1.0 if ss_tot <= 1.0e-24 else 1.0 - ss_res / ss_tot
    return slope, intercept, r2


def estimate_modulus(
    points: list[tuple[float, float]],
    *,
    window_start: float,
    window_end: float,
) -> dict[str, Any]:
    selected = [point for point in points if window_start <= point[0] <= window_end]
    method = "linear_regression_window"
    if len(selected) < 2:
        selected = [point for point in points if point[0] > 1.0e-12][: min(8, len(points))]
        method = "fallback_origin_slope_early_points"
        denom = sum(point[0] * point[0] for point in selected)
        if len(selected) < 2 or denom <= 1.0e-24:
            return {
                "method": method,
                "point_count": len(selected),
                "modulus_mpa": None,
                "modulus_gpa": None,
                "intercept_mpa": None,
                "r2": None,
                "window": {"start": window_start, "end": window_end},
            }
        slope = sum(point[0] * point[1] for point in selected) / denom
        return {
            "method": method,
            "point_count": len(selected),
            "modulus_mpa": slope,
            "modulus_gpa": slope / 1000.0,
            "intercept_mpa": 0.0,
            "r2": None,
            "window": {"start": window_start, "end": window_end},
        }
    slope, intercept, r2 = _linear_regression_slope(selected)
    return {
        "method": method,
        "point_count": len(selected),
        "modulus_mpa": slope,
        "modulus_gpa": slope / 1000.0 if slope is not None else None,
        "intercept_mpa": intercept,
        "r2": r2,
        "window": {"start": window_start, "end": window_end},
    }


def write_curve_csv(path: str | Path, points: list[tuple[float, float]]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["strain_engineering", "stress_mpa"])
        writer.writerows(points)
    return output


def extract_compression_dat(
    *,
    dat_path: str | Path,
    inp_path: str | Path,
    curve_csv: str | Path,
    summary_json: str | Path,
    modulus_window_start: float,
    modulus_window_end: float,
) -> dict[str, Any]:
    lx, ly, lz, volume = parse_part_bbox(inp_path)
    points = parse_node_print_curve(
        dat_path,
        ["Constraints Driver Fx"],
        volume,
        strain_multiplier=1.0,
        u_component=1,
        rf_component=1,
    )
    curve_path = str(write_curve_csv(curve_csv, points))
    curve_eval = evaluate_curve(points)
    modulus = estimate_modulus(
        points,
        window_start=modulus_window_start,
        window_end=modulus_window_end,
    )
    summary = {
        "dat_path": str(Path(dat_path).resolve()),
        "inp_path": str(Path(inp_path).resolve()),
        "curve_csv": curve_path,
        "bbox_mm": {"lx": lx, "ly": ly, "lz": lz},
        "volume_mm3": volume,
        "curve": curve_eval,
        "modulus": modulus,
        "metrics": {
            "modulus_gpa": modulus["modulus_gpa"],
            "compressive_strength_mpa": curve_eval["strength_mpa"],
            "peak_compression_strain_engineering": curve_eval["failure_strain_engineering"],
        },
    }
    if not points:
        summary["warning"] = "No driver curve points were extracted from the DAT file."
    _write_json(Path(summary_json), summary)
    return summary


def _manifest_path(run_dir: Path) -> Path:
    return run_dir / MANIFEST_NAME


def load_manifest(run_dir: Path) -> dict[str, Any]:
    path = _manifest_path(run_dir)
    if not path.exists():
        raise FileNotFoundError(f"Missing manifest: {path}")
    return _read_json(path)


def _write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    _write_json(_manifest_path(run_dir), manifest)


def _case_state_path(case: dict[str, Any]) -> Path:
    return Path(case["case_dir"]) / "state.json"


def read_case_state(case: dict[str, Any]) -> dict[str, Any]:
    path = _case_state_path(case)
    if not path.exists():
        return {"case_id": case["case_id"], "status": "MISSING"}
    return _read_json(path)


def write_case_state(case: dict[str, Any], state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _write_json(_case_state_path(case), state)


def _append_status(run_dir: Path, payload: dict[str, Any]) -> None:
    path = run_dir / "status.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"time": _now(), **payload}, ensure_ascii=False) + "\n")


def _case_by_id(manifest: dict[str, Any], case_id: str) -> dict[str, Any]:
    normalized = case_id[:-4] if case_id.lower().endswith(".inp") else case_id
    for case in manifest["cases"]:
        if case["case_id"] == normalized:
            return case
    raise KeyError(f"Unknown case: {case_id}")


def _copy_optional_env(params: dict[str, Any], case_dir: Path) -> str | None:
    working_inp = _resolve_project_path(params, params.get("model", {}).get("working_inp"))
    candidates = []
    if working_inp is not None:
        candidates.append(working_inp.parent / "abaqus_v6.env")
    candidates.append(Path(r"C:\Users\11843\abaqus_v6.env"))
    for candidate in candidates:
        if candidate.exists():
            target = case_dir / "abaqus_v6.env"
            shutil.copy2(candidate, target)
            return str(target)
    return None


def prepare_batch(
    *,
    params_json: Path,
    input_dir: Path,
    run_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    params = _read_json(params_json)
    run_dir = run_dir.resolve()
    input_dir = input_dir.resolve()
    if _manifest_path(run_dir).exists() and not force:
        raise FileExistsError(f"Manifest already exists: {_manifest_path(run_dir)}")
    source_umats = [
        _resolve_project_path(params, params.get("umat", {}).get("working_copy")),
        _resolve_project_path(params, params.get("umat", {}).get("source")),
    ]
    umat_source = next((path for path in source_umats if path is not None and path.exists()), None)
    if umat_source is None:
        raise FileNotFoundError("Could not resolve UMAT working_copy or source from params JSON")

    source_inps = sorted(input_dir.glob("*.inp"))
    if not source_inps:
        raise FileNotFoundError(f"No .inp files found under {input_dir}")

    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw_inp"
    cases: list[dict[str, Any]] = []
    params_snapshot = run_dir / "params_snapshot.json"
    shutil.copy2(params_json, params_snapshot)
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
        text = original.read_text(encoding="utf-8", errors="ignore")
        patched, patch_meta = patch_inp_text(text, params)
        working.write_text(patched, encoding="utf-8", newline="")
        env_copy = _copy_optional_env(params, case_dir)
        validation = validate_prepared_inp(working, params)
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
            "job_name": _job_name(case_id, params["candidate_id"]),
            "datacheck_job_name": _job_name(case_id, params["candidate_id"], datacheck=True),
            "curve_csv": str(case_dir / "post" / f"{case_id}_curve.csv"),
            "summary_json": str(case_dir / "post" / f"{case_id}_summary.json"),
            "early_stop_json": str(case_dir / "post" / f"{case_id}_early_stop.json"),
            "bbox_mm": {"lx": lx, "ly": ly, "lz": lz},
            "volume_mm3": volume,
            "patch": patch_meta,
            "validation": validation,
        }
        cases.append(case)
        initial_status = "PREPARED" if validation["ok"] else "PREPARE_FAILED"
        write_case_state(
            case,
            {
                "case_id": case_id,
                "status": initial_status,
                "validation_ok": validation["ok"],
                "validation_errors": validation["errors"],
                "created_at": _now(),
            },
        )

    early_stop = params["early_stop_and_job"]
    modulus_window = params["outputs_and_postprocess"]["modulus_window_strain"]
    manifest = {
        "pipeline_version": PIPELINE_VERSION,
        "created_at": _now(),
        "run_dir": str(run_dir),
        "params_json": str(params_json.resolve()),
        "params_snapshot": str(params_snapshot),
        "params_sha256": sha256_file(params_json),
        "params_payload_sha256": sha256_text(json.dumps(params, sort_keys=True, default=str)),
        "candidate_id": params["candidate_id"],
        "purpose": params.get("purpose"),
        "source_input_dir": str(input_dir),
        "case_count": len(cases),
        "settings": {
            "abaqus_cmd": early_stop.get("abaqus_cmd", r"C:\Users\11843\codex_abaqus.cmd"),
            "cpus": int(early_stop.get("cpus", 16)),
            "drop_fraction": float(early_stop["drop_fraction"]),
            "poll_seconds": float(early_stop["poll_seconds"]),
            "min_points": int(early_stop["min_points"]),
            "min_peak_mpa": float(early_stop["min_peak_mpa"]),
            "modulus_window_start": float(modulus_window["start"]),
            "modulus_window_end": float(modulus_window["end"]),
        },
        "materials": {
            name: {
                "depvar": params["materials"][name]["depvar"],
                "constants_order": params["materials"][name]["constants_order"],
                "constants": params["materials"][name]["constants"],
            }
            for name in ("MATRIX", "WARP", "WEFT")
        },
        "umat_source": str(umat_source),
        "umat_source_sha256": sha256_file(umat_source),
        "cases": cases,
    }
    _write_manifest(run_dir, manifest)
    _append_status(run_dir, {"event": "prepare", "case_count": len(cases)})
    return manifest


def _relative_to_case(case: dict[str, Any], path: str | Path) -> Path:
    return Path(path).resolve().relative_to(Path(case["case_dir"]).resolve())


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


def _run_subprocess(
    argv: list[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int | None = None,
) -> int:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
        )
    return int(completed.returncode)


def datacheck_case(
    manifest: dict[str, Any],
    case: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    state = read_case_state(case)
    if not state.get("validation_ok", True):
        state["status"] = "PREPARE_FAILED"
        write_case_state(case, state)
        return state
    case_dir = Path(case["case_dir"])
    command = _abaqus_command(manifest, case, datacheck=True)
    datacheck_dir = case_dir / "attempts" / "datacheck"
    datacheck_dir.mkdir(parents=True, exist_ok=True)
    command_path = datacheck_dir / "command.txt"
    command_path.write_text(command.to_shell_string() + "\n", encoding="utf-8")
    if dry_run:
        exit_code = 0
        success = True
    else:
        exit_code = _run_subprocess(
            command.to_argv(),
            cwd=case_dir,
            stdout_path=datacheck_dir / "stdout.log",
            stderr_path=datacheck_dir / "stderr.log",
        )
        success = exit_code == 0 and detect_success(case_dir, command.job)
    state.update(
        {
            "status": "DATACHECKED" if success else "DATACHECK_FAILED",
            "datacheck": {
                "job_name": command.job,
                "command": command.to_shell_string(),
                "command_path": str(command_path),
                "exit_code": exit_code,
                "success": success,
                "finished_at": _now(),
            },
        }
    )
    write_case_state(case, state)
    return state


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


def _monitor_curve_and_terminate(
    *,
    process: subprocess.Popen[Any],
    abaqus_cmd: str,
    job_name: str,
    work_dir: Path,
    inp_path: Path,
    dat_path: Path,
    poll_seconds: float,
    drop_fraction: float,
    min_points: int,
    min_peak_mpa: float,
) -> dict[str, Any]:
    _, _, _, volume = parse_part_bbox(inp_path)
    peak_stress = -math.inf
    peak_strain: float | None = None
    latest_count = 0
    termination: dict[str, Any] | None = None
    last_curve: dict[str, Any] = {}
    started_monotonic = time.monotonic()
    while True:
        points = parse_node_print_curve(
            dat_path,
            ["Constraints Driver Fx"],
            volume,
            strain_multiplier=1.0,
            u_component=1,
            rf_component=1,
        )
        if points:
            curve_eval = evaluate_curve(points)
            current_strain, current_stress = points[-1]
            if (
                curve_eval["strength_mpa"] is not None
                and float(curve_eval["strength_mpa"]) > peak_stress
            ):
                peak_stress = float(curve_eval["strength_mpa"])
                peak_strain = float(curve_eval["failure_strain_engineering"])
            latest_count = len(points)
            last_curve = {
                "point_count": latest_count,
                "current_strain_engineering": current_strain,
                "current_stress_mpa": current_stress,
                "peak_stress_mpa": peak_stress if math.isfinite(peak_stress) else None,
                "peak_strain_engineering": peak_strain,
                "drop_ratio": current_stress / peak_stress if peak_stress > 0.0 else None,
                "curve": curve_eval,
            }
            post_peak_count = latest_count - int(curve_eval.get("peak_index") or 0) - 1
            if (
                latest_count >= int(min_points)
                and peak_stress >= float(min_peak_mpa)
                and post_peak_count >= 1
                and current_stress <= peak_stress * (1.0 - float(drop_fraction))
            ):
                termination = {
                    "reason": "stress_drop_threshold",
                    "drop_fraction": drop_fraction,
                    "threshold_stress_mpa": peak_stress * (1.0 - float(drop_fraction)),
                    **last_curve,
                }
                termination["terminate_command"] = _terminate_job(abaqus_cmd, job_name, work_dir)
                break
        if process.poll() is not None:
            break
        time.sleep(float(poll_seconds))

    try:
        process.wait(timeout=900)
    except subprocess.TimeoutExpired:
        process.kill()
        termination = termination or {"reason": "process_wait_timeout"}
        termination["process_killed_after_terminate_timeout"] = True
    return {
        "terminated_by_monitor": termination is not None,
        "termination": termination,
        "analysis_returncode": process.returncode,
        "latest_curve": last_curve,
        "monitor_elapsed_seconds": time.monotonic() - started_monotonic,
        "monitor_finished_at": _now(),
        "poll_seconds": poll_seconds,
    }


def analysis_case(
    manifest: dict[str, Any],
    case: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    state = read_case_state(case)
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
    if dry_run:
        state.update(
            {
                "status": "SOLVED",
                "analysis": {
                    "job_name": command.job,
                    "command": command.to_shell_string(),
                    "command_path": str(command_path),
                    "exit_code": 0,
                    "dry_run": True,
                },
            }
        )
        write_case_state(case, state)
        return state

    stdout_path = analysis_dir / "stdout.log"
    stderr_path = analysis_dir / "stderr.log"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            command.to_argv(),
            cwd=case_dir,
            stdout=stdout,
            stderr=stderr,
        )
        monitor = _monitor_curve_and_terminate(
            process=process,
            abaqus_cmd=manifest["settings"]["abaqus_cmd"],
            job_name=command.job,
            work_dir=case_dir,
            inp_path=Path(case["working_inp"]),
            dat_path=case_dir / f"{command.job}.dat",
            poll_seconds=float(manifest["settings"]["poll_seconds"]),
            drop_fraction=float(manifest["settings"]["drop_fraction"]),
            min_points=int(manifest["settings"]["min_points"]),
            min_peak_mpa=float(manifest["settings"]["min_peak_mpa"]),
        )
    summary = extract_compression_dat(
        dat_path=case_dir / f"{command.job}.dat",
        inp_path=case["working_inp"],
        curve_csv=case["curve_csv"],
        summary_json=case["summary_json"],
        modulus_window_start=float(manifest["settings"]["modulus_window_start"]),
        modulus_window_end=float(manifest["settings"]["modulus_window_end"]),
    )
    payload = {
        "job_name": command.job,
        "command": command.to_shell_string(),
        "command_path": str(command_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "early_stop": monitor,
        "summary": summary,
        "finished_at": _now(),
    }
    _write_json(Path(case["early_stop_json"]), payload)
    point_count = int(summary.get("curve", {}).get("point_count") or 0)
    success = point_count > 0 and (
        int(monitor.get("analysis_returncode") or 0) == 0
        or bool(monitor.get("terminated_by_monitor"))
    )
    state.update(
        {
            "status": "SOLVED" if success else "ANALYSIS_FAILED",
            "analysis": payload,
            "metrics": summary.get("metrics", {}),
            "curve": summary.get("curve", {}),
        }
    )
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
    for case in cases:
        state = analysis_case(manifest, case, dry_run=dry_run)
        _append_status(
            run_dir,
            {
                "event": "analysis",
                "case_id": case["case_id"],
                "status": state.get("status"),
            },
        )
        states.append(state)
    return states


def collect_status(run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    counts: dict[str, int] = {}
    cases = []
    for case in manifest["cases"]:
        state = read_case_state(case)
        status = str(state.get("status", "MISSING"))
        counts[status] = counts.get(status, 0) + 1
        cases.append({"case_id": case["case_id"], "status": status, "state": state})
    return {"run_dir": str(run_dir.resolve()), "counts": counts, "cases": cases}


def _summary_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        state = read_case_state(case)
        summary_path = Path(case["summary_json"])
        summary = _read_json(summary_path) if summary_path.exists() else {}
        metrics = summary.get("metrics") or state.get("metrics") or {}
        curve = summary.get("curve") or state.get("curve") or {}
        early_stop_path = Path(case["early_stop_json"])
        early = _read_json(early_stop_path) if early_stop_path.exists() else {}
        early_stop = early.get("early_stop") or {}
        termination = early_stop.get("termination") or {}
        bbox = case["bbox_mm"]
        rows.append(
            {
                "case_id": case["case_id"],
                "status": state.get("status"),
                "source_inp": case["source_inp"],
                "job_name": case["job_name"],
                "datacheck_job_name": case["datacheck_job_name"],
                "datacheck_success": (state.get("datacheck") or {}).get("success"),
                "analysis_returncode": early_stop.get("analysis_returncode"),
                "terminated_by_monitor": early_stop.get("terminated_by_monitor"),
                "termination_reason": termination.get("reason"),
                "bbox_lx_mm": bbox["lx"],
                "bbox_ly_mm": bbox["ly"],
                "bbox_lz_mm": bbox["lz"],
                "volume_mm3": case["volume_mm3"],
                "modulus_gpa": metrics.get("modulus_gpa"),
                "compressive_strength_mpa": metrics.get("compressive_strength_mpa"),
                "peak_compression_strain_engineering": metrics.get(
                    "peak_compression_strain_engineering"
                ),
                "point_count": curve.get("point_count"),
                "last_strain_engineering": curve.get("last_strain_engineering"),
                "last_stress_mpa": curve.get("last_stress_mpa"),
                "curve_csv": case["curve_csv"],
                "summary_json": case["summary_json"],
                "state_json": str(_case_state_path(case)),
            }
        )
    return rows


def _failures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures = []
    for row in rows:
        status = str(row.get("status"))
        if status not in {"PREPARED", "DATACHECKED", "SOLVED"}:
            failures.append(row)
    return failures


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _cell_xml(row: int, col: int, value: Any) -> str:
    ref = f"{_column_name(col)}{row}"
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = xml_escape(str(value), quote=False)
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _sheet_xml(rows: list[list[Any]]) -> str:
    xml_rows = []
    for row_idx, row in enumerate(rows, start=1):
        cells = "".join(_cell_xml(row_idx, col_idx, value) for col_idx, value in enumerate(row, 1))
        xml_rows.append(f'<row r="{row_idx}">{cells}</row>')
    max_cols = max((len(row) for row in rows), default=1)
    dimension = f"A1:{_column_name(max_cols)}{max(len(rows), 1)}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/><sheetData>{"".join(xml_rows)}</sheetData>'
        "</worksheet>"
    )


def write_xlsx(path: Path, sheets: dict[str, list[list[Any]]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_names = list(sheets)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="'
            'application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for idx in range(1, len(sheet_names) + 1)
            )
            + "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            + "".join(
                f'<sheet name="{xml_escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
                for idx, name in enumerate(sheet_names, start=1)
            )
            + "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{idx}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/worksheet" '
                f'Target="worksheets/sheet{idx}.xml"/>'
                for idx in range(1, len(sheet_names) + 1)
            )
            + "</Relationships>",
        )
        for idx, name in enumerate(sheet_names, start=1):
            archive.writestr(f"xl/worksheets/sheet{idx}.xml", _sheet_xml(sheets[name]))
    return path


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
        ["params_json", manifest["params_json"]],
        ["params_sha256", manifest["params_sha256"]],
        ["candidate_id", manifest["candidate_id"]],
        ["purpose", manifest.get("purpose")],
        ["source_input_dir", manifest["source_input_dir"]],
        ["case_count", manifest["case_count"]],
        ["umat_source", manifest["umat_source"]],
        ["umat_source_sha256", manifest["umat_source_sha256"]],
        ["abaqus_cmd", manifest["settings"]["abaqus_cmd"]],
        ["cpus", manifest["settings"]["cpus"]],
        ["drop_fraction", manifest["settings"]["drop_fraction"]],
        ["poll_seconds", manifest["settings"]["poll_seconds"]],
        ["min_points", manifest["settings"]["min_points"]],
        ["min_peak_mpa", manifest["settings"]["min_peak_mpa"]],
    ]
    output = output or (Path(run_dir) / "reports" / "meso_compression_summary.xlsx")
    return write_xlsx(
        output,
        {
            "cases_summary": cases_sheet,
            "failures": failures_sheet,
            "provenance": provenance,
        },
    )


@app.command("prepare")
def prepare_command(
    params_json: Annotated[Path, typer.Option(help="Input parameter JSON.")],
    input_dir: Annotated[Path, typer.Option(help="Directory of raw INPs.")] = Path("13-inp"),
    run_dir: Annotated[Path, typer.Option(help="Batch run directory.")] = DEFAULT_RUN_DIR,
    force: Annotated[
        bool,
        typer.Option(help="Overwrite manifest and generated files."),
    ] = False,
) -> None:
    """Prepare patched compression cases from a parameter JSON."""

    manifest = prepare_batch(
        params_json=params_json,
        input_dir=input_dir,
        run_dir=run_dir,
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
    """Show meso compression batch status."""

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
    """Run datacheck for all prepared cases."""

    states = datacheck_cases(run_dir, dry_run=dry_run)
    failed = sum(1 for state in states if state.get("status") != "DATACHECKED")
    typer.echo(f"Datacheck processed {len(states)} cases, failed={failed}")
    if failed:
        raise typer.Exit(1)


@app.command("run")
def run_command(
    run_dir: Annotated[Path, typer.Option(help="Batch run directory.")] = DEFAULT_RUN_DIR,
    dry_run: Annotated[
        bool,
        typer.Option(help="Record commands without Abaqus."),
    ] = False,
) -> None:
    """Run all datachecked cases with DAT early stop."""

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
    """Write an XLSX summary of scalar metrics and curve paths."""

    path = write_results_xlsx(run_dir, output=output)
    typer.echo(str(path))
