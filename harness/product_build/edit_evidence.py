"""PKG-03-EDIT-EVIDENCE — the observation half of the five P8D edit-evidence producers.

The five targeted/manual-edit oracles (``build_soak/oracles/targeted_edit.py`` and
``manual_edit_preservation.py``) read ``product_evidence`` slices whose producer their own
docstrings name — *"the live producer is the P1B-LIVE / soak runner"* — and which was
never built. Every classification this campaign has frozen therefore SKIPped all five.

This module is that producer's PURE half: it turns real captured state into the five
slices. Its inputs are things the live runner (:mod:`edit_evidence_run`) read back out of
the product — the workspace file's bytes before and after a real agent edit, the run's own
event log, and the exact snippet a user wrote by hand. It never invents a value and never
supplies a default for an observation that was not made: a fabricated slice in front of an
oracle that has never had input is strictly worse than the SKIP it replaces, so an
observation that cannot be made raises :class:`EditObservationError`.

Pure and unit-tested, split from the runner exactly as ``build_soak/product_evidence.py``
is pure while the live spec drives the browser — so the observation logic can be
mutation-tested without a live model.
"""

from __future__ import annotations

import difflib
import posixpath
import re
from dataclasses import dataclass
from typing import Any

# The workspace-mutating tools. Mirrors the set the CD-TOOLS-9 / P8 live proofs adjudicate
# against (`targeted_edit_run.py`, `p8_selection_edit_run.py`), which were validated against
# the real product's event log.
EDIT_TOOLS = frozenset({
    "exact_replace",
    "file_edit",
    "file_insert_lines",
    "file_replace_lines",
    "file_str_replace",
    "run_project_script",
    "safe_write_file",
})

# A comment anchor: the durable, user-authored marker the CommentAnchorOracle asks about.
ANCHOR_RE = re.compile(r"<!--\s*anchor:\s*([A-Za-z0-9_.-]+)\s*-->")

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


class EditObservationError(RuntimeError):
    """An observation the producer must make could not be made from the captured state.

    Raised rather than returning a plausible-looking default: the caller's only honest
    options are to fix the capture or to record the gap as a finding.
    """


def _collapse(text: str) -> str:
    """Whitespace-collapsed text, so an edit that merely re-wraps a line is not read as a
    changed label."""
    return " ".join(text.split())


def observe_comment_anchors(html: str) -> list[str]:
    """The anchor names present in ``html``, sorted and de-duplicated.

    Set semantics, matching the oracle: it asks which anchors were LOST, not where they
    moved to, so a section reorder must not read as a loss.
    """
    return sorted({m.group(1) for m in ANCHOR_RE.finditer(html)})


def _first_heading_text(element: Any) -> str:
    """The text of the first heading *inside* ``element``, in document order."""
    for node in element.iter():
        if node is element or not isinstance(node.tag, str):
            continue
        if node.tag.lower() in _HEADING_TAGS:
            text = _collapse(node.text_content())
            if text:
                return text
    return ""


def observe_screen_labels(html: str) -> dict[str, str]:
    """Map each identified section to its on-screen label — its first heading's text.

    The label is read from the RENDERED heading text rather than from the id, because the
    oracle's question is whether an unedited section's user-visible identity moved: an edit
    that rewrites a heading changes the label while leaving the id untouched, and that is
    exactly the instability being looked for.
    """
    if not html.strip():
        raise EditObservationError("cannot observe screen labels in empty HTML")
    try:
        from lxml import html as lhtml
    except ImportError as exc:  # pragma: no cover — lxml is a hard product dependency
        raise EditObservationError("lxml is required to observe screen labels") from exc
    try:
        root = lhtml.fromstring(html)
    except Exception as exc:  # noqa: BLE001 — any parse failure is an unusable observation
        raise EditObservationError(f"unparseable HTML: {exc}") from exc
    labels: dict[str, str] = {}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        section_id = (element.get("id") or "").strip()
        if not section_id or section_id in labels:
            continue
        label = _first_heading_text(element)
        if label:
            labels[section_id] = label
    return labels


def normalize_workspace_path(raw: Any) -> str | None:
    """A workspace-relative posix path, or None when ``raw`` names no file.

    The edit tools accept ``index.html``, ``./index.html``, ``workspace/index.html`` and
    ``/workspace/index.html`` for one file (the product's own ROOT-2 prefix stripping), so
    the observation is normalized before it is compared with the scenario's declared paths.
    """
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return None
    text = re.sub(r"^/?workspace/", "", text)
    text = re.sub(r"^\./", "", text)
    normalized = posixpath.normpath(text).lstrip("/")
    return normalized if normalized and normalized != "." else None


