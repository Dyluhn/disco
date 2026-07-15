"""Skills — reusable instruction modules handed to the agent, Claude-Code style.

A Skill is a markdown file with frontmatter: a `name`, a one-line `description`,
an `enabled` flag, and a markdown body of instructions the model reads and acts
on (e.g. "How to fetch stock data from the Yahoo v8 endpoint", "Our house style
for React components"). Enabled skills are injected into the Build agent's system
context so it can follow them without the user re-explaining each run.

Persisted as one `.md` file per skill in a directory (`PMX_SKILLS_DIR`, default
`./skills`), so they survive restarts AND can be hand-edited / version-controlled
outside the app — exactly like Claude Code's SKILL.md files. The frontmatter is a
minimal `--- key: value ---` block (no YAML dependency); the body is everything
after the closing fence.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from .env import disco_env

_ENV_DIR = "DISCO_SKILLS_DIR"
_DEFAULT_DIR = "skills"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def slugify(name: str) -> str:
    """A filesystem-safe id from a skill name. Stable + readable."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "skill"


class Skill(BaseModel):
    """One reusable instruction module. `id` is the file slug; `body` is the
    markdown the agent reads. `enabled` gates whether it's injected into the
    agent's context."""

    id: str
    name: str
    description: str = ""
    enabled: bool = True
    body: str = Field(default="", description="Markdown instructions for the agent.")
    # Cluster 4 lazy injection: an optional glob (e.g. "src/**/*.tsx" or
    # "**/*.css"). When set, the skill's FULL body is injected only when the
    # agent is touching a matching path; otherwise only a one-line manifest
    # entry is shown (Claude-Code CLAUDE.md style). Empty scope → always full.
    scope: str = ""
    # Which SURFACES this skill applies to (e.g. ["build"], ["agent"], or both).
    # EMPTY = applies to all surfaces (back-compat: a skill file with no surfaces
    # line keeps its old "everywhere" behavior). Lets a "house style" skill target
    # the agent while a "screenshot this page hourly" skill targets build, instead
    # of both polluting every surface's prompt.
    surfaces: list[str] = Field(default_factory=list)

    def applies_to_surface(self, surface: str | None) -> bool:
        """True if this skill should be injected for `surface`. Empty `surfaces`
        (or no surface in hand) → applies everywhere."""
        return not self.surfaces or surface is None or surface in self.surfaces

    def to_markdown(self) -> str:
        """Serialize to a .md file with frontmatter. The body is preserved verbatim."""
        scope_line = f"scope: {self.scope}\n" if self.scope else ""
        surfaces_line = f"surfaces: {', '.join(self.surfaces)}\n" if self.surfaces else ""
        fm = (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"enabled: {'true' if self.enabled else 'false'}\n"
            f"{scope_line}"
            f"{surfaces_line}"
            "---\n"
        )
        return fm + (self.body or "")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a skill .md into (frontmatter dict, body). Tolerant: a file with no
    frontmatter returns ({}, whole_text). Values are taken verbatim after the
    first colon; keys are lowercased."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ({}, text)
    raw_fm, body = m.group(1), m.group(2)
    fm: dict[str, str] = {}
    for line in raw_fm.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip().lower()] = value.strip()
    return (fm, body)


