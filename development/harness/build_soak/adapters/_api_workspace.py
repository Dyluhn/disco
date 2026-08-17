"""Bounded workspace expectation policy extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ._api_browser import (
    _manifest_lookup,
    _manifest_present,
    _manifest_sha,
    _needs_resolved_write_proof,
    _norm_rel,
    _payload,
)
from ._api_shell import (
    _shell_is_proven_read_only,
    _shell_removes,
)
from ._api_types import (
    _ELISION_MARKER_RE,
    _FILE_READ_FULL_HEADER_RE,
    _FILE_READ_TOOLS,
    _FILE_WRITE_FULL_TOOLS,
    _FILE_WRITE_PARTIAL_TOOLS,
    _RAW_SHA256_RE,
)

_SHELL_TOOLS = frozenset({"shell", "shell_exec"})
_SCRIPT_MUTATE_TOOLS = frozenset({"run_project_script"})
_Mutation = tuple[int, str, str | None]


@dataclass(frozen=True)
class _ObservationIndex:
    success: dict[str, bool]
    content: dict[str, Any]
    structured: dict[str, Any]
    error_call_ids: set[str]


def _full_readback_body(args: dict[str, Any], content: Any) -> str | None:
    """Return the RENDERED body of a FULL, GENUINELY-RENDERED ``file_read`` observation, or
    None when the read is paged / truncated / synthetic (and so cannot define the agent's
    final bytes). The body is the readback MINUS its ``[lines 1-N of N]`` header line — i.e.
    the ``<line-no>\\t<line>`` block, ready to compare against ``_render_numbered`` of the
    on-disk file.

    Rejections (→ None): an explicit ``offset``/``limit`` (a targeted, partial read); any
    header that is not the exact full-read shape (a budget-truncated ``; read more…`` or a
    pressure ``— HEAD-ONLY…`` read has unequal/extra header text); the F9 dedup pointer or
    any other synthetic content (no ``[lines 1-N of N]`` header at all)."""
    if args.get("offset") is not None or args.get("limit") is not None:
        return None
    if not isinstance(content, str):
        return None
    head, sep, body = content.partition("\n")
    m = _FILE_READ_FULL_HEADER_RE.match(head)
    if m is None or m.group(1) != m.group(2):
        return None  # not a full read (or a synthetic [F9 dedup: …] / non-rendered payload)
    # `sep` empty ⇒ a header-only (empty-file) read: body is "" (matches _render_numbered("")).
    return body if sep else ""


def _render_numbered(text: str) -> str:
    """Render `text` EXACTLY as ``file_read`` renders a full read — 1-based, right-aligned
    line numbers + a tab, no trailing newline — by importing the PRODUCT's own pure helper
    (``disco.tools.builtin.files._number_lines``) so the format can never drift from the
    readback we compare against. Imported lazily to keep the deterministic test path free of
    the live tool deps until a readback signal actually needs rendering."""
    from disco.tools.builtin.files import _number_lines

    return _number_lines(text, 1)


def _observation_index(events: list[dict[str, Any]]) -> _ObservationIndex:
    index = _ObservationIndex({}, {}, {}, set())
    for event in events:
        kind = event.get("kind")
        payload = _payload(event)
        if kind == "agent_error":
            call_id = payload.get("tool_call_id")
            if call_id is not None:
                index.error_call_ids.add(str(call_id))
            continue
        if kind != "observation":
            continue
        result = payload.get("tool_result") or {}
        call_id = result.get("call_id")
        if call_id is None:
            continue
        key = str(call_id)
        index.success[key] = bool(result.get("success", True))
        index.content[key] = result.get("content")
        index.structured[key] = result.get("structured")
    return index


def _successful_action(
    event: dict[str, Any],
    observations: _ObservationIndex,
) -> tuple[int, Any, dict[str, Any], str] | None:
    if event.get("kind") != "action":
        return None
    tool_call = _payload(event).get("tool_call") or {}
    call_id = tool_call.get("call_id")
    if call_id is None:
        return None
    key = str(call_id)
    if key in observations.error_call_ids or not observations.success.get(key, False):
        return None
    return (
        int(event.get("seq", -1)),
        tool_call.get("tool_name"),
        tool_call.get("arguments") or {},
        key,
    )


def _structured_write_identity(structured: dict[str, Any]) -> tuple[str | None, str | None]:
    path = structured.get("path")
    sha = structured.get("sha256")
    observed_path = _norm_rel(path) if isinstance(path, str) and path else None
    observed_sha = sha.lower() if isinstance(sha, str) and _RAW_SHA256_RE.fullmatch(sha) else None
    return observed_path, observed_sha


def _content_sha(content: Any) -> str | None:
    if not isinstance(content, str) or _ELISION_MARKER_RE.search(content):
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _record_full_write(
    *,
    seq: int,
    args: dict[str, Any],
    structured: Any,
    want: dict[str, str],
    last_mut: dict[str, _Mutation],
) -> None:
    raw_path = str(args.get("path", ""))
    normalized_path = _norm_rel(raw_path)
    content_sha = _content_sha(args.get("content"))
    if isinstance(structured, dict):
        observed_path, observed_sha = _structured_write_identity(structured)
        implicated = {path for path in (normalized_path, observed_path) if path in want}
        valid = (
            observed_path == normalized_path
            and observed_sha is not None
            and content_sha is not None
            and content_sha == observed_sha
        )
        if valid and normalized_path in want:
            last_mut[normalized_path] = (seq, "sha", observed_sha)
        else:
            for path in implicated:
                last_mut[path] = (seq, "present", None)
        return
    if normalized_path not in want:
        return
    if not _needs_resolved_write_proof(raw_path) and content_sha is not None:
        last_mut[normalized_path] = (seq, "sha", content_sha)
    else:
        last_mut[normalized_path] = (seq, "present", None)


def _record_shell_mutation(
    *,
    seq: int,
    args: dict[str, Any],
    want: dict[str, str],
    last_mut: dict[str, _Mutation],
) -> None:
    command = str(args.get("command") or args.get("cmd") or "")
    if _shell_is_proven_read_only(command, set(want)):
        return
    for path in want:
        kind = "absent" if _shell_removes(command, path) else "present"
        last_mut[path] = (seq, kind, None)


def _record_script_mutations(
    *,
    seq: int,
    args: dict[str, Any],
    want: dict[str, str],
    last_mut: dict[str, _Mutation],
) -> None:
    operations = args.get("operations")
    if not isinstance(operations, list):
        return
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") not in {
            "save",
            "replace_text",
        }:
            continue
        path = _norm_rel(str(operation.get("path", "")))
        if path in want:
            last_mut[path] = (seq, "present", None)


def _record_action_expectation(
    action: tuple[int, Any, dict[str, Any], str],
    observations: _ObservationIndex,
    want: dict[str, str],
    last_mut: dict[str, _Mutation],
    last_read: dict[str, tuple[int, str]],
) -> None:
    seq, name, args, call_id = action
    if name in _FILE_WRITE_FULL_TOOLS:
        _record_full_write(
            seq=seq,
            args=args,
            structured=observations.structured.get(call_id),
            want=want,
            last_mut=last_mut,
        )
        return
    path = _norm_rel(str(args.get("path", "")))
    if name in _FILE_WRITE_PARTIAL_TOOLS and path in want:
        last_mut[path] = (seq, "present", None)
    elif name in _FILE_READ_TOOLS and path in want:
        body = _full_readback_body(args, observations.content.get(call_id))
        if body is not None:
            last_read[path] = (seq, body)
    elif name in _SHELL_TOOLS:
        _record_shell_mutation(seq=seq, args=args, want=want, last_mut=last_mut)
    elif name in _SCRIPT_MUTATE_TOOLS:
        _record_script_mutations(seq=seq, args=args, want=want, last_mut=last_mut)


def _resolved_expected_states(
    want: dict[str, str],
    last_mut: dict[str, _Mutation],
    last_read: dict[str, tuple[int, str]],
) -> dict[str, tuple[Any, ...]]:
    by_norm: dict[str, tuple[Any, ...]] = {}
    for np in want:
        mut = last_mut.get(np)
        rd = last_read.get(np)
        if mut is None:
            continue  # (e) no mutation signal → unknown (omitted)
        mseq, mkind, msha = mut
        if mkind == "sha":
            by_norm[np] = ("sha", msha)  # (a)
        elif mkind == "absent":
            by_norm[np] = ("absent",)  # (c)
        elif rd is not None and rd[0] > mseq:
            by_norm[np] = ("rendered", rd[1])  # (b) full readback AFTER the last partial edit
        else:
            by_norm[np] = ("present_unproven",)  # (d) partial edit, no provable post-state
    return {want[np]: st for np, st in by_norm.items()}


def _agent_declared_expected(
    events: list[dict[str, Any]], declared: list[str]
) -> dict[str, tuple[Any, ...]]:
    """Reconstruct each declared file's agent-final state from successful actions."""
    want = {_norm_rel(path): path for path in declared}
    if not want:
        return {}
    observations = _observation_index(events)
    last_mut: dict[str, _Mutation] = {}
    last_read: dict[str, tuple[int, str]] = {}
    for event in events:
        action = _successful_action(event, observations)
        if action is not None:
            _record_action_expectation(
                action,
                observations,
                want,
                last_mut,
                last_read,
            )
    return _resolved_expected_states(want, last_mut, last_read)


