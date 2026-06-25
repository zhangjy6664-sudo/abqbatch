from __future__ import annotations

from pathlib import Path

from abqbatch.abaqus_cmd import AbaqusCommand
from abqbatch.restart import build_explicit_recover_command, build_standard_restart_command


def test_solve_command() -> None:
    cmd = AbaqusCommand(job="C000001", input_file=Path("generated.inp"), cpus=8, memory="90%")

    assert (
        cmd.to_shell_string()
        == "abaqus job=C000001 input=generated.inp cpus=8 memory=90% interactive"
    )


def test_datacheck_command() -> None:
    cmd = AbaqusCommand(job="C000001", input_file=Path("generated.inp"), datacheck=True)

    assert cmd.to_shell_string() == "abaqus job=C000001 input=generated.inp datacheck interactive"


def test_restart_command() -> None:
    cmd = build_standard_restart_command("C000001", "C000001", "C000001_restart_001")

    assert cmd.to_shell_string() == "abaqus job=C000001_restart_001 oldjob=C000001 interactive"


def test_recover_command() -> None:
    cmd = build_explicit_recover_command("C000001")

    assert cmd.to_shell_string() == "abaqus job=C000001 recover interactive"
