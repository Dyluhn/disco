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

_ENV_DIR = "PMX_SKILLS_DIR"
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

    def to_markdown(self) -> str:
        """Serialize to a .md file with frontmatter. The body is preserved verbatim."""
        fm = (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"enabled: {'true' if self.enabled else 'false'}\n"
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

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._dir = Path(path or os.environ.get(_ENV_DIR, _DEFAULT_DIR))

    @property
    def directory(self) -> Path:
        return self._dir

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
        f = self._dir / f"{skill_id}.md"
        return self._read_file(f) if f.is_file() else None

    def enabled(self) -> list[Skill]:
        """Only the enabled skills — what the agent's context injection reads."""
        return [s for s in self.list() if s.enabled]

    def save(self, skill: Skill) -> Skill:
        """Create or overwrite a skill file (atomic write). Returns the saved skill."""
        self._dir.mkdir(parents=True, exist_ok=True)
        f = self._dir / f"{skill.id}.md"
        tmp = f.with_suffix(".md.tmp")
        tmp.write_text(skill.to_markdown(), encoding="utf-8")
        tmp.replace(f)
        return skill

    def create(self, name: str, description: str = "", body: str = "", enabled: bool = True) -> Skill:
        """Create a new skill, deriving a unique id from the name."""
        base = slugify(name)
        skill_id = base
        i = 2
        while (self._dir / f"{skill_id}.md").exists():
            skill_id = f"{base}-{i}"
            i += 1
        return self.save(
            Skill(id=skill_id, name=name, description=description, body=body, enabled=enabled)
        )

    def delete(self, skill_id: str) -> bool:
        """Delete a skill file. Returns True if it existed."""
        f = self._dir / f"{skill_id}.md"
        if f.is_file():
            f.unlink()
            return True
        return False

    def _read_file(self, f: Path) -> Skill | None:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            return None
        fm, body = _parse_frontmatter(text)
        return Skill(
            id=f.stem,
            name=fm.get("name", f.stem),
            description=fm.get("description", ""),
            enabled=fm.get("enabled", "true").lower() != "false",
            body=body.strip("\n"),
        )


def render_skills_for_prompt(skills: list[Skill]) -> str:
    """Render enabled skills as a system-prompt block the agent reads. Empty
    string when there are none (so the prompt is unchanged for users with no
    skills). Each skill is a titled section; the body is the agent-facing
    instructions."""
    enabled = [s for s in skills if s.enabled and s.body.strip()]
    if not enabled:
        return ""
    parts = [
        "The user has configured these SKILLS — reusable instructions you should "
        "follow when relevant to the task. Treat them as standing guidance:",
    ]
    for s in enabled:
        header = f"## Skill: {s.name}"
        if s.description:
            header += f"\n_{s.description}_"
        parts.append(f"{header}\n\n{s.body.strip()}")
    return "\n\n".join(parts)
