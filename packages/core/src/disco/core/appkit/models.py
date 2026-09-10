"""AppSpec — the structured representation of an AppKit app (P4 / TOOL-1).

An app is described by an AppSpec (sections + design tokens + tweaks), NOT by hand-drawn
HTML. The specialized mutation tools (app_update_content / app_add_section / …) make
SMALL, semantic edits to this spec, and the deterministic renderer projects it to a
self-contained ``index.html``. Targeted edits touch one field/section — never a full
rewrite. Pure, frozen, serializable value objects + a pure renderer; no runtime imports.
"""

from __future__ import annotations

import html
import re
from typing import Any

from disco.core.appkit import semantic_metadata as _md  # canonical data-disco-* vocabulary (P8B)
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# Strip characters that could break OUT of a CSS declaration / the <style> element
# (a design-token value is interpolated into inline CSS, so it must not carry these).
_CSS_UNSAFE = re.compile(r"""[<>{};"'\\\n\r]""")


def _css_safe(v: str) -> str:
    return _CSS_UNSAFE.sub("", v)


# The section kinds the renderer knows how to draw.
SECTION_KINDS: frozenset[str] = frozenset(
    {"hero", "features", "lead_form", "about", "cta", "footer"}
)

# Default design tokens (a real app overrides via app_set_design).
DEFAULT_DESIGN: dict[str, str] = {
    "primary": "#4077a3",
    "accent": "#e2725b",
    "bg": "#fcfcfa",
    "fg": "#1a1a1a",
    "font": "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
}


class AppSection(BaseModel):
    """One section of the app (a hero, a lead form, …) with its editable fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    fields: dict[str, str] = {}

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in SECTION_KINDS:
            raise ValueError(f"unknown section kind {v!r}; known: {sorted(SECTION_KINDS)}")
        return v


class AppSpec(BaseModel):
    """The whole app: title + ordered sections + design tokens + tweak values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    sections: tuple[AppSection, ...] = ()
    design: dict[str, str] = {}
    tweaks: dict[str, Any] = {}

    @model_validator(mode="after")
    def _unique_section_ids(self) -> AppSpec:
        ids = [s.id for s in self.sections]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"section ids must be unique; duplicates: {dupes}")
        return self

    # --- lookups ---------------------------------------------------------------
    def _index_of(self, section_id: str) -> int:
        for i, s in enumerate(self.sections):
            if s.id == section_id:
                return i
        raise ValueError(f"no section {section_id!r}")

    def section(self, section_id: str) -> AppSection:
        return self.sections[self._index_of(section_id)]

    def resolved_design(self) -> dict[str, str]:
        return {**DEFAULT_DESIGN, **self.design}

    # --- pure mutations (return a NEW AppSpec) ---------------------------------
    def with_content(self, section_id: str, field: str, value: str) -> AppSpec:
        i = self._index_of(section_id)
        sec = self.sections[i]
        new_sec = sec.model_copy(update={"fields": {**sec.fields, field: value}})
        return self.model_copy(
            update={"sections": (*self.sections[:i], new_sec, *self.sections[i + 1 :])}
        )

    def with_section_added(self, section: AppSection, *, index: int | None = None) -> AppSpec:
        if any(s.id == section.id for s in self.sections):
            raise ValueError(f"duplicate section id {section.id!r}")
        secs = list(self.sections)
        secs.insert(len(secs) if index is None else index, section)
        return self.model_copy(update={"sections": tuple(secs)})

    def with_section_removed(self, section_id: str) -> AppSpec:
        i = self._index_of(section_id)
        return self.model_copy(update={"sections": (*self.sections[:i], *self.sections[i + 1 :])})

    def with_section_reordered(self, section_id: str, to_index: int) -> AppSpec:
        i = self._index_of(section_id)
        secs = list(self.sections)
        sec = secs.pop(i)
        to_index = max(0, min(to_index, len(secs)))
        secs.insert(to_index, sec)
        return self.model_copy(update={"sections": tuple(secs)})

    def with_design(self, key: str, value: str) -> AppSpec:
        return self.model_copy(update={"design": {**self.design, key: value}})

    def with_tweak(self, key: str, value: Any) -> AppSpec:
        return self.model_copy(update={"tweaks": {**self.tweaks, key: value}})


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _sec_attrs(sec: AppSection) -> str:
    """The data-disco-* attributes every section carries — emitted through the canonical
    vocabulary (P8B) so the names live in ONE place, plus a stable screen label."""
    return _md.attr(_md.DataDiscoAttr.SECTION, sec.id) + _md.attr(
        _md.DataDiscoAttr.SCREEN_LABEL, _md.screen_label_value(sec.id)
    )


