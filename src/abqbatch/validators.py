"""Static validation for Abaqus input decks and abqbatch configs."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abqbatch import db
from abqbatch.config import ProjectConfig
from abqbatch.exceptions import Status
from abqbatch.inp_parser import InpDeck, KeywordBlock
from abqbatch.inventory import read_cases_csv


@dataclass(frozen=True)
class ValidationIssue:
    case_id: str
    severity: str
    code: str
    message: str
    path: str
    line: int | None = None
    context: str | None = None


def _issue(
    case_id: str,
    severity: str,
    code: str,
    message: str,
    path: Path,
    line: int | None = None,
    context: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(case_id, severity, code, message, str(path), line, context)


def validate_inp_file(
    path: Path,
    case_id: str = "-",
    project_root: Path | None = None,
    material_profile: str | None = None,
    load_profile: str | None = None,
    post_profile: str | None = None,
    materials: dict[str, Any] | None = None,
    loads: dict[str, Any] | None = None,
    post_profiles: dict[str, Any] | None = None,
) -> list[ValidationIssue]:
    """Validate one .inp file and its selected abqbatch profiles."""

    issues: list[ValidationIssue] = []
    if not path.exists():
        return [_issue(case_id, "ERROR", "FILE_MISSING", "Input file does not exist", path)]
    if path.stat().st_size == 0:
        return [_issue(case_id, "ERROR", "FILE_EMPTY", "Input file is empty", path)]

    try:
        deck = InpDeck.from_file(path)
    except OSError as exc:
        return [_issue(case_id, "ERROR", "FILE_UNREADABLE", str(exc), path)]

    base = path.parent if project_root is None else project_root
    for include in deck.find_includes():
        include_path = include if include.is_absolute() else base / include
        if not include_path.exists():
            issues.append(
                _issue(
                    case_id,
                    "ERROR",
                    "INCLUDE_MISSING",
                    f"Included file does not exist: {include}",
                    path,
                )
            )

    issues.extend(_validate_pairs(deck, path, case_id))
    issues.extend(_validate_steps(deck, path, case_id))
    issues.extend(_validate_materials(deck, path, case_id))
    issues.extend(_validate_sections(deck, path, case_id))
    issues.extend(
        _validate_profiles(
            path,
            case_id,
            material_profile,
            load_profile,
            post_profile,
            materials,
            loads,
            post_profiles,
        )
    )

    if load_profile and loads:
        load = (loads.get("loads") or {}).get(load_profile)
        if load:
            issues.extend(_validate_load_target(deck, path, case_id, load))

    if not deck.find_blocks("output"):
        issues.append(
            _issue(case_id, "WARNING", "OUTPUT_MISSING", "No *Output request found", path)
        )
    if not deck.find_blocks("restart"):
        issues.append(
            _issue(case_id, "WARNING", "RESTART_MISSING", "No *Restart request found", path)
        )
    return issues


def _validate_pairs(deck: InpDeck, path: Path, case_id: str) -> list[ValidationIssue]:
    pairs = {
        "part": "end part",
        "assembly": "end assembly",
        "instance": "end instance",
        "step": "end step",
    }
    issues: list[ValidationIssue] = []
    for start, end in pairs.items():
        stack: list[KeywordBlock] = []
        for block in deck.blocks:
            if block.keyword == start:
                stack.append(block)
            elif block.keyword == end:
                if stack:
                    stack.pop()
                else:
                    issues.append(
                        _issue(
                            case_id,
                            "ERROR",
                            "UNMATCHED_END",
                            f"*{end.title()} without matching *{start.title()}",
                            path,
                            block.start_line,
                        )
                    )
        for block in stack:
            issues.append(
                _issue(
                    case_id,
                    "ERROR",
                    "UNCLOSED_BLOCK",
                    f"*{start.title()} is not closed by *{end.title()}",
                    path,
                    block.start_line,
                )
            )
    return issues


def _validate_steps(deck: InpDeck, path: Path, case_id: str) -> list[ValidationIssue]:
    procedure_keywords = {
        "static",
        "dynamic",
        "heat transfer",
        "frequency",
        "buckle",
        "coupled temperature-displacement",
    }
    issues: list[ValidationIssue] = []
    for step in deck.find_steps():
        end_line = next(
            (
                block.start_line
                for block in deck.blocks
                if block.keyword == "end step" and block.start_line > step.start_line
            ),
            None,
        )
        if end_line is None:
            continue
        has_procedure = any(
            block.start_line > step.start_line
            and block.start_line < end_line
            and block.keyword in procedure_keywords
            for block in deck.blocks
        )
        if not has_procedure:
            issues.append(
                _issue(
                    case_id,
                    "ERROR",
                    "STEP_PROCEDURE_MISSING",
                    "Step has no supported analysis procedure keyword",
                    path,
                    step.start_line,
                )
            )
    return issues


def _validate_materials(deck: InpDeck, path: Path, case_id: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen: dict[str, KeywordBlock] = {}
    for block in deck.find_blocks("material"):
        name = block.params.get("name")
        if not name:
            issues.append(
                _issue(
                    case_id,
                    "ERROR",
                    "MATERIAL_NAME_MISSING",
                    "Material has no name",
                    path,
                    block.start_line,
                )
            )
            continue
        key = name.lower()
        if key in seen:
            issues.append(
                _issue(
                    case_id,
                    "ERROR",
                    "MATERIAL_DUPLICATE",
                    f"Duplicate material name: {name}",
                    path,
                    block.start_line,
                )
            )
        seen[key] = block
    return issues


def _validate_sections(deck: InpDeck, path: Path, case_id: str) -> list[ValidationIssue]:
    material_names = {
        (block.params.get("name") or "").lower()
        for block in deck.find_blocks("material")
        if block.params.get("name")
    }
    issues: list[ValidationIssue] = []
    for keyword in ("solid section", "shell section", "beam section"):
        for block in deck.find_blocks(keyword):
            material = block.params.get("material")
            if material and material.lower() not in material_names:
                issues.append(
                    _issue(
                        case_id,
                        "ERROR",
                        "SECTION_MATERIAL_MISSING",
                        f"Section references undefined material: {material}",
                        path,
                        block.start_line,
                    )
                )
    return issues


def _validate_profiles(
    path: Path,
    case_id: str,
    material_profile: str | None,
    load_profile: str | None,
    post_profile: str | None,
    materials: dict[str, Any] | None,
    loads: dict[str, Any] | None,
    post_profiles: dict[str, Any] | None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    material_defs = (materials or {}).get("materials") or {}
    load_defs = (loads or {}).get("loads") or {}
    post_defs = (post_profiles or {}).get("profiles") or {}
    if material_profile and material_profile not in material_defs:
        issues.append(_issue(case_id, "ERROR", "MATERIAL_PROFILE_MISSING", material_profile, path))
    if load_profile and load_profile not in load_defs:
        issues.append(_issue(case_id, "ERROR", "LOAD_PROFILE_MISSING", load_profile, path))
    if post_profile and post_profile not in post_defs:
        issues.append(_issue(case_id, "ERROR", "POST_PROFILE_MISSING", post_profile, path))

    for name, material in material_defs.items():
        density = material.get("density")
        elastic = material.get("elastic") or {}
        if density is not None and float(density) <= 0:
            issues.append(_issue(case_id, "ERROR", "DENSITY_NONPOSITIVE", name, path))
        if elastic:
            if float(elastic.get("E", 0)) <= 0:
                issues.append(_issue(case_id, "ERROR", "ELASTIC_E_NONPOSITIVE", name, path))
            nu = float(elastic.get("nu", 0))
            if not 0 < nu < 0.5:
                issues.append(_issue(case_id, "ERROR", "ELASTIC_NU_INVALID", name, path))
        plastic = material.get("plastic") or []
        strains = [float(item[1]) for item in plastic]
        if any(later < earlier for earlier, later in zip(strains, strains[1:], strict=False)):
            issues.append(_issue(case_id, "ERROR", "PLASTIC_STRAIN_NOT_MONOTONIC", name, path))
    return issues


def _validate_load_target(
    deck: InpDeck, path: Path, case_id: str, load: dict[str, Any]
) -> list[ValidationIssue]:
    sets = deck.find_sets()
    surfaces = deck.find_surfaces()
    issues: list[ValidationIssue] = []
    target_set = load.get("target_set")
    if target_set and target_set not in sets["nset"] and target_set not in sets["elset"]:
        issues.append(
            _issue(case_id, "ERROR", "LOAD_TARGET_SET_MISSING", f"Missing set: {target_set}", path)
        )
    surface = load.get("surface")
    if surface and surface not in surfaces:
        issues.append(
            _issue(case_id, "ERROR", "LOAD_SURFACE_MISSING", f"Missing surface: {surface}", path)
        )
    return issues


def validate_project(config: ProjectConfig, static: bool = True) -> list[ValidationIssue]:
    """Validate all cases in a project and write validation_report.csv."""

    del static
    rows = read_cases_csv(config.root / "configs" / "cases.csv")
    db.init_db(config.db_path)
    issues: list[ValidationIssue] = []
    for row in rows:
        source = config.root / row["source_inp"]
        case_issues = validate_inp_file(
            source,
            row["case_id"],
            config.root,
            row.get("material_profile"),
            row.get("load_profile"),
            row.get("post_profile"),
            config.materials,
            config.loads,
            config.postprocess,
        )
        issues.extend(case_issues)
        status = (
            Status.STATIC_FAILED.value
            if any(i.severity == "ERROR" for i in case_issues)
            else Status.STATIC_CHECKED.value
        )
        db.update_status(config.db_path, row["case_id"], status)

    report_path = config.reports_dir / "validation_report.csv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["case_id", "severity", "code", "message", "path", "line", "context"],
        )
        writer.writeheader()
        for issue in issues:
            writer.writerow(issue.__dict__)
    return issues

