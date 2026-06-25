from __future__ import annotations

from abqbatch.inp_parser import InpDeck


def test_parser_handles_keywords_material_step_include_and_sets() -> None:
    text = """** comment
*HEADING
demo
*Include, input=parts/mesh.inc
*Material, NAME=Steel
*Elastic
1., 0.3
*Nset, NSET=RP_LOAD
1, 2
*Step, Name=Step-1
*Static
0.1, 1.
*End Step
"""
    deck = InpDeck.from_text(text)

    assert deck.find_blocks("heading")
    assert deck.find_blocks("material")[0].params["name"] == "Steel"
    assert deck.find_material("steel") is not None
    assert deck.find_steps()[0].params["name"] == "Step-1"
    assert deck.find_includes()[0].as_posix() == "parts/mesh.inc"
    assert "RP_LOAD" in deck.find_sets()["nset"]
    assert deck.blocks[0].keyword == "heading"
