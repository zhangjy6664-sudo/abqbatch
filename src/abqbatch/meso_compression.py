"""Puck-ZT meso compression batch workflow."""

from __future__ import annotations

import copy
import csv
import json
import math
import re
import shutil
import subprocess
import time
import zipfile
from collections.abc import Callable, Iterable
from datetime import datetime
from html import escape as xml_escape
from pathlib import Path
from typing import Annotated, Any
from xml.etree import ElementTree as ET

import typer

from abqbatch.abaqus_cmd import AbaqusCommand
from abqbatch.hashes import sha256_file, sha256_text
from abqbatch.monitor import detect_success

app = typer.Typer(help="Run JSON-driven Puck-ZT meso compression batches.")

PIPELINE_VERSION = "meso-compression-v1"
DEFAULT_RUN_DIR = Path("runs/puckzt_angle_0p8_g1c5_compression_35inp")
DEFAULT_SMOKE_CASE = "13-12-24"
MANIFEST_NAME = "manifest.json"
TARGET_ONLY = "TARGET_ONLY"
REFERENCE_SIM = "REFERENCE_SIM"
CURRENT_SIM = "CURRENT_SIM"
SIM_DATA_ROLES = {REFERENCE_SIM, CURRENT_SIM}
ANGLE_MIN_DEG = 1.0
ANGLE_MAX_DEG = 3.0
G1C_MIN = 5.0
G1C_MAX = 120.0
ACCEPTANCE_ERROR_PCT = 15.0
DEFAULT_MAX_NEW_ATTEMPTS = 3
DEFAULT_CALIBRATION_BATCH_SIZE = 6
DEFAULT_TARGET_GROUPS = {0: "QJ", 1: "13", 2: "132"}
DEFAULT_EXCLUDED_CALIBRATION_CASES = {
    "qj-24-24",
    "13-24-24",
    "13-24-48",
    "13-24-72",
}
DEFAULT_132_48_72_SOURCE = (
    Path("D:/ZDYF-NBY-ZJY")
    / "\u8d44\u6599"
    / "inp"
    / "inp\u7edf\u8ba1-JQ"
    / "inp\u7edf\u8ba1-JQ"
    / "132-48-72-jq.inp"
)


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

def _xlsx_relation_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets: dict[str, str] = {}
    for relation in root.findall("{*}Relationship"):
        relation_id = relation.get("Id")
        target = relation.get("Target")
        if relation_id and target:
            targets[relation_id] = target
    return targets


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall("{*}si"):
        strings.append("".join(node.text or "" for node in item.findall(".//{*}t")))
    return strings


def _xlsx_cell_column(ref: str | None, fallback: int) -> int:
    if not ref:
        return fallback
    match = re.match(r"([A-Za-z]+)", ref)
    if not match:
        return fallback
    index = 0
    for char in match.group(1).upper():
        index = index * 26 + ord(char) - ord("A") + 1
    return index


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str]) -> Any:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//{*}t"))
    value_node = cell.find("{*}v")
    if value_node is None or value_node.text is None:
        return None
    raw = value_node.text
    if cell_type == "s":
        index = int(raw)
        return shared_strings[index] if 0 <= index < len(shared_strings) else raw
    if cell_type == "b":
        return raw.strip() in {"1", "true", "TRUE"}
    try:
        value = float(raw)
    except ValueError:
        return raw
    if value.is_integer():
        return int(value)
    return value