def _manifest_content(manifest: dict[str, Any], path: str) -> str | None:
    """The on-disk file's decoded UTF-8 text from the manifest entry (empty for a binary /
    oversized file), or None when the file is absent."""
    entry = _manifest_lookup(manifest, path)
    if entry is None:
        return None
    c = entry.get("content")
    return c if isinstance(c, str) else ""


def _evaluate_snapshot_readiness(
    declared: list[str],
    expected: dict[str, tuple[Any, ...]],
    manifest: dict[str, Any],
    stable_n: dict[str, int],
    *,
    stable_polls: int,
) -> tuple[bool, list[str], list[str]]:
    """Decide whether the snapshot has settled on the agent's final state.

    Returns ``(ready, blocking, unproven_ready)``:
      * ``ready`` — EVERY declared file's expected state is met.
      * ``blocking`` — UNSATISFIED files carrying a DEFINITE, content-precise signal
        (``sha`` / ``rendered`` / ``absent``). On the wait-budget deadline a non-empty
        ``blocking`` is the FAIL-FAST trigger (the snapshot is provably behind the agent).
      * ``unproven_ready`` — files accepted ONLY via extended content-stability
        (``present_unproven`` / ``unknown``), i.e. with no content proof; the caller logs a
        warning. These NEVER fail-fast (a legit edit-without-readback build must not become
        INVALID_RUN); an unsatisfied unproven file simply keeps `ready` False and is accepted
        best-effort at the deadline.

    Content-precise gates:
      * ``sha`` — on-disk raw sha256 == the written bytes' sha.
      * ``rendered`` — the on-disk file RENDERED by file_read's own numberer == the readback.
      * ``absent`` — the file is NOT on disk.
    Stability gates (count of consecutive identical reads in ``stable_n``):
      * ``present_unproven`` — present AND ``>= stable_polls`` (+warn).
      * ``unknown`` — present-or-absent stable ``>= stable_polls`` (+warn)."""
    ready = True
    blocking: list[str] = []
    unproven_ready: list[str] = []
    for p in declared:
        kind = expected.get(p, ("unknown",))[0]
        present = _manifest_present(manifest, p)
        n = stable_n.get(p, 0)
        definite = False
        if kind == "sha":
            ok = present and _manifest_sha(manifest, p) == expected[p][1]
            definite = True
        elif kind == "rendered":
            on_disk_rendered = _render_numbered(_manifest_content(manifest, p) or "")
            ok = present and on_disk_rendered == expected[p][1]
            definite = True
        elif kind == "absent":
            ok = not present
            definite = True
        elif kind == "present_unproven":
            ok = present and n >= stable_polls
            if ok:
                unproven_ready.append(p)
        else:  # unknown — require PRESENT + extended stability (NOT stable-absent).
            # Settling on a STABLE-ABSENT declared file accepted a PARTIAL snapshot (only the
            # .pmx/ live-browser scaffolding had flushed) as final, before the real output
            # (e.g. index.html) appeared → a FALSE_FINISH_NO_OUTPUT false-negative on a build
            # that actually succeeded. Requiring `present` makes a not-yet-flushed file keep
            # the snapshot un-ready; a file that is GENUINELY never created waits the full
            # window and the run's own timeout flags it (correct), instead of premature ready.
            ok = present and n >= stable_polls
            if ok:
                unproven_ready.append(p)
        if not ok:
            ready = False
            if definite:
                blocking.append(p)
    return ready, blocking, unproven_ready
