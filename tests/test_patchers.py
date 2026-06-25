from __future__ import annotations

from pathlib import Path

from abqbatch.inp_parser import InpDeck
from abqbatch.patchers import patch_load, patch_material


def test_anchor_replacement_material() -> None:
    deck = InpDeck.from_text(
        """*Heading
** @BEGIN AUTO_MATERIAL Steel
old
** @END AUTO_MATERIAL Steel
*Step, name=Step-1
*Static
*End Step
"""
    )

    patch_material(deck, "Steel", "*Include, input=material.inc\n")

    assert "*Include, input=material.inc" in deck.to_text()
    assert "old" not in deck.to_text()


def test_block_replacement_material() -> None:
    deck = InpDeck.from_text(
        """*Heading
*Material, name=Steel
*Elastic
1., 0.3
*Step, name=Step-1
*Static
*End Step
"""
    )

    patch_material(deck, "Steel", "*Include, input=material.inc\n")

    assert "*Material, name=Steel" not in deck.to_text()
    assert "*Include, input=material.inc" in deck.to_text()


def test_load_inserted_before_step_end() -> None:
    deck = InpDeck.from_text("*Step, name=Step-1\n*Static\n*End Step\n")

    patch_load(deck, "Step-1", "*Include, input=load.inc\n")

    text = deck.to_text()
    assert text.index("*Include, input=load.inc") < text.index("*End Step")


def test_original_file_is_not_modified(workspace_tmp: Path) -> None:
    source = workspace_tmp / "source.inp"
    original_text = "*Material, name=Steel\n*Elastic\n1., 0.3\n"
    source.write_text(original_text, encoding="utf-8")
    deck = InpDeck.from_file(source)

    patch_material(deck, "Steel", "*Include, input=material.inc\n")

    assert source.read_text(encoding="utf-8") == original_text
