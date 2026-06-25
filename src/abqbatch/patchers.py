"""Material, load, restart, and output patch helpers for INP generation."""

from __future__ import annotations

from typing import Any

from abqbatch.inp_parser import InpDeck

RESTART_INC = """*Restart, write, frequency=10
"""

OUTPUT_INC = """*Output, field
*Node Output
U, RF
*Element Output
S, E, PEEQ
*Output, history
*Energy Output
ALLIE, ALLKE, ALLSE, ALLWK
"""


def generate_material_block(profile: dict[str, Any]) -> str:
    """Render an Abaqus material block from a material profile."""

    name = profile["name"]
    density = profile["density"]
    elastic = profile["elastic"]
    lines = [
        f"*Material, name={name}",
        "*Density",
        f"{density}",
        "*Elastic",
        f"{elastic['E']}, {elastic['nu']}",
    ]
    if profile.get("model") == "elastic_plastic":
        lines.append("*Plastic")
        for yield_stress, plastic_strain in profile.get("plastic", []):
            lines.append(f"{yield_stress}, {plastic_strain}")
    return "\n".join(lines) + "\n"


def generate_load_block(profile: dict[str, Any]) -> str:
    """Render an Abaqus load block from a load profile."""

    amp = profile.get("amplitude") or {"name": "Amp-Ramp", "data": [[0.0, 0.0], [1.0, 1.0]]}
    lines = [f"*Amplitude, name={amp['name']}"]
    lines.extend(f"{t}, {a}" for t, a in amp.get("data", []))
    load_type = profile["type"]
    if load_type == "displacement_control":
        lines.extend(
            [
                f"*Boundary, amplitude={amp['name']}",
                f"{profile['target_set']}, {profile['dof']}, {profile['dof']}, {profile['value']}",
            ]
        )
    elif load_type == "force_control":
        lines.extend(
            [
                f"*Cload, amplitude={amp['name']}",
                f"{profile['target_set']}, {profile['dof']}, {profile['value']}",
            ]
        )
    elif load_type == "pressure":
        lines.extend(
            [
                f"*Dsload, amplitude={amp['name']}",
                f"{profile['surface']}, P, {profile['value']}",
            ]
        )
    else:
        raise ValueError(f"Unsupported load type: {load_type}")
    return "\n".join(lines) + "\n"


def replace_anchor(text: str, begin_marker: str, end_marker: str, replacement: str) -> str | None:
    """Replace content between two marker lines, preserving the marker lines."""

    lines = text.splitlines()
    begin = next((i for i, line in enumerate(lines) if begin_marker in line), None)
    end = next(
        (
            i
            for i, line in enumerate(lines)
            if end_marker in line and begin is not None and i > begin
        ),
        None,
    )
    if begin is None or end is None:
        return None
    replacement_lines = replacement.rstrip("\n").splitlines()
    new_lines = lines[: begin + 1] + replacement_lines + lines[end:]
    return "\n".join(new_lines) + "\n"


def patch_material(deck: InpDeck, material_name: str, include_text: str) -> None:
    """Replace a material anchor or material block with include text."""

    text = deck.to_text()
    anchored = replace_anchor(
        text,
        f"** @BEGIN AUTO_MATERIAL {material_name}",
        f"** @END AUTO_MATERIAL {material_name}",
        include_text,
    )
    if anchored is not None:
        deck.lines = anchored.splitlines()
        deck.refresh()
        return

    block = deck.find_material(material_name)
    if block is not None:
        deck.replace_block(block, include_text)
        return

    first_step = deck.find_steps()[0] if deck.find_steps() else None
    insert_at = first_step.start_line - 1 if first_step else len(deck.lines)
    deck.lines[insert_at:insert_at] = include_text.rstrip("\n").splitlines()
    deck.refresh()


def patch_load(deck: InpDeck, step_name: str, include_text: str) -> None:
    """Replace a load anchor or insert load include before the step end."""

    text = deck.to_text()
    anchored = replace_anchor(text, "** @BEGIN AUTO_LOAD", "** @END AUTO_LOAD", include_text)
    if anchored is not None:
        deck.lines = anchored.splitlines()
        deck.refresh()
        return
    deck.insert_before_end_step(step_name, include_text)


def ensure_restart(
    deck: InpDeck, step_name: str, include_text: str = "*Include, input=restart.inc\n"
) -> None:
    """Insert restart include when the deck has no restart keyword."""

    if deck.find_blocks("restart"):
        return
    deck.insert_before_end_step(step_name, include_text)


def ensure_output(
    deck: InpDeck, step_name: str, include_text: str = "*Include, input=output.inc\n"
) -> None:
    """Insert output include when the deck has no output keyword."""

    if deck.find_blocks("output"):
        return
    deck.insert_before_end_step(step_name, include_text)