def read_xlsx_rows(path: Path, sheet_name: str | None = None) -> list[list[Any]]:
    """Read one worksheet from a simple XLSX without adding an openpyxl dependency."""

    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        targets = _xlsx_relation_targets(archive)
        sheets = []
        relationship_id_attr = (
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        for sheet in workbook.findall(".//{*}sheet"):
            name = sheet.get("name")
            relationship_id = sheet.get(relationship_id_attr)
            if name and relationship_id:
                sheets.append((name, relationship_id))
        if not sheets:
            raise ValueError(f"No worksheets found in {path}")
        selected = next((item for item in sheets if item[0] == sheet_name), None)
        if selected is None:
            if sheet_name is not None:
                names = ", ".join(name for name, _ in sheets)
                raise ValueError(
                    f"Worksheet {sheet_name!r} not found in {path}; available: {names}"
                )
            selected = sheets[0]
        target = targets[selected[1]]
        sheet_path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        shared_strings = _xlsx_shared_strings(archive)
        sheet_root = ET.fromstring(archive.read(sheet_path))
    rows: list[list[Any]] = []
    for row in sheet_root.findall(".//{*}row"):
        values: list[Any] = []
        fallback_col = 1
        for cell in row.findall("{*}c"):
            col = _xlsx_cell_column(cell.get("r"), fallback_col)
            while len(values) < col - 1:
                values.append(None)
            values.append(_xlsx_cell_value(cell, shared_strings))
            fallback_col = col + 1
        rows.append(values)
    return rows


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _coerce_int(value: Any) -> int | None:
    number = _coerce_float(value)
    if number is None:
        return None
    rounded = round(number)
    if abs(number - rounded) > 1e-9:
        return None
    return int(rounded)


def _header_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _dict_rows(rows: list[list[Any]]) -> list[dict[str, Any]]:
    header: list[str] | None = None
    data_rows: list[dict[str, Any]] = []
    for row in rows:
        if not any(cell is not None and str(cell).strip() for cell in row):
            continue
        if header is None:
            header = [_header_key(cell) for cell in row]
            continue
        record: dict[str, Any] = {}
        for index, key in enumerate(header):
            if key:
                record[key] = row[index] if index < len(row) else None
        data_rows.append(record)
    return data_rows


def _case_key(case_id: str) -> str:
    return case_id.strip().lower()


def _target_case_id(group_value: Any, dim_a: Any, dim_b: Any) -> str | None:
    group_index = _coerce_int(group_value)
    dim_a_int = _coerce_int(dim_a)
    dim_b_int = _coerce_int(dim_b)
    if dim_a_int is None or dim_b_int is None:
        return None
    group_name: str | None = None
    if group_index is not None:
        group_name = DEFAULT_TARGET_GROUPS.get(group_index)
    if group_name is None:
        raw_group = str(group_value).strip()
        if raw_group.lower() in {"qj", "0"}:
            group_name = "QJ"
        elif raw_group in {"13", "1"}:
            group_name = "13"
        elif raw_group in {"132", "2"}:
            group_name = "132"
    if group_name is None:
        return None
    if group_name.upper() == "QJ":
        return f"QJ-{dim_a_int}-{dim_b_int}"
    return f"{group_name}-{dim_a_int}-{dim_b_int}"


def _default_source_inp(case_id: str) -> Path:
    if case_id.upper().startswith("QJ-"):
        return Path("qj") / f"{case_id}.inp"
    if case_id.startswith("132-"):
        return Path("132") / f"{case_id}.inp"
    return Path("13-inp") / f"{case_id}.inp"


def parse_strength_targets(
    target_xlsx: Path,
    *,
    sheet_name: str = "Sheet1",
    excluded_case_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse target strengths from Sheet1; these rows never become interpolation points."""

    excluded = excluded_case_ids or DEFAULT_EXCLUDED_CALIBRATION_CASES
    rows = read_xlsx_rows(target_xlsx, sheet_name=sheet_name)
    targets: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, start=1):
        if len(row) < 5:
            continue
        case_id = _target_case_id(row[0], row[1], row[2])
        target_strength = _coerce_float(row[4])
        if case_id is None or target_strength is None:
            continue
        source_inp = (
            DEFAULT_132_48_72_SOURCE
            if _case_key(case_id) == "132-48-72"
            else _default_source_inp(case_id)
        )
        targets.append(
            {
                "data_role": TARGET_ONLY,
                "case_id": case_id,
                "case_key": _case_key(case_id),
                "target_strength_mpa": target_strength,
                "target_xlsx": str(target_xlsx),
                "target_sheet": sheet_name,
                "target_row": row_index,
                "source_inp": str(source_inp),
                "excluded": _case_key(case_id) in excluded,
            }
        )
    return targets


def _first_present(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        normalized = _header_key(key)
        if normalized in row and row[normalized] not in {None, ""}:
            return row[normalized]
    return None


def _parse_param_token(token: str) -> float | None:
    token = token.strip().lower().replace("p", ".")
    return _coerce_float(token)


def _infer_angle_g1c(row: dict[str, Any]) -> tuple[float | None, float | None]:
    haystack = " ".join(
        str(value)
        for key, value in row.items()
        if key in {"candidate_id", "job_name", "datacheck_job_name", "batch", "source_inp"}
        and value is not None
    )
    match = re.search(r"angle[_-]([0-9]+(?:p[0-9]+|\.[0-9]+)?)", haystack, re.IGNORECASE)
    angle = _parse_param_token(match.group(1)) if match else None
    match = re.search(r"g1c[_-]?([0-9]+(?:p[0-9]+|\.[0-9]+)?)", haystack, re.IGNORECASE)
    g1c = _parse_param_token(match.group(1)) if match else None
    return angle, g1c


def parse_sim_summary_xlsx(
    summary_xlsx: Path,
    *,
    data_role: str,
    sheet_name: str = "all_cases_summary",
    default_angle_deg: float | None = None,
    default_g1c: float | None = None,
) -> list[dict[str, Any]]:
    """Parse actual simulation points from a summary workbook."""

    if data_role not in SIM_DATA_ROLES:
        raise ValueError(f"Simulation summary must use a simulation data_role, got {data_role!r}")
    try:
        raw_rows = read_xlsx_rows(summary_xlsx, sheet_name=sheet_name)
    except ValueError:
        raw_rows = read_xlsx_rows(summary_xlsx)
    records: list[dict[str, Any]] = []
    for row_index, row in enumerate(_dict_rows(raw_rows), start=2):
        case_id = _first_present(row, ["case_id"])
        strength = _coerce_float(
            _first_present(
                row, ["compressive_strength_mpa", "compression_strength_mpa", "strength_mpa"]
            )
        )
        if not case_id or strength is None:
            continue
        inferred_angle, inferred_g1c = _infer_angle_g1c(row)
        angle = _coerce_float(_first_present(row, ["angle_deg", "fiber_angle_deg", "angle"]))
        g1c = _coerce_float(_first_present(row, ["g1c", "g1c_j_mm2", "G1C"]))
        angle = (
            angle
            if angle is not None
            else (inferred_angle if inferred_angle is not None else default_angle_deg)
        )
        g1c = (
            g1c if g1c is not None else (inferred_g1c if inferred_g1c is not None else default_g1c)
        )
        if angle is None or g1c is None:
            continue
        case_id_text = str(case_id).strip()
        records.append(
            {
                "data_role": data_role,
                "case_id": case_id_text,
                "case_key": _case_key(case_id_text),
                "angle_deg": float(angle),
                "g1c": float(g1c),
                "compressive_strength_mpa": float(strength),
                "status": _first_present(row, ["status"]),
                "job_name": _first_present(row, ["job_name"]),
                "attempt_id": _first_present(row, ["attempt_id"]),
                "batch_id": _first_present(row, ["batch_id", "batch"]),
                "summary_xlsx": str(summary_xlsx),
                "summary_row": row_index,
            }
        )
    return records


def read_calibration_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for row_index, row in enumerate(reader, start=2):
            role = row.get("data_role") or CURRENT_SIM
            if role not in SIM_DATA_ROLES:
                raise ValueError(
                    f"History row {row_index} has invalid simulation data_role: {role!r}"
                )
            case_id = str(row.get("case_id") or "").strip()
            strength = _coerce_float(row.get("compressive_strength_mpa"))
            angle = _coerce_float(row.get("angle_deg"))
            g1c = _coerce_float(row.get("g1c"))
            if not case_id or strength is None or angle is None or g1c is None:
                continue
            records.append(
                {
                    "data_role": role,
                    "case_id": case_id,
                    "case_key": _case_key(case_id),
                    "angle_deg": angle,
                    "g1c": g1c,
                    "compressive_strength_mpa": strength,
                    "status": row.get("status"),
                    "job_name": row.get("job_name"),
                    "attempt_id": row.get("attempt_id"),
                    "batch_id": row.get("batch_id"),
                    "history_csv": str(path),
                    "history_row": row_index,
                }
            )
    return records


def append_calibration_history(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "data_role",
        "batch_id",
        "case_id",
        "attempt_id",
        "angle_deg",
        "g1c",
        "target_strength_mpa",
        "compressive_strength_mpa",
        "error_pct",
        "accepted",
        "status",
        "job_name",
        "run_dir",
        "state_json",
        "summary_json",
        "curve_csv",
    ]
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            out = dict(row)
            out["data_role"] = out.get("data_role") or CURRENT_SIM
            writer.writerow(out)


def strength_error_pct(*, target_strength_mpa: float, simulated_strength_mpa: float) -> float:
    if target_strength_mpa <= 0:
        raise ValueError("Target strength must be positive for relative error calculation")
    return abs(simulated_strength_mpa - target_strength_mpa) / target_strength_mpa * 100.0


def _assert_simulation_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    checked = list(records)
    bad_roles = sorted(
        {
            str(record.get("data_role"))
            for record in checked
            if record.get("data_role") not in SIM_DATA_ROLES
        }
    )
    if bad_roles:
        raise ValueError(
            f"Interpolator input includes non-simulation data_role(s): {', '.join(bad_roles)}"
        )
    return checked


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _same_param(value: float) -> float:
    return round(float(value), 6)


def _sampled(records: list[dict[str, Any]], angle_deg: float, g1c: float) -> bool:
    return any(
        abs(float(record["angle_deg"]) - angle_deg) <= 1e-6
        and abs(float(record["g1c"]) - g1c) <= 1e-6
        for record in records
    )


def _within_calibration_bounds(record: dict[str, Any]) -> bool:
    angle = float(record["angle_deg"])
    g1c = float(record["g1c"])
    return ANGLE_MIN_DEG <= angle <= ANGLE_MAX_DEG and G1C_MIN <= g1c <= G1C_MAX


def _brackets(target_strength: float, left: float, right: float) -> bool:
    return min(left, right) <= target_strength <= max(left, right) and abs(left - right) > 1e-9


def _linear_inverse(target_strength: float, x1: float, y1: float, x2: float, y2: float) -> float:
    return x1 + (target_strength - y1) * (x2 - x1) / (y2 - y1)


def _candidate_from_sim_bracket(
    records: list[dict[str, Any]],
    *,
    target_strength: float,
) -> dict[str, Any] | None:
    by_g1c: dict[float, list[dict[str, Any]]] = {}
    by_angle: dict[float, list[dict[str, Any]]] = {}
    for record in records:
        by_g1c.setdefault(_same_param(record["g1c"]), []).append(record)
        by_angle.setdefault(_same_param(record["angle_deg"]), []).append(record)

    candidates: list[dict[str, Any]] = []
    for g1c, group in by_g1c.items():
        ordered = sorted(group, key=lambda item: float(item["angle_deg"]))
        for left, right in zip(ordered, ordered[1:], strict=False):
            y1 = float(left["compressive_strength_mpa"])
            y2 = float(right["compressive_strength_mpa"])
            if not _brackets(target_strength, y1, y2):
                continue
            angle = _linear_inverse(
                target_strength, float(left["angle_deg"]), y1, float(right["angle_deg"]), y2
            )
            angle = _clamp(angle, ANGLE_MIN_DEG, ANGLE_MAX_DEG)
            candidates.append(
                {
                    "angle_deg": angle,
                    "g1c": float(g1c),
                    "reason": "same_g1c_strength_bracket",
                    "span_mpa": abs(y2 - y1),
                }
            )
    for angle, group in by_angle.items():
        ordered = sorted(group, key=lambda item: float(item["g1c"]))
        for left, right in zip(ordered, ordered[1:], strict=False):
            y1 = float(left["compressive_strength_mpa"])
            y2 = float(right["compressive_strength_mpa"])
            g1c1 = float(left["g1c"])
            g1c2 = float(right["g1c"])
            if g1c1 <= 0 or g1c2 <= 0 or not _brackets(target_strength, y1, y2):
                continue
            log_g1c = _linear_inverse(target_strength, math.log(g1c1), y1, math.log(g1c2), y2)
            g1c = _clamp(math.exp(log_g1c), G1C_MIN, G1C_MAX)
            candidates.append(
                {
                    "angle_deg": float(angle),
                    "g1c": g1c,
                    "reason": "same_angle_log_g1c_strength_bracket",
                    "span_mpa": abs(y2 - y1),
                }
            )
    for candidate in sorted(
        candidates, key=lambda item: (item["span_mpa"], item["g1c"], item["angle_deg"])
    ):
        candidate["angle_deg"] = round(float(candidate["angle_deg"]), 4)
        candidate["g1c"] = round(float(candidate["g1c"]), 4)
        if not _sampled(records, candidate["angle_deg"], candidate["g1c"]):
            return candidate
    return None


def _fallback_candidate(
    records: list[dict[str, Any]], *, target_strength: float
) -> dict[str, Any] | None:
    if not records:
        return {"angle_deg": ANGLE_MIN_DEG, "g1c": G1C_MIN, "reason": "initial_exploration"}
    best = min(
        records,
        key=lambda item: abs(float(item["compressive_strength_mpa"]) - target_strength),
    )
    best_strength = float(best["compressive_strength_mpa"])
    if best_strength > target_strength:
        grid = [(3.0, 5.0), (2.5, 5.0), (2.0, 5.0), (3.0, 15.0), (2.0, 15.0), (1.0, 5.0)]
        reason = "nearest_sim_above_target_reduce_strength"
    else:
        grid = [(1.0, 120.0), (1.0, 80.0), (1.5, 120.0), (1.0, 60.0), (2.0, 120.0), (1.0, 30.0)]
        reason = "nearest_sim_below_target_raise_strength"
    for angle, g1c in grid:
        if not _sampled(records, angle, g1c):
            return {"angle_deg": angle, "g1c": g1c, "reason": reason}
    return None


def select_strength_candidate(
    target: dict[str, Any],
    sim_records: Iterable[dict[str, Any]],
    *,
    max_new_attempts: int = DEFAULT_MAX_NEW_ATTEMPTS,
    acceptance_error_pct: float = ACCEPTANCE_ERROR_PCT,
) -> dict[str, Any]:
    records = [
        record
        for record in _assert_simulation_records(sim_records)
        if record.get("case_key") == target.get("case_key")
    ]
    target_strength = float(target["target_strength_mpa"])
    accepted_records = []
    for record in records:
        error = strength_error_pct(
            target_strength_mpa=target_strength,
            simulated_strength_mpa=float(record["compressive_strength_mpa"]),
        )
        if error <= acceptance_error_pct and _within_calibration_bounds(record):
            accepted_records.append((error, record))
    if accepted_records:
        error, record = min(accepted_records, key=lambda item: item[0])
        return {
            "status": "accepted",
            "case_id": target["case_id"],
            "case_key": target["case_key"],
            "target_strength_mpa": target_strength,
            "best_error_pct": round(error, 6),
            "accepted_source_role": record["data_role"],
            "accepted_angle_deg": record["angle_deg"],
            "accepted_g1c": record["g1c"],
            "reason": "simulation_within_error_limit",
        }

    current_attempts = sum(1 for record in records if record.get("data_role") == CURRENT_SIM)
    if current_attempts >= max_new_attempts:
        best_error = None
        if records:
            best_error = min(
                strength_error_pct(
                    target_strength_mpa=target_strength,
                    simulated_strength_mpa=float(record["compressive_strength_mpa"]),
                )
                for record in records
            )
        return {
            "status": "max_attempts_reached",
            "case_id": target["case_id"],
            "case_key": target["case_key"],
            "target_strength_mpa": target_strength,
            "current_attempts": current_attempts,
            "best_error_pct": round(best_error, 6) if best_error is not None else None,
            "reason": "max_new_attempts_reached_without_acceptance",
        }

    candidate = _candidate_from_sim_bracket(records, target_strength=target_strength)
    if candidate is None:
        candidate = _fallback_candidate(records, target_strength=target_strength)
    if candidate is None:
        return {
            "status": "no_unsampled_candidate",
            "case_id": target["case_id"],
            "case_key": target["case_key"],
            "target_strength_mpa": target_strength,
            "current_attempts": current_attempts,
            "reason": "exploration_grid_exhausted",
        }
    attempt_id = current_attempts + 1
    return {
        "status": "planned",
        "case_id": target["case_id"],
        "case_key": target["case_key"],
        "target_strength_mpa": target_strength,
        "attempt_id": attempt_id,
        "angle_deg": float(candidate["angle_deg"]),
        "g1c": float(candidate["g1c"]),
        "source_inp": target.get("source_inp"),
        "planned_data_role": CURRENT_SIM,
        "reason": candidate["reason"],
        "current_attempts": current_attempts,
    }


def build_calibration_plan(
    targets: list[dict[str, Any]],
    sim_records: list[dict[str, Any]],
    *,
    case_ids: set[str] | None = None,
    batch_size: int = DEFAULT_CALIBRATION_BATCH_SIZE,
    max_new_attempts: int = DEFAULT_MAX_NEW_ATTEMPTS,
) -> dict[str, Any]:
    _assert_simulation_records(sim_records)
    selected_case_keys = {_case_key(case_id) for case_id in case_ids} if case_ids else None
    decisions: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    active_targets = [
        target
        for target in targets
        if target.get("data_role") == TARGET_ONLY
        and not target.get("excluded")
        and (selected_case_keys is None or target.get("case_key") in selected_case_keys)
    ]
    for target in sorted(active_targets, key=lambda item: str(item["case_id"])):
        decision = select_strength_candidate(
            target,
            sim_records,
            max_new_attempts=max_new_attempts,
        )
        decisions.append(decision)
        if decision["status"] == "planned" and len(candidates) < batch_size:
            candidate = dict(decision)
            candidate["batch_slot"] = len(candidates) + 1
            candidates.append(candidate)
    return {
        "created_at": _now(),
        "pipeline_version": PIPELINE_VERSION,
        "target_count": len(targets),
        "active_target_count": len(active_targets),
        "simulation_point_count": len(sim_records),
        "batch_size": batch_size,
        "candidate_count": len(candidates),
        "decisions": decisions,
        "candidates": candidates,
    }


def _dict_sheet(rows: list[dict[str, Any]], headers: list[str] | None = None) -> list[list[Any]]:
    if headers is None:
        seen: list[str] = []
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.append(key)
        headers = seen or ["status"]
    return [headers] + [[row.get(header) for header in headers] for row in rows]


def write_calibration_outputs(
    output_root: Path,
    plan: dict[str, Any],
    targets: list[dict[str, Any]],
    sim_records: list[dict[str, Any]],
) -> dict[str, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "calibration_manifest.json"
    candidates_csv = output_root / "next_candidates.csv"
    xlsx_path = output_root / "calibration_summary.xlsx"
    _write_json(manifest_path, plan)
    candidate_headers = [
        "batch_slot",
        "case_id",
        "attempt_id",
        "angle_deg",
        "g1c",
        "target_strength_mpa",
        "planned_data_role",
        "reason",
        "source_inp",
    ]
    with candidates_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=candidate_headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(plan["candidates"])
    write_xlsx(
        xlsx_path,
        {
            "next_candidates": _dict_sheet(plan["candidates"], candidate_headers),
            "decisions": _dict_sheet(plan["decisions"]),
            "targets": _dict_sheet(targets),
            "simulation_points": _dict_sheet(sim_records),
        },
    )
    return {"manifest": manifest_path, "candidates_csv": candidates_csv, "xlsx": xlsx_path}




def _param_slug(value: float) -> str:
    return f"{float(value):.4g}".replace("-", "m").replace(".", "p")


def calibration_candidate_id(candidate: dict[str, Any]) -> str:
    return (
        f"cal_a{_param_slug(float(candidate['angle_deg']))}_"
        f"g{_param_slug(float(candidate['g1c']))}_"
        f"t{int(candidate.get('attempt_id') or 1)}"
    )


def _resolve_existing_path(path_text: str | Path, *, base_dir: Path | None = None) -> Path:
    path = Path(path_text)
    candidates = [path]
    if not path.is_absolute():
        if base_dir is not None:
            candidates.append(base_dir / path)
        candidates.append(Path.cwd() / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(f"Could not resolve path: {path_text}")


def patch_umat_angle(text: str, angle_deg: float) -> str:
    replacement = rf"\g<1>{float(angle_deg):.10g}\g<2>"
    patched, count = re.subn(
        r"(?im)^(\s*ANGLE_INI\s*=\s*)[0-9.+\-EeDd]+(\s*/\s*180\.\s*\*\s*3\.141592654\s*)$",
        replacement,
        text,
        count=1,
    )
    if count != 1:
        raise ValueError("Could not find active ANGLE_INI assignment in UMAT")
    return patched


def build_calibration_candidate_params(
    base_params: dict[str, Any],
    candidate: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    params = copy.deepcopy(base_params)
    case_id = str(candidate["case_id"])
    angle = float(candidate["angle_deg"])
    g1c = float(candidate["g1c"])
    candidate_id = calibration_candidate_id(candidate)
    params["candidate_id"] = candidate_id
    params["purpose"] = f"adaptive compression calibration {case_id} {candidate_id}"
    candidate_parameters = params.setdefault("candidate_parameters", {})
    candidate_parameters.update(
        {
            "fiber_angle_deg": angle,
            "g1c": g1c,
            "calibration_case_id": case_id,
            "calibration_attempt_id": candidate.get("attempt_id"),
            "calibration_data_role": CURRENT_SIM,
        }
    )
    for material_name in ("WARP", "WEFT"):
        params["materials"][material_name]["constants"]["G1C"] = g1c

    umat_source = next(
        (
            path
            for path in (
                _resolve_project_path(params, params.get("umat", {}).get("working_copy")),
                _resolve_project_path(params, params.get("umat", {}).get("source")),
            )
            if path is not None and path.exists()
        ),
        None,
    )
    if umat_source is None:
        raise FileNotFoundError("Could not resolve UMAT working_copy or source from params JSON")
    output_dir.mkdir(parents=True, exist_ok=True)
    umat_target = output_dir / f"{candidate_id}_{Path(umat_source).name}"
    umat_text = Path(umat_source).read_text(encoding="utf-8", errors="ignore")
    umat_target.write_text(patch_umat_angle(umat_text, angle), encoding="utf-8", newline="")
    params.setdefault("umat", {})["working_copy"] = str(umat_target.resolve())
    params["umat"]["angle_assignment"] = f"ANGLE_INI={angle:.10g}/180.*3.141592654"
    return params


def prepare_calibration_candidate(
    *,
    params_json: Path,
    candidate: dict[str, Any],
    batch_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    base_params = _read_json(params_json)
    case_id = str(candidate["case_id"])
    candidate_id = calibration_candidate_id(candidate)
    attempt_name = f"{int(candidate.get('batch_slot') or 1):02d}_{case_id}_{candidate_id}"
    attempt_dir = batch_dir / attempt_name
    input_dir = attempt_dir / "source_inp"
    input_dir.mkdir(parents=True, exist_ok=True)
    source_inp = _resolve_existing_path(
        candidate["source_inp"],
        base_dir=Path(params_json).resolve().parent,
    )
    canonical_source = input_dir / f"{case_id}.inp"
    shutil.copy2(source_inp, canonical_source)
    params_dir = attempt_dir / "params"
    params = build_calibration_candidate_params(
        base_params,
        candidate,
        output_dir=params_dir / "umats",
    )
    params_path = params_dir / f"{candidate_id}_params.json"
    _write_json(params_path, params)
    run_dir = attempt_dir / "run"
    manifest = prepare_batch(
        params_json=params_path,
        input_dir=input_dir,
        run_dir=run_dir,
        force=force,
    )
    manifest["calibration_candidate"] = {
        key: value for key, value in candidate.items() if isinstance(value, (str, int, float, bool))
    }
    _write_manifest(run_dir, manifest)
    return {
        "candidate": candidate,
        "attempt_dir": str(attempt_dir),
        "run_dir": str(run_dir),
        "params_json": str(params_path),
        "manifest": manifest,
        "case": manifest["cases"][0],
    }


def calibration_history_row_from_case(
    *,
    candidate: dict[str, Any],
    case: dict[str, Any],
    batch_id: str,
) -> dict[str, Any]:
    state = read_case_state(case)
    summary_path = Path(case["summary_json"])
    summary = _read_json(summary_path) if summary_path.exists() else {}
    metrics = summary.get("metrics") or state.get("metrics") or {}
    strength = _coerce_float(metrics.get("compressive_strength_mpa"))
    target_strength = _coerce_float(candidate.get("target_strength_mpa"))
    error = None
    accepted = None
    if strength is not None and target_strength is not None:
        error = strength_error_pct(
            target_strength_mpa=target_strength,
            simulated_strength_mpa=strength,
        )
        accepted = error <= ACCEPTANCE_ERROR_PCT
    return {
        "data_role": CURRENT_SIM,
        "batch_id": batch_id,
        "case_id": candidate["case_id"],
        "case_key": candidate.get("case_key") or _case_key(str(candidate["case_id"])),
        "attempt_id": candidate.get("attempt_id"),
        "angle_deg": candidate.get("angle_deg"),
        "g1c": candidate.get("g1c"),
        "target_strength_mpa": target_strength,
        "compressive_strength_mpa": strength,
        "error_pct": round(error, 6) if error is not None else None,
        "accepted": accepted,
        "status": state.get("status"),
        "job_name": case.get("job_name"),
        "run_dir": str(Path(case["case_dir"]).parent.parent.parent),
        "state_json": str(_case_state_path(case)),
        "summary_json": case.get("summary_json"),
        "curve_csv": case.get("curve_csv"),
    }


def run_calibration_batch(
    *,
    params_json: Path,
    candidates: list[dict[str, Any]],
    output_root: Path,
    batch_id: str,
    history_csv: Path,
    archive_root: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    batch_dir = output_root / "batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[dict[str, Any]] = []
    history_rows: list[dict[str, Any]] = []
    attempt_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        prepared_attempt = prepare_calibration_candidate(
            params_json=params_json,
            candidate=candidate,
            batch_dir=batch_dir,
            force=force,
        )
        prepared.append(
            {
                "case_id": candidate["case_id"],
                "attempt_id": candidate.get("attempt_id"),
                "angle_deg": candidate.get("angle_deg"),
                "g1c": candidate.get("g1c"),
                "run_dir": prepared_attempt["run_dir"],
                "params_json": prepared_attempt["params_json"],
            }
        )
        run_dir = Path(prepared_attempt["run_dir"])
        datacheck_states = datacheck_cases(run_dir, dry_run=dry_run)
        analysis_states = analysis_cases(run_dir, dry_run=dry_run)
        case = load_manifest(run_dir)["cases"][0]
        row = calibration_history_row_from_case(candidate=candidate, case=case, batch_id=batch_id)
        row["datacheck_status"] = datacheck_states[0].get("status") if datacheck_states else None
        row["analysis_status"] = analysis_states[0].get("status") if analysis_states else None
        history_rows.append(row)
        attempt_rows.append(
            {
                "batch_id": batch_id,
                "case_id": candidate["case_id"],
                "attempt_id": candidate.get("attempt_id"),
                "angle_deg": candidate.get("angle_deg"),
                "g1c": candidate.get("g1c"),
                "job_name": case.get("job_name"),
            }
        )
    append_calibration_history(history_csv, history_rows)
    archive_result = None
    if archive_root is not None:
        raw_archive_result = archive_odb_files(
            batch_dir,
            archive_root=archive_root,
            index_dir=output_root / "archive_indexes",
            batch_id=batch_id,
            attempt_rows=attempt_rows,
        )
        archive_result = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in raw_archive_result.items()
        }
    result = {
        "batch_id": batch_id,
        "batch_dir": str(batch_dir),
        "history_csv": str(history_csv),
        "prepared": prepared,
        "history_rows": history_rows,
        "attempt_rows": attempt_rows,
        "archive": archive_result,
    }
    _write_json(output_root / f"{batch_id}_execution_summary.json", result)
    return result


def archive_odb_files(
    run_dir: Path,
    *,
    archive_root: Path,
    index_dir: Path,
    batch_id: str,
    attempt_rows: list[dict[str, Any]],
    stamp: str | None = None,
    file_glob: str = "*.odb",
    move_file: Callable[[Path, Path], None] | None = None,
) -> dict[str, Path | int]:
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = archive_root / f"compression_calibration_{stamp}" / batch_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)
    attempts_by_job = {str(row.get("job_name")): row for row in attempt_rows if row.get("job_name")}
    rows: list[dict[str, Any]] = []
    for odb in sorted(run_dir.rglob(file_glob)):
        original_path = odb.resolve()
        try:
            relative = original_path.relative_to(run_dir.resolve())
        except ValueError:
            relative = Path(odb.name)
        job_name = odb.stem
        attempt = attempts_by_job.get(job_name, {})
        size = odb.stat().st_size
        digest = sha256_file(odb)
        archive_path = archive_dir / relative
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if move_file is None:
            shutil.move(str(odb), str(archive_path))
            if odb.exists():
                raise PermissionError(f"ODB archive move left source file in place: {odb}")
        else:
            move_file(odb, archive_path)
        rows.append(
            {
                "archived_at": _now(),
                "batch_id": batch_id,
                "case_id": attempt.get("case_id"),
                "attempt_id": attempt.get("attempt_id"),
                "angle_deg": attempt.get("angle_deg"),
                "g1c": attempt.get("g1c"),
                "original_path": str(original_path),
                "archive_path": str(archive_path.resolve()),
                "size_bytes": size,
                "sha256": digest,
                "job_name": job_name,
            }
        )
    headers = [
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
    ]
    d_index = index_dir / f"{batch_id}_odb_archive_index.csv"
    e_index = archive_dir / f"{batch_id}_odb_archive_index.csv"
    for index_path in (d_index, e_index):
        with index_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
    return {
        "archive_dir": archive_dir,
        "d_index": d_index,
        "e_index": e_index,
        "archived_count": len(rows),
    }


@app.command("calibrate-strength")
def calibrate_strength_command(
    target_xlsx: Annotated[
        Path,
        typer.Option(
            "--target-xlsx",
            help="Static strength dataset XLSX; Sheet1 column 5 is the compression target.",
        ),
    ],
    reference_summary_xlsx: Annotated[
        list[Path] | None,
        typer.Option(
            "--reference-summary-xlsx",
            help="Previous-round actual simulation summary XLSX. May be repeated.",
        ),
    ] = None,
    params_json: Annotated[
        Path | None,
        typer.Option("--params-json", help="Base compression parameter JSON for execution."),
    ] = None,
    current_summary_xlsx: Annotated[
        list[Path] | None,
        typer.Option(
            "--current-summary-xlsx",
            help="Current-round completed simulation summary XLSX. May be repeated.",
        ),
    ] = None,
    current_history_csv: Annotated[
        Path | None,
        typer.Option("--current-history-csv", help="Current calibration history CSV."),
    ] = None,
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Directory for calibration manifests, history, candidate CSVs, and XLSX.",
        ),
    ] = Path("runs/compression_strength_calibration"),
    case_id: Annotated[
        list[str] | None,
        typer.Option("--case-id", help="Restrict planning to a case id. May be repeated."),
    ] = None,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="Maximum new candidates in each batch.")
    ] = DEFAULT_CALIBRATION_BATCH_SIZE,
    max_new_attempts: Annotated[
        int, typer.Option("--max-new-attempts", help="Maximum new simulations per case.")
    ] = DEFAULT_MAX_NEW_ATTEMPTS,
    max_batches: Annotated[
        int, typer.Option("--max-batches", help="Maximum adaptive batches to execute.")
    ] = 1,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute",
            help="Prepare, datacheck, run, record history, and archive ODBs.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Use pipeline dry-run submission paths; no Abaqus solve."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing prepared batch dirs."),
    ] = False,
    batch_id: Annotated[str | None, typer.Option("--batch-id", help="Batch id prefix.")] = None,
    archive_root: Annotated[
        Path,
        typer.Option("--archive-root", help="Archive root, normally on E: drive."),
    ] = Path("E:/ZDYF_NBY_abqbatch_archive"),
    no_archive: Annotated[
        bool,
        typer.Option("--no-archive", help="Do not archive ODBs after executed batches."),
    ] = False,
    reference_angle_deg: Annotated[
        float,
        typer.Option(
            "--reference-angle-deg",
            help="Default angle for legacy reference summaries without an angle column.",
        ),
    ] = 0.8,
    reference_g1c: Annotated[
        float,
        typer.Option(
            "--reference-g1c",
            help="Default G1C for legacy reference summaries without a G1C column.",
        ),
    ] = 5.0,
) -> None:
    """Plan or execute adaptive compression calibration batches."""

    if target_xlsx.name.startswith("~$"):
        raise typer.BadParameter("Use the real target workbook, not an Excel lock file")
    if max_batches < 1:
        raise typer.BadParameter("--max-batches must be at least 1")
    if execute and params_json is None:
        raise typer.BadParameter("--params-json is required with --execute")

    output_root.mkdir(parents=True, exist_ok=True)
    history_csv = current_history_csv or (output_root / "calibration_history.csv")
    targets = parse_strength_targets(target_xlsx)
    reference_records: list[dict[str, Any]] = []
    for summary in reference_summary_xlsx or []:
        reference_records.extend(
            parse_sim_summary_xlsx(
                summary,
                data_role=REFERENCE_SIM,
                default_angle_deg=reference_angle_deg,
                default_g1c=reference_g1c,
            )
        )
    current_summary_records: list[dict[str, Any]] = []
    for summary in current_summary_xlsx or []:
        current_summary_records.extend(
            parse_sim_summary_xlsx(
                summary,
                data_role=CURRENT_SIM,
                default_angle_deg=None,
                default_g1c=None,
            )
        )

    batch_prefix = batch_id or f"calibration_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    last_plan: dict[str, Any] | None = None
    paths: dict[str, Path] | None = None
    executed_batches = 0
    for batch_index in range(1, max_batches + 1):
        sim_records = list(reference_records) + list(current_summary_records)
        sim_records.extend(read_calibration_history(history_csv))
        this_batch_id = batch_prefix if max_batches == 1 else f"{batch_prefix}_b{batch_index:02d}"
        plan = build_calibration_plan(
            targets,
            sim_records,
            case_ids=set(case_id) if case_id else None,
            batch_size=batch_size,
            max_new_attempts=max_new_attempts,
        )
        plan.update(
            {
                "batch_id": this_batch_id,
                "batch_index": batch_index,
                "target_xlsx": str(target_xlsx),
                "reference_summary_xlsx": [str(path) for path in reference_summary_xlsx or []],
                "current_summary_xlsx": [str(path) for path in current_summary_xlsx or []],
                "current_history_csv": str(history_csv),
                "params_json": str(params_json) if params_json else None,
                "execute": execute,
                "dry_run": dry_run,
                "data_roles": {
                    "targets": TARGET_ONLY,
                    "reference_summaries": REFERENCE_SIM,
                    "current_summaries": CURRENT_SIM,
                    "current_history": CURRENT_SIM,
                },
            }
        )
        paths = write_calibration_outputs(output_root, plan, targets, sim_records)
        last_plan = plan
        if not execute or not plan["candidates"]:
            break
        archive_target = None if no_archive or dry_run else archive_root
        execution = run_calibration_batch(
            params_json=params_json or Path(),
            candidates=plan["candidates"],
            output_root=output_root,
            batch_id=this_batch_id,
            history_csv=history_csv,
            archive_root=archive_target,
            dry_run=dry_run,
            force=force,
        )
        executed_batches += 1
        plan["execution"] = execution
        sim_records.extend(execution["history_rows"])
        paths = write_calibration_outputs(output_root, plan, targets, sim_records)

    if last_plan is None or paths is None:
        raise typer.Exit(1)
    message = (
        f"Planned {last_plan['candidate_count']} candidates from "
        f"{last_plan['active_target_count']} active targets and "
        f"{last_plan['simulation_point_count']} simulation points."
    )
    typer.echo(message)
    if execute:
        typer.echo(f"Executed batches: {executed_batches}")
        typer.echo(f"History CSV: {history_csv}")
    typer.echo(f"Manifest: {paths['manifest']}")
    typer.echo(f"Candidates: {paths['candidates_csv']}")
    typer.echo(f"Summary XLSX: {paths['xlsx']}")


@app.command("archive-odb")
def archive_odb_command(
    run_dir: Annotated[
        Path, typer.Option("--run-dir", help="Run directory containing ODB files to archive.")
    ],
    batch_id: Annotated[
        str, typer.Option("--batch-id", help="Calibration batch id for the archive index.")
    ],
    archive_root: Annotated[
        Path,
        typer.Option("--archive-root", help="Archive root, normally on E: drive."),
    ] = Path("E:/ZDYF_NBY_abqbatch_archive"),
    index_dir: Annotated[
        Path | None,
        typer.Option("--index-dir", help="D-drive directory for a copy of the archive index."),
    ] = None,
    attempts_csv: Annotated[
        Path | None,
        typer.Option(
            "--attempts-csv",
            help="CSV carrying case_id/attempt_id/angle_deg/g1c/job_name metadata.",
        ),
    ] = None,
) -> None:
    """Move ODB files to the archive root and write D/E index CSVs."""

    attempt_rows: list[dict[str, Any]] = []
    if attempts_csv is not None and attempts_csv.exists():
        with attempts_csv.open("r", encoding="utf-8-sig", newline="") as stream:
            attempt_rows = list(csv.DictReader(stream))
    result = archive_odb_files(
        run_dir,
        archive_root=archive_root,
        index_dir=index_dir or (run_dir / "reports"),
        batch_id=batch_id,
        attempt_rows=attempt_rows,
    )
    typer.echo(f"Archived {result['archived_count']} ODB files to {result['archive_dir']}")
    typer.echo(f"D index: {result['d_index']}")
    typer.echo(f"Archive index: {result['e_index']}")


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