def _call_paths(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    """Every workspace path one edit call names."""
    if tool_name == "run_project_script":
        operations = arguments.get("operations") or []
        raw = [(op or {}).get("path") for op in operations if isinstance(op, dict)]
    else:
        raw = [arguments.get("path")]
    return [p for p in (normalize_workspace_path(r) for r in raw) if p]


def _successful_edit_call_ids(events: list[dict[str, Any]]) -> set[str]:
    """The call ids of edit calls the product reported as SUCCEEDING."""
    ok: set[str] = set()
    for event in events:
        result = event.get("tool_result") or {}
        if result.get("tool_name") in EDIT_TOOLS and result.get("success"):
            call_id = result.get("call_id")
            if call_id:
                ok.add(str(call_id))
    return ok


def observe_edited_files(events: list[dict[str, Any]], *, after_seq: int = 0) -> list[str]:
    """The workspace paths a SUCCESSFUL edit tool actually wrote, after ``after_seq``.

    Read from the run's own event log — the product's record of what it did — rather than
    by diffing the filesystem: the oracle asks which files the EDIT touched, and a file can
    differ on disk for reasons the edit did not cause (a preview build artifact, a lockfile).
    A rejected edit touched nothing, so only calls whose ``tool_result`` reports success
    count.
    """
    ok_ids = _successful_edit_call_ids(events)
    touched: set[str] = set()
    for event in events:
        if int(event.get("seq") or 0) <= after_seq:
            continue
        call = event.get("tool_call") or {}
        name = call.get("tool_name")
        if name not in EDIT_TOOLS or str(call.get("call_id") or "") not in ok_ids:
            continue
        touched.update(_call_paths(str(name), call.get("arguments") or {}))
    return sorted(touched)


def observe_churn(before: str, after: str) -> tuple[int, int]:
    """``(changed_lines, total_lines)`` between two real revisions of one file.

    ``total_lines`` is the PRE-edit line count — the size of the thing a rewrite would have
    rewritten — so a full rewrite lands near a ratio of 1.0 however much the file grew.
    ``changed_lines`` counts, per non-equal diff opcode, the larger of the lines removed and
    the lines added: a replaced line is one change rather than two, while an append-only
    edit still counts every line it appended.
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if not before_lines:
        raise EditObservationError("cannot measure churn against an empty pre-edit file")
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    changed = sum(
        max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
    )
    return changed, len(before_lines)


@dataclass(frozen=True)
class EditCapture:
    """Everything the live runner read back out of the product for one edit run.

    ``before``/``after`` are the target file's real bytes on either side of the agent edit;
    ``before`` is snapshotted AFTER the user's manual override was written, so it is the
    state the agent was actually asked to edit.
    """

    path: str
    """The workspace-relative file the edit was scoped to."""

    before: str
    """The target file's bytes before the agent edit (and after the manual seed)."""

    after: str
    """The target file's bytes after the agent edit."""

    final_files: dict[str, str]
    """Every override-carrying file, read back from the workspace after the edit."""

    overrides: dict[str, str]
    """The verbatim snippets the user wrote by hand and that must survive."""

    edited_files: tuple[str, ...]
    """Observed from the event log — the files a successful edit call wrote."""

    expected_files: tuple[str, ...]
    """The files the scenario scoped the edit to."""

    edited_sections: tuple[str, ...]
    """The sections the edit was scoped to; every OTHER section's label must hold."""

    edit_scope: str
    """The scenario's declared scope. Only ``small`` is adjudicated for churn."""

    max_churn_ratio: float
    """The scenario's declared churn bound — evidence-supplied, never an oracle opinion."""


def assert_overrides_seeded(capture: EditCapture) -> None:
    """Fail closed when a manual override is missing from the PRE-edit file.

    If the snippet was never in the file the agent was handed, the manual edit never
    happened and a later ManualEditPreservationOracle PASS would be vacuous — it would be
    adjudicating the survival of something that never existed.
    """
    missing = sorted(p for p, snippet in capture.overrides.items() if snippet not in capture.before)
    if missing:
        raise EditObservationError(
            f"manual override(s) absent from the pre-edit file — seeding failed: {missing}"
        )


def build_edit_slices(capture: EditCapture) -> dict[str, dict[str, Any]]:
    """The five edit-evidence slices, each derived from ``capture``'s real observations."""
    assert_overrides_seeded(capture)
    changed_lines, total_lines = observe_churn(capture.before, capture.after)
    return {
        "targeted_edit": {
            "edited_files": list(capture.edited_files),
            "expected_files": list(capture.expected_files),
        },
        "rewrite_avoidance": {
            "edit_scope": capture.edit_scope,
            "changed_lines": changed_lines,
            "total_lines": total_lines,
            "max_churn_ratio": float(capture.max_churn_ratio),
        },
        "manual_edit": {
            "overrides": dict(capture.overrides),
            "final_files": dict(capture.final_files),
        },
        "comment_anchors": {
            "before": observe_comment_anchors(capture.before),
            "after": observe_comment_anchors(capture.after),
        },
        "screen_labels": {
            "edited_sections": list(capture.edited_sections),
            "before": observe_screen_labels(capture.before),
            "after": observe_screen_labels(capture.after),
        },
    }
