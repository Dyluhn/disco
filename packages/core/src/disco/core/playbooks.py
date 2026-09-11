"""Playbooks — bundled integration recipes the build agent reads on demand.

A playbook is one markdown file under ``playbooks_data/<id>.md``: a ``# Title`` line, a
``> summary`` line the tool catalog shows, then the recipe (stack, pinned
dependencies, env var NAMES, code shape, verification, security notes). Packs
are data, not code: nothing here executes them. Loaded via ``importlib.resources``
so they ship in the wheel like the workflow prompt packs.
"""

from __future__ import annotations

from importlib.resources import files as _res_files
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_PACKS_PKG = "disco.core.playbooks_data"


class Playbook(BaseModel):
    """One parsed playbook: id, title, one-line summary, full markdown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack_id: str
    title: str
    summary: str
    raw: str


def parse_playbook(pack_id: str, text: str) -> Playbook:
    """Parse ``# Title`` + ``> summary`` from the top of the markdown; both are required."""
    lines = text.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), "")
    summary = next((line[2:].strip() for line in lines if line.startswith("> ")), "")
    if not title or not summary:
        raise ValueError(f"playbook {pack_id!r} needs a '# Title' and a '> summary' line")
    return Playbook(pack_id=pack_id, title=title, summary=summary, raw=text)


class PlaybookRegistry:
    """Loads and caches the bundled playbooks (an explicit ``pack_dir`` overrides for tests)."""

    def __init__(self, pack_dir: Path | None = None) -> None:
        self._dir = pack_dir
        self._packs: dict[str, Playbook] | None = None

    def _load(self) -> dict[str, Playbook]:
        if self._packs is None:
            root = self._dir if self._dir is not None else _res_files(_PACKS_PKG)
            packs = {
                entry.name[:-3]: parse_playbook(entry.name[:-3], entry.read_text("utf-8"))
                for entry in sorted(root.iterdir(), key=lambda e: e.name)
                if entry.name.endswith(".md")
            }
            self._packs = packs
        return self._packs

    def ids(self) -> tuple[str, ...]:
        return tuple(self._load())

    def get(self, pack_id: str) -> Playbook | None:
        return self._load().get(pack_id)

    def index(self) -> str:
        """The catalog: one ``id — summary`` line per playbook, in id order."""
        return "\n".join(f"- {p.pack_id} — {p.summary}" for p in self._load().values())

    _default: PlaybookRegistry | None = None

    @classmethod
    def default(cls) -> PlaybookRegistry:
        if cls._default is None:
            cls._default = cls()
        return cls._default


__all__ = ["Playbook", "PlaybookRegistry", "parse_playbook"]
