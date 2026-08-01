"""AppKit EPIC J: semantic edit metadata (`data-disco-*` attributes).

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.

Additive `data-disco-*` attributes that let a UI click on a rendered element map
back to the AppSpec slot it came from, so the host can steer an `app_update_content`
patch (the in-frame selection agent reads these; see selection_agent.js / appResolver).
They never change layout/content and are design_lint-inert (the linter scans for
slop tells, not data-attributes). The section root carries file/section/screen-label;
a content-bearing element carries the AppSpec slot name (`data-disco-field`), and an
item row carries its collection id + 0-based index + item kind.

The file is ALWAYS `.disco/appspec.json` (where `app_update_content` mutates), and
the field names are AppSpec slot names — `cta_label` (NOT the `ctaLabel` content.ts
key), `heading`, `subheading`, `body`, `items`, `success_message` — exactly the
keys the tool accepts.
"""

from __future__ import annotations

from .. import semantic_metadata as _md
from ..spec import Section
from .ids import _ts

_DISCO_SPEC_FILE = ".disco/appspec.json"


def _disco_attr(a: _md.DataDiscoAttr, value: object) -> str:
    """A JSX-safe static `data-disco-*` attribute fragment in canonical attr order."""
    return f" {a.value}={_ts(value)}"


def _disco_expr_attr(a: _md.DataDiscoAttr, expr: str) -> str:
    """A JSX expression-valued `data-disco-*` attribute fragment."""
    return f" {a.value}={{{expr}}}"


def _disco_field_attr(field: str) -> str:
    return _disco_attr(_md.DataDiscoAttr.FIELD, field)


def _disco_section_attrs(section: Section) -> str:
    """The semantic-edit attrs stamped on a section root through the canonical
    vocabulary: the spec file, section id, and stable screen label."""
    return (
        _disco_attr(_md.DataDiscoAttr.FILE, _DISCO_SPEC_FILE)
        + _disco_attr(_md.DataDiscoAttr.SECTION, section.id)
        + _disco_attr(
            _md.DataDiscoAttr.SCREEN_LABEL,
            _md.screen_label_value(section.id),
        )
    )


def _disco_item_attrs(section: Section, *, index_expr: str, item_kind: str) -> str:
    """Semantic metadata for one generated `content.items` row."""
    return (
        _disco_attr(_md.DataDiscoAttr.COLLECTION, f"{section.id}.items")
        + _disco_expr_attr(_md.DataDiscoAttr.INDEX, index_expr)
        + _disco_attr(_md.DataDiscoAttr.ITEM_KIND, item_kind)
    )


def _render_body(comp_id: str) -> str:
    """Generic content body shared by non-form sections: renders ONLY the slots
    that are set at runtime, reading from content.ts by id (so updating content
    never requires regenerating the component). Each content-bearing element carries
    a `data-disco-field` slot tag (Epic J) so a UI click maps back to the spec slot."""
    return (
        '      {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
        f"      {{c.heading ? <h2{_disco_field_attr('heading')}>{{c.heading}}</h2> : null}}\n"
        f'      {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
        "{c.subheading}</p> : null}\n"
        f"      {{c.body ? <p{_disco_field_attr('body')}>{{c.body}}</p> : null}}\n"
    )
