"""Workspace snapshot rendering owner for the agent loop.

This module is the single bounded implementation owner for the always-fresh
per-turn CURRENT WORKSPACE snapshot: the live on-disk content of the working-set
files re-read from the sandbox each turn and rendered as an authoritative
message outside the condensable history.  The former ``view_render`` module
re-exports these symbols so every historical import path, signature, prompt
byte, receipt, and omission rule is preserved.

These are render-time projections that carry no loop state: the snapshot takes
the sandbox explicitly and is pure over its inputs. Bodies are byte-identical
to the former ``view_render`` implementations.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
from typing import TYPE_CHECKING, Any

from ..effects import ObservationReceipt
from ..events import WORKSPACE_SNAPSHOT_SENTINEL, Event, LLMMessage
from .context_budget import ContextCaps
from .file_state import FileStateTracker
from .messages import _workspace_paths_from_events
from .resource_context import (
    current_prompt_coverage_lines,
    snapshot_receipt,
    snapshot_window_receipt,
)

if TYPE_CHECKING:
    from .boundaries import Sandbox

# CW-2 — the assist-ON baseline snapshot caps (today's weak-model calibration).
# These are the byte-identical fallback when no derived caps are threaded in (the
# engine's back-compat delegator + legacy tests). assist-OFF caps scale with the
# live context window via context_budget.derive_context_caps.
_WS_MAX_FILES = 8  # cap the snapshot breadth (most-recently-touched first)
_WS_PER_FILE_CHARS = 6_000  # per-file cap; larger files head/tail-truncate with a marker
_WS_TOTAL_CHARS = 16_000  # total snapshot budget (~4k tokens), bounded vs the condenser
# The assist-ON baseline as a ContextCaps bundle (the default when caps is None).
_BASELINE_CAPS = ContextCaps(
    max_files=_WS_MAX_FILES,
    per_file_chars=_WS_PER_FILE_CHARS,
    total_chars=_WS_TOTAL_CHARS,
    read_char_budget=7_000,
    obs_snip_chars=8_000,
)
_WS_READ_TIMEOUT_S = 2.0  # per-file read cap — the snapshot runs under the conversation
#                           lock, so a hung sandbox read must never freeze control ops
_WS_HASH_TIMEOUT_S = 5.0  # the single per-turn hash exec over the working set


class SnapshotCache:
    """Bytes of working-set files keyed by content hash, held across turns.

    The per-turn snapshot used to re-read every working-set file from the sandbox —
    one container exec per file, ~25 s a turn on a ten-file set (Pharmacy run 2).
    Now one `sha256sum` exec names what changed; a file is read again only when its
    hash differs from the cached one, and its bytes come from here otherwise. The
    rendered block is byte-identical either way; only the I/O differs."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, bytes]] = {}

    def get(self, path: str, sha: str) -> bytes | None:
        entry = self._entries.get(path)
        return entry[1] if entry is not None and entry[0] == sha else None

    def put(self, path: str, raw: bytes) -> None:
        self._entries[path] = (hashlib.sha256(raw).hexdigest(), raw)

    def retain(self, paths: list[str]) -> None:
        keep = set(paths)
        for path in [p for p in self._entries if p not in keep]:
            del self._entries[path]

    def __len__(self) -> int:
        return len(self._entries)