class SkillStore:
    """CRUD over skill `.md` files in a directory. One file per skill, named
    `<id>.md`. The directory is created lazily on first write."""

    # Hard cap on a skill body's size — these are injected verbatim into every
    # Build request's system prompt, so an unbounded body would blow the context
    # window and inflate cost. 64 KB is generous for instructions while bounding
    # the blast radius.
    MAX_BODY_BYTES = 64 * 1024

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._dir = Path(path or disco_env("SKILLS_DIR", _DEFAULT_DIR))

    @property
    def directory(self) -> Path:
        return self._dir

    def _path_for(self, skill_id: str) -> Path | None:
        """Resolve a skill id to its file path, REFUSING any id that would escape
        the skills directory (path traversal). The id must be a clean slug —
        anything else (``..``, slashes, absolute paths) is rejected by requiring
        the slug to be idempotent AND the resolved path to stay inside the dir.
        Returns None for an unsafe id; callers treat that as 'not found'."""
        # A valid skill id is its own slug — reject ../, slashes, dots, etc.
        if not skill_id or slugify(skill_id) != skill_id:
            return None
        candidate = (self._dir / f"{skill_id}.md").resolve()
        try:
            base = self._dir.resolve()
        except OSError:
            return None
        # The resolved path must live directly under the skills directory.
        if candidate.parent != base:
            return None
        return candidate

    def list(self) -> list[Skill]:
        """All skills, sorted by name. Missing directory → empty list (not an error)."""
        if not self._dir.is_dir():
            return []
        skills: list[Skill] = []
        for f in sorted(self._dir.glob("*.md")):
            skill = self._read_file(f)
            if skill is not None:
                skills.append(skill)
        skills.sort(key=lambda s: s.name.lower())
        return skills

    def get(self, skill_id: str) -> Skill | None:
        f = self._path_for(skill_id)  # rejects path-traversal ids → None
        return self._read_file(f) if (f is not None and f.is_file()) else None

    def enabled(self) -> list[Skill]:
        """Only the enabled skills — what the agent's context injection reads."""
        return [s for s in self.list() if s.enabled]

    def save(self, skill: Skill) -> Skill:
        """Create or overwrite a skill file (atomic write). Returns the saved skill.
        Refuses an unsafe id (path traversal) and over-cap bodies."""
        f = self._path_for(skill.id)
        if f is None:
            raise ValueError(f"unsafe skill id {skill.id!r}")
        body = (skill.body or "")[: self.MAX_BODY_BYTES]
        skill = skill.model_copy(update={"body": body})
        self._dir.mkdir(parents=True, exist_ok=True)
        # Unique temp name so two concurrent saves of the same id can't interleave
        # bytes on a shared scratch file before the atomic rename.
        tmp = f.with_suffix(f".md.{os.getpid()}.{id(skill)}.tmp")
        tmp.write_text(skill.to_markdown(), encoding="utf-8")
        tmp.replace(f)
        return skill

    def create(
        self,
        name: str,
        description: str = "",
        body: str = "",
        enabled: bool = True,
        surfaces: list[str] | None = None,
    ) -> Skill:
        """Create a new skill, deriving a unique id from the name."""
        base = slugify(name)
        skill_id = base
        i = 2
        while (self._dir / f"{skill_id}.md").exists():
            skill_id = f"{base}-{i}"
            i += 1
        return self.save(
            Skill(
                id=skill_id,
                name=name,
                description=description,
                body=body,
                enabled=enabled,
                surfaces=surfaces or [],
            )
        )

    def delete(self, skill_id: str) -> bool:
        """Delete a skill file. Returns True if it existed. Refuses unsafe ids."""
        f = self._path_for(skill_id)  # rejects path-traversal ids → None
        if f is not None and f.is_file():
            f.unlink()
            return True
        return False

    def _read_file(self, f: Path) -> Skill | None:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            return None
        fm, body = _parse_frontmatter(text)
        surfaces = [s.strip() for s in fm.get("surfaces", "").split(",") if s.strip()]
        return Skill(
            id=f.stem,
            name=fm.get("name", f.stem),
            description=fm.get("description", ""),
            enabled=fm.get("enabled", "true").lower() != "false",
            scope=fm.get("scope", ""),
            surfaces=surfaces,
            body=body.strip("\n"),
        )


def render_skills_for_prompt(
    skills: list[Skill], *, surface: str | None = None, active_paths: list[str] | None = None
) -> str:
    """Render enabled skills as a system-prompt block. Empty string when there
    are none.

    `surface` filters to skills that apply to it (a skill's `surfaces` list; empty
    = everywhere). None → no surface filter (every enabled skill), the back-compat
    default for callers that don't run on a specific surface.

    Cluster 4 LAZY injection: a SCOPED skill (with a `scope` glob) shows only a
    one-line manifest entry by default, and its FULL body only when one of
    `active_paths` matches its glob — so N full bodies every turn becomes
    1 manifest + only-the-relevant-bodies (the CLAUDE.md table-of-contents +
    path-triggered detail pattern). UNSCOPED skills always show their full body
    (back-compat). `active_paths` is the set of workspace paths the agent is
    currently touching; None/empty → only unscoped skills render in full.
    """
    import fnmatch

    enabled = [s for s in skills if s.enabled and s.body.strip() and s.applies_to_surface(surface)]
    if not enabled:
        return ""
    paths = active_paths or []

    def in_scope(s: Skill) -> bool:
        if not s.scope:
            return True  # unscoped → always full
        return any(fnmatch.fnmatch(p, s.scope) for p in paths)

    full = [s for s in enabled if in_scope(s)]
    manifest_only = [s for s in enabled if not in_scope(s)]

    parts = [
        "The user has configured these SKILLS — reusable instructions you should "
        "follow when relevant to the task. Treat them as standing guidance:",
    ]
    for s in full:
        header = f"## Skill: {s.name}"
        if s.description:
            header += f"\n_{s.description}_"
        parts.append(f"{header}\n\n{s.body.strip()}")
    if manifest_only:
        lines = [
            f"  • {s.name}"
            + (f" — {s.description}" if s.description else "")
            + (f"  (applies to {s.scope})" if s.scope else "")
            for s in manifest_only
        ]
        parts.append(
            "Other skills available (their detail loads when you work on a matching "
            "file):\n" + "\n".join(lines)
        )
    return "\n\n".join(parts)
