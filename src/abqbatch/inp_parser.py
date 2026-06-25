"""A conservative keyword block parser for Abaqus .inp files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def normalize_keyword(keyword: str) -> str:
    """Normalize an Abaqus keyword for case-insensitive matching."""

    return " ".join(keyword.strip().lstrip("*").lower().split())


@dataclass(frozen=True)
class KeywordBlock:
    """One Abaqus keyword block with original line numbers and data lines."""

    keyword: str
    params: dict[str, str | None]
    start_line: int
    end_line: int
    header: str
    data_lines: list[str] = field(default_factory=list)

    @property
    def name(self) -> str | None:
        return self.params.get("name")


class InpDeck:
    """A mutable Abaqus input deck represented as keyword blocks plus raw lines."""

    def __init__(self, lines: list[str], path: Path | None = None) -> None:
        self.lines = lines
        self.path = path
        self.blocks = self._parse_blocks()

    @classmethod
    def from_file(cls, path: Path) -> InpDeck:
        """Read and parse an input deck from disk."""

        return cls(path.read_text(encoding="utf-8", errors="replace").splitlines(), path)

    @classmethod
    def from_text(cls, text: str) -> InpDeck:
        """Parse an input deck from a text string."""

        return cls(text.splitlines())

    def _parse_blocks(self) -> list[KeywordBlock]:
        starts: list[int] = []
        for index, line in enumerate(self.lines):
            stripped = line.lstrip()
            if stripped.startswith("*") and not stripped.startswith("**"):
                starts.append(index)

        blocks: list[KeywordBlock] = []
        for pos, start in enumerate(starts):
            stop = starts[pos + 1] if pos + 1 < len(starts) else len(self.lines)
            header = self.lines[start]
            keyword, params = parse_keyword_line(header)
            blocks.append(
                KeywordBlock(
                    keyword=keyword,
                    params=params,
                    start_line=start + 1,
                    end_line=stop,
                    header=header,
                    data_lines=self.lines[start + 1 : stop],
                )
            )
        return blocks

    def refresh(self) -> None:
        """Reparse blocks after line mutations."""

        self.blocks = self._parse_blocks()

    def find_blocks(self, keyword: str) -> list[KeywordBlock]:
        """Find all blocks matching a keyword case-insensitively."""

        wanted = normalize_keyword(keyword)
        return [block for block in self.blocks if block.keyword == wanted]

    def find_material(self, name: str) -> KeywordBlock | None:
        """Find a material block by Abaqus material name."""

        wanted = name.lower()
        for block in self.find_blocks("material"):
            if (block.params.get("name") or "").lower() == wanted:
                return block
        return None

    def find_steps(self) -> list[KeywordBlock]:
        """Return all *Step blocks."""

        return self.find_blocks("step")

    def find_sets(self) -> dict[str, set[str]]:
        """Return named node and element sets."""

        sets: dict[str, set[str]] = {"nset": set(), "elset": set()}
        for keyword in ("nset", "elset"):
            for block in self.find_blocks(keyword):
                name = block.params.get(keyword) or block.params.get("name")
                if name:
                    sets[keyword].add(name)
        return sets

    def find_surfaces(self) -> set[str]:
        """Return named surfaces."""

        surfaces: set[str] = set()
        for block in self.find_blocks("surface"):
            name = block.params.get("name")
            if name:
                surfaces.add(name)
        return surfaces

    def find_includes(self) -> list[Path]:
        """Return paths referenced by *Include, input=... blocks."""

        includes: list[Path] = []
        for block in self.find_blocks("include"):
            value = block.params.get("input")
            if value:
                includes.append(Path(value))
        return includes

    def replace_block(self, block: KeywordBlock, new_text: str) -> None:
        """Replace a block with new text and reparse."""

        new_lines = new_text.rstrip("\n").splitlines()
        self.lines[block.start_line - 1 : block.end_line] = new_lines
        self.refresh()

    def insert_before_end_step(self, step_name: str | None, text: str) -> None:
        """Insert text immediately before the matching *End Step."""

        step_block = self._find_step_block(step_name)
        if step_block is None:
            raise ValueError(f"Step not found: {step_name}")
        end_step = self._find_end_step_after(step_block)
        if end_step is None:
            raise ValueError(f"Step is not closed: {step_name}")
        insert_at = end_step.start_line - 1
        insert_lines = text.rstrip("\n").splitlines()
        self.lines[insert_at:insert_at] = insert_lines
        self.refresh()

    def to_text(self) -> str:
        """Return the deck as newline-terminated text."""

        return "\n".join(self.lines) + "\n"

    def write(self, path: Path) -> None:
        """Write the deck to disk."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_text(), encoding="utf-8")

    def _find_step_block(self, step_name: str | None) -> KeywordBlock | None:
        steps = self.find_steps()
        if step_name is None:
            return steps[0] if steps else None
        wanted = step_name.lower()
        for block in steps:
            if (block.params.get("name") or "").lower() == wanted:
                return block
        return None

    def _find_end_step_after(self, step_block: KeywordBlock) -> KeywordBlock | None:
        for block in self.blocks:
            if block.start_line > step_block.start_line and block.keyword == "end step":
                return block
        return None


def parse_keyword_line(line: str) -> tuple[str, dict[str, str | None]]:
    """Parse an Abaqus keyword header into normalized keyword and parameters."""

    parts = [part.strip() for part in line.strip()[1:].split(",")]
    keyword = normalize_keyword(parts[0])
    params: dict[str, str | None] = {}
    for item in parts[1:]:
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            params[key.strip().lower()] = value.strip()
        else:
            params[item.strip().lower()] = None
    return keyword, params