def _parse_sha256sum(stdout: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2:
            continue
        # GNU marks a line whose name needed escaping with a leading backslash.
        digest, path = parts[0].lstrip("\\"), parts[1].strip()
        if len(digest) != 64:
            continue
        hashes[path] = digest
    return hashes


async def working_set_hashes(sbx: Any, paths: list[str]) -> dict[str, str] | None:
    """One sandbox exec: the sha256 of every working-set file that exists.

    Missing files are simply absent from the map (their read path reports them).
    Returns None when the sandbox has no shell or the exec fails, so the caller falls
    back to reading every file — never a wrong "unchanged"."""
    exec_shell = getattr(sbx, "exec_shell", None)
    if not callable(exec_shell) or not paths:
        return None
    command = "sha256sum -- " + " ".join(shlex.quote(p) for p in paths) + " 2>/dev/null"
    try:
        result = await asyncio.wait_for(
            exec_shell(command, timeout_s=int(_WS_HASH_TIMEOUT_S)), timeout=_WS_HASH_TIMEOUT_S + 2
        )
    except Exception:  # noqa: BLE001 — a failed hash means "read everything", never "unchanged"
        return None
    return _parse_sha256sum(getattr(result, "stdout", "") or "")


class _SnapshotHalt:
    """Typed sentinel: a hung read ⇒ wedged sandbox → stop the snapshot.
    A distinct type (not bare ``object()``) so the caller's isinstance check
    narrows the read result cleanly to ``bytes | None``."""


_SNAPSHOT_READ_HALT = _SnapshotHalt()


async def _read_working_file(
    sbx: Sandbox, path: str, omitted_notes: list[str]
) -> bytes | _SnapshotHalt | None:
    """Read one working-set file's raw bytes for the snapshot. Returns the
    bytes on success; ``None`` to SKIP this file (gone / unreadable / directory
    / binary — an explanatory note is appended to ``omitted_notes``); or
    ``_SNAPSHOT_READ_HALT`` to STOP the snapshot (a hung read implies a wedged/
    dead sandbox — don't hold the conversation lock for timeout×N files, which
    would block pause/steer/cancel). Raises nothing. Extracted from
    workspace_snapshot_message to keep that coordinator under the size cap."""
    try:
        raw = await asyncio.wait_for(sbx.read_file(path), timeout=_WS_READ_TIMEOUT_S)
    except TimeoutError:
        # Degrade to the snapshot collected so far. The sandbox isn't healed
        # here (a pure projection step); the model's NEXT real action goes
        # through _execute_and_observe, which detects the dead sandbox.
        return _SNAPSHOT_READ_HALT
    except FileNotFoundError:
        # E4 (T8) — deleted between the action and this read. Surface it (the
        # model can re-decide whether to re-create); don't silently lose it.
        omitted_notes.append(f"[file gone: {path}]")
        return None
    except PermissionError:
        # E4 (T8) — sandbox denied the read. Note it so the model knows the path
        # is in its working set but the snapshot can't show it.
        omitted_notes.append(f"[unreadable: permission: {path}]")
        return None
    except (IsADirectoryError, NotADirectoryError, OSError) as e:
        # E4 (T8) — a directory slipped into the working set. Note + skip; any
        # other OSError keeps the prior silent-skip contract.
        if isinstance(e, IsADirectoryError) or (
            isinstance(e, OSError) and getattr(e, "errno", None) == 21  # EISDIR
        ):
            omitted_notes.append(f"[directory: {path}]")
        return None
    except Exception:  # noqa: BLE001 — file gone/unreadable this turn: skip it
        return None
    # E4 (T8) — binary sniff. A NUL byte in the first 1KB is a near-certain
    # binary signal; the snapshot's only value is content the model can act on.
    # Bounded sample so a huge text file with one stray NUL past the head still
    # renders via errors="replace".
    if b"\x00" in raw[:1024]:
        omitted_notes.append(f"[binary omitted: {path}]")
        return None
    return raw


def _workspace_snapshot_preamble(pin_full: bool) -> str:
    """The CURRENT WORKSPACE block preamble. Per-tier wording (CW-3 / CW P1-c):
    ``pin_full`` (assist OFF) moves the block to the cacheable PREFIX, so it names
    ITSELF ("this prompt") instead of a directional "above/below"; the assist-ON
    tail-placement keeps the original directional wording byte-for-byte."""
    if pin_full:
        return (
            f"{WORKSPACE_SNAPSHOT_SENTINEL} — your files on disk RIGHT NOW (authoritative).\n"
            "This block contains live disk content for the files you are working on, "
            "re-read this turn. A body shown without a truncation or omission notice "
            "is complete and exact; other files are not fully present. It OVERRIDES "
            "any other copy of these "
            "files shown elsewhere in this prompt; trust THIS over your memory.\n"
            "To change a file: for a SMALL change, prefer `file_edit` (pass the exact "
            "text you see as `old`) or `file_replace_lines` / `file_insert_lines` (use "
            "the line numbers shown for each file in this block). For a full rewrite, "
            "use `file_write` with the FULL new content. A complete, untruncated file "
            "body shown here already counts as a current read. If a body is truncated, "
            "omitted, or modified afterward by a tool that does not return its complete "
            "current content, call `file_read` before a full rewrite. Keep every existing "
            "function, constant, and docstring you are not "
            "deliberately removing — do not drop code you did not mean to delete.\n"
            "**SILENT CONTEXT** — use this block without narrating it. Do NOT "
            "acknowledge the snapshot in your reply (no 'I can see the files', "
            "'the workspace shows…', 'good, the content is here', etc.). "
            "Just continue the work.\n\n"
        )
    return (
        f"{WORKSPACE_SNAPSHOT_SENTINEL} — your files on disk RIGHT NOW (authoritative).\n"
        "Below is live disk content for the files you are working on, re-read this "
        "turn. A body shown without a truncation or omission notice is complete and "
        "exact; other files are not fully present. It OVERRIDES any earlier or elided copy of "
        "these files shown above; trust THIS over your memory.\n"
        "To change a file: for a SMALL change, prefer `file_edit` (pass the exact "
        "text you see as `old`) or `file_replace_lines` / `file_insert_lines` (use "
        "the line numbers shown below). For a full rewrite, use `file_write` with "
        "the FULL new content. A complete, untruncated file body shown here already "
        "counts as a current read. If a body is truncated, omitted, or modified "
        "afterward by a tool that does not return its complete current content, call "
        "`file_read` before a full rewrite. Keep every existing function, constant, "
        "and docstring you are not deliberately "
        "removing — do not drop code you did not mean to delete.\n"
        "**SILENT CONTEXT** — use this block without narrating it. Do NOT "
        "acknowledge the snapshot in your reply (no 'I can see the files', "
        "'the workspace shows…', 'good, the content is here', etc.). "
        "Just continue the work.\n\n"
    )


def _filter_snapshot_candidates(ordered: list[str]) -> list[str]:
    """Filter out overflow spill logs and pmc artifacts from the working set.

    E4 (T8) — .disco-spill-* are T10's overflow logs (head/tail markers of an
    oversize stdout), not deliverables. They pollute the working set with
    multi-MB noise and dwarf every real file. Skip them outright: the model
    has no business re-reading its own spill log. Match the leaf filename
    (basename) so an absolute path the model might use (e.g.
    /workspace/.disco-spill-abc.log) is caught too.
    """
    spill_basename_prefix = ".disco-spill-"
    return [
        path
        for path in ordered
        if not os.path.basename(path).startswith(spill_basename_prefix)
        and ".pmx" not in path.split("/")
    ]


def _render_truncated_file(
    text: str, raw: bytes, path: str, cap: int, prompt_receipts: list[ObservationReceipt]
) -> str:
    """Render an oversize file as head + tail with a windowed-read marker.

    Emits a byte-range window receipt for the rendered head/tail spans.
    """
    head = cap * 3 // 4
    tail = cap - head
    shown = (
        f"{text[:head]}\n"
        f"… [{len(text) - head - tail:,} more chars — this file is large. "
        "To see a region: file_read(path, offset=L, limit=M). "
        "To change it: file_edit with a content anchor, or "
        "file_replace_lines on a freshly-read range.] …\n"
        f"{text[-tail:]}"
    )
    head_bytes = len(text[:head].encode("utf-8"))
    tail_bytes = len(text[-tail:].encode("utf-8")) if tail else 0
    byte_spans = tuple(
        span
        for span in (
            (0, head_bytes) if head_bytes else None,
            (len(raw) - tail_bytes, len(raw)) if tail_bytes else None,
        )
        if span is not None
    )
    prompt_receipts.append(
        snapshot_window_receipt(
            path=path,
            sha256=hashlib.sha256(raw).hexdigest(),
            raw_size_bytes=len(raw),
            byte_spans=byte_spans,
            rendered_content=shown,
        )
    )
    return shown


def _render_full_file(
    text: str, raw: bytes, path: str, prompt_receipts: list[ObservationReceipt]
) -> str:
    """Render a full file body and emit the exact full-file receipt.

    Only an untruncated, losslessly decoded body proves the disk revision
    byte-for-byte. Replacement-decoded text remains useful context, but it
    cannot authorize compaction or satisfy read grounding as 'exact'.
    """
    prompt_receipts.append(
        snapshot_receipt(
            path=path,
            sha256=hashlib.sha256(raw).hexdigest(),
            total_lines=len(text.splitlines()),
            raw_size_bytes=len(raw),
            rendered_content=text,
        )
    )
    return text


def _format_file_block(path: str, text: str, shown: str) -> str:
    """Format one file's BEGIN/END block.

    Plain text delimiters — NOT markdown ``` fences. A fenced snapshot
    invites the model to reflexively copy the ``` into its file_write
    (models are trained to wrap code in ```), polluting the real file and
    then thrashing to strip it (caught live on gpt-oss-120b). file_read
    returns raw content with no fences; the snapshot matches that.
    """
    return (
        f"----- BEGIN FILE {path} ({len(text):,} chars on disk) -----\n"
        f"{shown}\n"
        f"----- END FILE {path} -----"
    )


def _compose_snapshot_notice(
    budget_omitted: list[str], omitted_notes: list[str]
) -> str:
    """Compose the trailing notice: budget-omitted count plus per-file notes.

    A single trailing paragraph keeps the snapshot's shape consistent; the
    notes are short one-liners the model can act on (file_read, recreate, etc.).
    """
    notice_parts: list[str] = []
    if budget_omitted:
        shown_names = budget_omitted[:12]
        remaining = len(budget_omitted) - len(shown_names)
        suffix = f", +{remaining} more" if remaining else ""
        notice_parts.append(
            "[not present in this request because of the snapshot budget: "
            + ", ".join(shown_names)
            + suffix
            + ". Their current bytes remain on disk; file_read only a path/range needed "
            "for the next action.]"
        )
    notice_parts.extend(omitted_notes)
    return ("\n\n" + "\n".join(notice_parts)) if notice_parts else ""


def _compose_snapshot_coverage(prompt_receipts: list[ObservationReceipt]) -> str:
    """Compose the CURRENT PROMPT FILE COVERAGE block from the emitted receipts."""
    coverage_lines = current_prompt_coverage_lines(prompt_receipts)
    if not coverage_lines:
        return ""
    return (
        "\n\n# CURRENT PROMPT FILE COVERAGE\n"
        "Host-observed exact resource ranges physically present in this request:\n"
        + "\n".join(f"- {line}" for line in coverage_lines)
    )


async def _fetch_working_file(
    sbx: Sandbox,
    path: str,
    omitted_notes: list[str],
    *,
    hashes: dict[str, str] | None,
    cache: SnapshotCache | None,
) -> bytes | _SnapshotHalt | None:
    """Cached bytes when the per-turn hash matches; otherwise a sandbox read."""
    if hashes is not None and cache is not None:
        sha = hashes.get(path)
        if sha is not None:
            cached = cache.get(path, sha)
            if cached is not None:
                return cached
    raw = await _read_working_file(sbx, path, omitted_notes)
    if isinstance(raw, bytes) and cache is not None:
        cache.put(path, raw)
    return raw


async def _collect_snapshot_files(
    sbx: Sandbox,
    candidates: list[str],
    *,
    caps: ContextCaps,
    budget: int,
    tracker: FileStateTracker | None,
    out_pinned_full: set[str] | None,
    hashes: dict[str, str] | None = None,
    cache: SnapshotCache | None = None,
) -> tuple[list[str], list[ObservationReceipt], list[str], list[str], int, bool]:
    """Read and render each working-set file within the snapshot budget.

    Returns ``(blocks, prompt_receipts, omitted_notes, budget_omitted,
    shown_count, halted)``. ``halted`` is True when a hung read implies a wedged
    sandbox and the snapshot should degrade to what was collected so far.
    """
    blocks: list[str] = []
    prompt_receipts: list[ObservationReceipt] = []
    omitted_notes: list[str] = []
    budget_omitted: list[str] = []
    shown_count = 0
    for index, path in enumerate(candidates):
        if shown_count >= caps.max_files or budget <= 0:
            budget_omitted.extend(candidates[index:])
            break
        raw = await _fetch_working_file(sbx, path, omitted_notes, hashes=hashes, cache=cache)
        if isinstance(raw, _SnapshotHalt):
            budget_omitted.extend(candidates[index:])
            return blocks, prompt_receipts, omitted_notes, budget_omitted, shown_count, True
        if raw is None:
            continue  # gone / unreadable / directory / binary — note already added
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            omitted_notes.append(
                f"[invalid UTF-8 text omitted without lossy replacement: {path}; "
                "the exact bytes remain on disk]"
            )
            continue
        # Full body: file is stale, never-shown-in-tracker, or tracker is None
        # (backward-compat mode). Render head + tail when oversize.
        cap = min(caps.per_file_chars, budget)
        if len(text) > cap:
            shown = _render_truncated_file(text, raw, path, cap, prompt_receipts)
        else:
            shown = _render_full_file(text, raw, path, prompt_receipts)
            if out_pinned_full is not None:
                out_pinned_full.add(path)
        block = _format_file_block(path, text, shown)
        budget -= len(block)  # charge the WHOLE block incl header/markers (#6)
        blocks.append(block)
        shown_count += 1
        # W2 — update tracker after showing full body (seq=0: the snapshot
        # doesn't have its own seq; SHA is the ground truth for staleness).
        if tracker is not None:
            tracker.record_agent_io(path, raw, seq=0)
    return blocks, prompt_receipts, omitted_notes, budget_omitted, shown_count, False


async def workspace_snapshot_message(
    sbx: Sandbox | None,
    events: list[Event],
    *,
    tracker: FileStateTracker | None = None,
    stale: frozenset[str] | None = None,
    caps: ContextCaps | None = None,
    pin_full: bool = False,
    out_pinned_full: set[str] | None = None,
    out_prompt_receipts: list[ObservationReceipt] | None = None,
    hashes: dict[str, str] | None = None,
    cache: SnapshotCache | None = None,
) -> LLMMessage | None:
    """Re-derive the CURRENT on-disk content of the working-set files from the
    sandbox each turn and render it as an authoritative, always-fresh message.

    WHY (root cause, source-verified + reproduced live): a file_write's body is
    elided at render time to a "[[DISCO-ELIDED: N chars ...]]" sentinel (events._snip_args,
    >1.5k chars), and observation-masking + condensation can later erase a
    file_read's output too. A weak driver that doesn't proactively file_read
    every turn therefore loses sight of what's on disk and regenerates files
    from lossy memory — clobbering prior content (reproduced: a 4,441-char file
    rewritten to 202 chars, all constants + fingerprint gone).

    The OSS-proven fix (Aider's ChatChunks.chat_files re-read via io.read_text
    each turn; OpenHands str_replace operating on current on-disk text) is to
    re-derive file content from the SOURCE OF TRUTH (disk) every turn and keep
    it OUT of the condensable history. This message is rebuilt here, post-
    projection, so View.of's snip/mask/condense never touch it and the
    condenser (which acts on EVENTS) can never erase it. Returns None if there
    is no sandbox or no tracked files — degrading to prior behavior.

    Every provider request is stateless. A file shown on a prior turn is therefore
    never replaced by a "shown earlier" pointer. ``tracker`` and ``stale`` still
    support external-change notices, while every request independently carries the
    current bounded body. Full, untruncated, byte-exact UTF-8 bodies also emit typed
    receipts through ``out_prompt_receipts``; those receipts are the sole authority
    for compacting an equivalent historical read.

    Layering-safe: reaches the sandbox via the same duck-typed
    getattr(self.executor, "sandbox", None) seam as _execute_and_observe."""
    if sbx is None:
        return None
    # CW-2: assist-OFF derives larger caps from the live window; the default
    # (caps is None) is the byte-identical assist-ON baseline used by the engine's
    # back-compat delegator and legacy tests.
    _caps = caps if caps is not None else _BASELINE_CAPS
    # Keep the stale argument as an API-compatible input. Staleness affects the
    # separate warning message, not whether this stateless request receives bytes.
    _ = stale
    mutated, read_only = _workspace_paths_from_events(events)
    ordered = mutated + read_only  # mutated first → never evicted by reads (#1)
    if not ordered:
        return None
    # CW-3 / CW P1-c — PER-TIER preamble wording, gated on `pin_full` (which is set
    # iff assist is OFF). assist-OFF moves the block to the cacheable PREFIX (before
    # the event history), so any directional reference ("below"/"above") to the
    # history is WRONG → the block names ITSELF ("the CURRENT WORKSPACE block in this
    # prompt"). assist-ON keeps the block in the TAIL (after the history), so the
    # ORIGINAL pre-CW-3 directional wording is CORRECT — and restoring it byte-for-byte
    # keeps the assist-ON rendered prompt byte-identical to pre-CW-3 (P1-c).
    preamble = _workspace_snapshot_preamble(pin_full)
    budget = _caps.total_chars - len(preamble)
    candidates = _filter_snapshot_candidates(ordered)
    if cache is not None:
        cache.retain(candidates)
    blocks, prompt_receipts, omitted_notes, budget_omitted, _shown, _halted = (
        await _collect_snapshot_files(
            sbx,
            candidates,
            caps=_caps,
            budget=budget,
            tracker=tracker,
            out_pinned_full=out_pinned_full,
            hashes=hashes,
            cache=cache,
        )
    )
    # E4 (T8) — return None only when there is NOTHING to tell the model.
    # If every file was omitted (binary / gone / permission / spill) we
    # still want to surface the notes so the model knows the working
    # set is not empty on disk — it just couldn't be rendered.
    if not blocks and not omitted_notes and not budget_omitted:
        return None
    notice = _compose_snapshot_notice(budget_omitted, omitted_notes)
    coverage = _compose_snapshot_coverage(prompt_receipts)
    if out_prompt_receipts is not None:
        out_prompt_receipts.extend(prompt_receipts)
    return LLMMessage(
        role="user",
        content=preamble + "\n\n".join(blocks) + coverage + notice,
    )
