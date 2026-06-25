"""Abaqus/Standard restart and Abaqus/Explicit recover helpers."""

from __future__ import annotations

from pathlib import Path

from abqbatch import db
from abqbatch.abaqus_cmd import AbaqusCommand
from abqbatch.exceptions import Status
from abqbatch.inp_parser import InpDeck


def ensure_restart_write(inp_path: Path) -> bool:
    """Insert *Restart, write into an INP file if it is absent."""

    deck = InpDeck.from_file(inp_path)
    for block in deck.find_blocks("restart"):
        if "write" in block.params:
            return False
    first_step = deck.find_steps()[0] if deck.find_steps() else None
    if first_step is None:
        deck.lines.append("*Restart, write, frequency=10")
    else:
        deck.insert_before_end_step(
            first_step.params.get("name"), "*Restart, write, frequency=10\n"
        )
    deck.write(inp_path)
    return True


def build_standard_restart_command(case_id: str, oldjob: str, newjob: str) -> AbaqusCommand:
    """Build a Standard restart command."""

    del case_id
    return AbaqusCommand(job=newjob, oldjob=oldjob, interactive=True)


def build_explicit_recover_command(job: str) -> AbaqusCommand:
    """Build an Explicit recover command."""

    return AbaqusCommand(job=job, recover=True, interactive=True)


def mark_restart_ready(db_path: Path, case_id: str) -> None:
    """Mark a case as ready for a Standard restart."""

    db.update_status(db_path, case_id, Status.RESTART_READY.value)


def mark_recover_ready(db_path: Path, case_id: str) -> None:
    """Mark a case as ready for an Explicit recover."""

    db.update_status(db_path, case_id, Status.RECOVER_READY.value)
