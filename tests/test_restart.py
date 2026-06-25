from __future__ import annotations

from pathlib import Path

from abqbatch.restart import (
    build_explicit_recover_command,
    build_standard_restart_command,
    ensure_restart_write,
)


def test_ensure_restart_write_inserts_once(workspace_tmp: Path) -> None:
    inp = workspace_tmp / "a.inp"
    inp.write_text("*Step, name=Step-1\n*Static\n*End Step\n", encoding="utf-8")

    assert ensure_restart_write(inp) is True
    assert ensure_restart_write(inp) is False
    text = inp.read_text(encoding="utf-8")
    assert text.count("*Restart, write") == 1


def test_restart_and_recover_commands() -> None:
    restart = build_standard_restart_command("C000001", "old", "new")
    recover = build_explicit_recover_command("old")

    assert restart.to_shell_string() == "abaqus job=new oldjob=old interactive"
    assert recover.to_shell_string() == "abaqus job=old recover interactive"