def _field(name: str) -> str:
    return _md.attr(_md.DataDiscoAttr.FIELD, name)


def _render_section(sec: AppSection) -> str:
    f = sec.fields
    if sec.kind == "hero":
        return (
            f'<section class="hero"{_sec_attrs(sec)}>'
            f"<h1{_field('headline')}>{_esc(f.get('headline', ''))}</h1>"
            f"<p{_field('subhead')}>{_esc(f.get('subhead', ''))}</p>"
            f'<a class="cta" href="#lead"{_field("cta_text")}>'
            f"{_esc(f.get('cta_text', 'Get started'))}</a>"
            f"</section>"
        )
    if sec.kind == "lead_form":
        return (
            f'<section class="lead" id="lead"{_sec_attrs(sec)}>'
            f"<h2{_field('title')}>{_esc(f.get('title', 'Contact us'))}</h2>"
            f'<form method="post" action="/lead">'
            f'<input name="name" placeholder="Name" required>'
            f'<input name="email" type="email" placeholder="Email" required>'
            f'<button type="submit">{_esc(f.get("submit_text", "Send"))}</button>'
            f"</form></section>"
        )
    if sec.kind == "about":
        return (
            f'<section class="about"{_sec_attrs(sec)}>'
            f"<h2{_field('title')}>{_esc(f.get('title', 'About'))}</h2>"
            f"<p{_field('body')}>{_esc(f.get('body', ''))}</p></section>"
        )
    # features / cta / footer + any future-but-known kind: a generic titled block.
    return (
        f'<section class="{_esc(sec.kind)}"{_sec_attrs(sec)}>'
        f"<h2{_field('title')}>{_esc(f.get('title', sec.kind.title()))}</h2>"
        f"<p{_field('body')}>{_esc(f.get('body', ''))}</p></section>"
    )


def render_html(spec: AppSpec) -> str:
    """Deterministically render an AppSpec to a self-contained index.html (inline CSS
    so it paints reliably — no separate stylesheet that streams late)."""
    d = {k: _css_safe(v) for k, v in spec.resolved_design().items()}
    css = (
        f":root{{--primary:{d['primary']};--accent:{d['accent']};--bg:{d['bg']};--fg:{d['fg']}}}"
        f"body{{margin:0;font-family:{d['font']};background:var(--bg);color:var(--fg)}}"
        "section{padding:3rem 1.5rem;max-width:900px;margin:0 auto}"
        ".hero{text-align:center}.hero h1{font-size:2.5rem;margin:0 0 .5rem}"
        ".cta,button{display:inline-block;background:var(--primary);color:#fff;border:0;"
        "padding:.75rem 1.25rem;border-radius:.5rem;text-decoration:none;cursor:pointer}"
        ".lead form{display:flex;flex-direction:column;gap:.5rem;max-width:360px}"
        ".lead input{padding:.6rem;border:1px solid #ccc;border-radius:.4rem}"
    )
    body = "\n".join(_render_section(s) for s in spec.sections)
    ver = _md.attr(_md.DataDiscoAttr.VERSION, _md.METADATA_VERSION)
    return (
        f'<!doctype html>\n<html lang="en"{ver}>\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{_esc(spec.title)}</title>\n<style>{css}</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )
