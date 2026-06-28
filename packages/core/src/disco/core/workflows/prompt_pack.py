"""WorkflowPromptPack format + loader (WPP-1).

A pack is a markdown file whose ``## <Section>`` headers carry the mode-specific
operating manual. The required sections are pinned so a pack can't silently omit a rule
the assembler (WPP-2) relies on. Packs are bundled under ``prompt_packs/<id>.md`` and
loaded via ``importlib.resources`` (works from src AND an installed wheel).
"""

from __future__ import annotations

import re
from importlib.resources import files as _res_files
from pathlib import Path

from pydantic import BaseModel, ConfigDict

# The sections every pack MUST define (the model's mode-specific operating manual).
REQUIRED_SECTIONS: tuple[str, ...] = (
    "role",
    "artifact_contract",
    "workflow_steps",
    "allowed_tools",
    "forbidden_tools",
    "targeted_edit_law",
    "preview_rule",
    "verify_rule",
    "export_rule",
    "context_policy",
    "done_criteria",
)

_PACKS_PKG = "disco.core.workflows.prompt_packs"
_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.strip().lower()).strip("_")


class PromptPack(BaseModel):
    """A parsed workflow prompt pack: id + named sections + the raw markdown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack_id: str
    sections: dict[str, str]
    raw: str

    def section(self, name: str) -> str:
        return self.sections.get(name, "")

    def render(self) -> str:
        """The full pack text to inject into the prompt (kernel-neutral; WPP-2)."""
        return self.raw

    def missing_required(self) -> tuple[str, ...]:
        return tuple(s for s in REQUIRED_SECTIONS if not self.sections.get(s, "").strip())


def parse_prompt_pack(pack_id: str, text: str) -> PromptPack:
    """Parse markdown into a PromptPack, keying sections by slugged ``## header``.

    Fence-aware (a ``## `` inside a ``` code block is NOT a header) and rejects a
    duplicate section slug (a pack that defines the same section twice is malformed —
    silent overwrite would hide a rule)."""
    sections: dict[str, str] = {}

    def _commit(slug: str | None, lines: list[str]) -> None:
        if slug is None:
            return
        if slug in sections:
            raise ValueError(f"prompt pack {pack_id!r}: duplicate section {slug!r}")
        sections[slug] = "\n".join(lines).strip()

    current: str | None = None
    buf: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if current is not None:
                buf.append(line)
            continue
        m = None if in_fence else _HEADER_RE.match(line)
        if m is not None:
            _commit(current, buf)
            current = _slug(m.group(1))
            buf = []
        elif current is not None:
            buf.append(line)
    _commit(current, buf)
    return PromptPack(pack_id=pack_id, sections=sections, raw=text)


class PromptPackRegistry:
    """Loads + caches the bundled prompt packs by id (importlib.resources by default;
    an explicit ``pack_dir`` overrides for tests/custom directories)."""

    def __init__(self, pack_dir: Path | None = None) -> None:
        self._dir = pack_dir
        self._cache: dict[str, PromptPack] = {}

    def _read(self, pack_id: str) -> str | None:
        if self._dir is not None:
            p = self._dir / f"{pack_id}.md"
            return p.read_text(encoding="utf-8") if p.is_file() else None
        res = _res_files(_PACKS_PKG) / f"{pack_id}.md"
        return res.read_text(encoding="utf-8") if res.is_file() else None

    def ids(self) -> frozenset[str]:
        if self._dir is not None:
            return frozenset(p.stem for p in self._dir.glob("*.md"))
        root = _res_files(_PACKS_PKG)
        return frozenset(r.name[:-3] for r in root.iterdir() if r.name.endswith(".md"))

    def get(self, pack_id: str) -> PromptPack | None:
        if pack_id in self._cache:
            return self._cache[pack_id]
        text = self._read(pack_id)
        if text is None:
            return None
        pack = parse_prompt_pack(pack_id, text)
        self._cache[pack_id] = pack
        return pack

    def require(self, pack_id: str) -> PromptPack:
        pack = self.get(pack_id)
        if pack is None:
            raise KeyError(f"no prompt pack {pack_id!r}")
        return pack
