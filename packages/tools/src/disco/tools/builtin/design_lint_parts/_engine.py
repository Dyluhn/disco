"""The pure `design_lint` engine: `lint_design` scans an in-memory `{path:
text}` map + an optional `DesignSpec`/`DesignDirection` and returns the
structured verdict. Extracted from `design_lint.py`.

`lint_design` itself is DECOMPOSED (not just relocated) versus the original
17-mccabe monolith: the per-file rule-dispatch loop body is now its own
callable (`_lint_single_file`), the workspace-wide focus-visible probe is its
own callable (`_workspace_has_focus_visible`), and the tail (severity counts +
summary string) is split into `_finding_counts` / `_lint_summary`. Every rule
call, in the exact same order, for the exact same files, feeding the exact
same sort/counts/summary logic — this is pure decomposition, not a behavior
change."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from disco.core.appkit.spec import DesignSpec
from disco.core.context import ArtifactMemoryStore
from disco.core.design import DesignDirection, direction_from_markdown

from ._constants import _CSS_EXTS, _MARKUP_EXTS, _SIZE_PROBE_TIMEOUT_S
from ._deck_slides import _extract_deck_slides
from ._model import _RULE_META, _SEVERITY_RANK, DesignFinding
from ._rules_basic import (
    _rule_ai_purple,
    _rule_font_size_too_small,
    _rule_generic_font,
    _rule_tight_line_height,
    _rule_too_many_colors,
    _rule_too_many_fonts,
    _rule_web_banned_default_font,
)
from ._rules_deck import (
    _rule_deck_bullet_monotony,
    _rule_deck_glass,
    _rule_deck_handwritten_html,
    _rule_deck_missing_imagery,
    _rule_deck_orphan_slide,
    _rule_deck_text_budget,
    _rule_deck_type_floor,
)
from ._rules_direction import _rule_direction_conformance
from ._rules_effects import (
    _rule_dark_neon_glow,
    _rule_gradient_hero_text,
    _rule_too_many_animations,
)
from ._rules_web_interaction import (
    _rule_centered_hero_3_cards_cta,
    _rule_emoji_icons,
    _rule_pill_buttons,
    _rule_web_default_hidden_content,
    _rule_web_glass,
    _rule_web_hover_only_interactivity,
    _rule_web_reflexive_hover_scale,
    _rule_web_text_over_image_no_scrim,
)
from ._scan_helpers import _ext_of
from ._suppression import _justified_keys

if TYPE_CHECKING:
    from ..anatomy import ToolContext
    from ..sandbox.base import SandboxInstance


async def _bounded_read(sandbox: SandboxInstance, path: str, cap: int) -> bytes | None:
    """Read a file ONLY if it is within `cap` bytes — and prove that BEFORE pulling
    any bytes into this process. We stat the size in-sandbox (`wc -c < file` streams
    the file through the guest's own `wc`; nothing lands in Python memory) and refuse
    to `read_file` an over-cap (or unmeasurable) file. A multi-GB hostile file is
    therefore never a memory/time bomb here, regardless of any post-read length check.

    Returns the file bytes when readable and within `cap`; None when the size can't
    be determined (non-zero exit / unparseable) or exceeds `cap` — the caller treats
    None as absent/oversized (a source file is skipped; the designspec is treated as
    present-but-invalid, so rules fire and nothing is suppressed)."""
    try:
        res = await sandbox.exec_shell(
            f"wc -c < {shlex.quote(path)}", timeout_s=_SIZE_PROBE_TIMEOUT_S
        )
    except Exception:  # noqa: BLE001 — probe failed → treat as unmeasurable
        return None
    if res.exit_code != 0 or res.timed_out:
        return None
    try:
        size = int(res.stdout.strip())
    except (ValueError, AttributeError):
        return None
    if size < 0 or size > cap:
        return None
    try:
        return await sandbox.read_file(path)
    except Exception:  # noqa: BLE001 — vanished/unreadable between probe and read
        return None


def _workspace_has_focus_visible(files: Mapping[str, str]) -> bool:
    return any(
        ":focus-visible" in text.lower() or "focus-visible:" in text.lower()
        for text in files.values()
    )


def _lint_single_file(
    path: str,
    text: str,
    design_spec: DesignSpec | None,
    justified: set[str],
    direction: DesignDirection | None,
    workspace_has_focus_visible: bool,
) -> tuple[list[DesignFinding], tuple[str, str] | None]:
    """Run every applicable rule against one scanned file. Returns its findings
    plus a `(path, text)` markup entry when the file is markup (for the
    workspace-level section-sequence rule), or None when the file is neither
    CSS nor markup (nothing to scan)."""
    findings: list[DesignFinding] = []
    ext = _ext_of(path)
    is_css = ext in _CSS_EXTS
    is_markup = ext in _MARKUP_EXTS
    if not (is_css or is_markup):
        return findings, None

    markup_entry: tuple[str, str] | None = None
    if is_markup:
        markup_entry = (path, text)
        findings += _rule_deck_handwritten_html(path, text)
    deck_slides = _extract_deck_slides(text) if is_markup else []

    # rules that apply to any styled text (css + markup w/ inline styles)
    findings += _rule_generic_font(path, text, design_spec, justified)
    if direction is not None:
        findings += _rule_direction_conformance(path, text, direction)
    if not deck_slides:
        findings += _rule_web_banned_default_font(path, text, design_spec, justified)
        findings += _rule_too_many_fonts(path, text)
        findings += _rule_font_size_too_small(path, text)
        findings += _rule_too_many_colors(path, text)
        findings += _rule_tight_line_height(path, text)
        findings += _rule_web_reflexive_hover_scale(path, text)
        findings += _rule_web_hover_only_interactivity(
            path, text, workspace_has_focus_visible=workspace_has_focus_visible
        )
        findings += _rule_web_text_over_image_no_scrim(path, text)
        findings += _rule_web_glass(path, text)
        findings += _rule_web_default_hidden_content(path, text)
    findings += _rule_ai_purple(path, text, design_spec, justified)
    findings += _rule_gradient_hero_text(path, text, justified)
    findings += _rule_dark_neon_glow(path, text, justified)
    findings += _rule_too_many_animations(path, text, justified)
    findings += _rule_pill_buttons(path, text, justified)
    if is_markup:
        findings += _rule_emoji_icons(path, text, justified)
        if deck_slides:
            findings += _rule_deck_type_floor(path, text, deck_slides)
            findings += _rule_deck_text_budget(path, deck_slides)
            findings += _rule_deck_bullet_monotony(path, deck_slides)
            findings += _rule_deck_missing_imagery(path, deck_slides)
            findings += _rule_deck_glass(path, text, deck_slides)
            findings += _rule_deck_orphan_slide(path, deck_slides)
    return findings, markup_entry


def _scan_all_files(
    files: Mapping[str, str],
    design_spec: DesignSpec | None,
    justified: set[str],
    direction: DesignDirection | None,
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    markup: list[tuple[str, str]] = []
    workspace_has_focus_visible = _workspace_has_focus_visible(files)

    for path in sorted(files):
        text = files[path]
        file_findings, markup_entry = _lint_single_file(
            path, text, design_spec, justified, direction, workspace_has_focus_visible
        )
        findings += file_findings
        if markup_entry is not None:
            markup.append(markup_entry)

    findings += _rule_centered_hero_3_cards_cta(markup, justified)
    return findings


def _finding_counts(findings: list[DesignFinding]) -> dict[str, int]:
    counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _lint_summary(
    files: Mapping[str, str], findings: list[DesignFinding], counts: dict[str, int], ok: bool
) -> str:
    if ok:
        return (
            f"design_lint: clean — no slop across {len(files)} scanned file(s)."
            if files
            else "design_lint: no scannable files found."
        )
    rule_ids = [f.rule_id for f in findings]
    return (
        f"design_lint: {len(findings)} finding(s) "
        f"({counts['error']} error, {counts['warning']} warning, {counts['info']} info) — "
        f"{', '.join(dict.fromkeys(rule_ids))}."
    )


def lint_design(
    files: Mapping[str, str],
    design_spec: DesignSpec | None,
    *,
    spec_present: bool,
    spec_valid: bool,
    direction: DesignDirection | None = None,
) -> dict[str, Any]:
    """Scan an in-memory `{path: text}` map and return the structured verdict.

    `design_spec` is the parsed spec (None when absent OR invalid). `spec_present`
    / `spec_valid` are reported in the verdict so the agent knows WHY rules fired
    (a missing/invalid spec suppresses nothing — intent can't be claimed).
    `direction` is the committed design direction (None when absent/unparseable);
    when present it drives the ADVISORY conformance rules (font/palette), which are
    ground-truthed to the direction and never suppressed by a designspec."""
    justified = _justified_keys(design_spec)
    findings = _scan_all_files(files, design_spec, justified, direction)

    findings.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(f.severity, 9),
            _RULE_META.get(f.rule_id, ("info", 999))[1],
            f.path,
            f.line,
        )
    )

    counts = _finding_counts(findings)
    ok = len(findings) == 0
    summary = _lint_summary(files, findings, counts, ok)

    return {
        "ok": ok,
        "findings": [f.model_dump() for f in findings],
        "counts": counts,
        "summary": summary,
        "scanned_files": len(files),
        "design_spec_present": spec_present,
        "design_spec_valid": spec_valid,
    }


async def load_committed_direction(ctx: ToolContext) -> DesignDirection | None:
    """Load the same immutable design direction every lint boundary enforces.

    Absent, unreadable, or unparseable context intentionally resolves to ``None``:
    the pure direction-conformance rules then no-op. Keeping this best-effort loader
    shared prevents semantic AppKit mutations from certifying a tree against a
    weaker contract than the final ``design_lint`` / ``verify_appkit_app`` scan.
    """

    assert ctx.sandbox is not None
    try:
        markdown = await ArtifactMemoryStore(ctx.sandbox).read_design_direction()
    except Exception:  # noqa: BLE001 — absent/unreadable → no committed direction
        return None
    return direction_from_markdown(markdown or "")
