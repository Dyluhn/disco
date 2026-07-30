"""Render-time helpers for event payloads — elision, snipping, and truncation.

These functions are pure + deterministic (same input → same output) so they
preserve ``View.of`` purity.  They operate on payload value objects defined in
``_event_types`` and are consumed by the concrete event ``to_llm_message``
methods in ``_event_interaction``, ``_event_outputs``, and by external
consumers (the tool executor, the provider adapter).
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any

from ._event_types import LLMMessage

# A-S2 (the Snip shaper): the per-observation char cap applied at RENDER time
# (to_llm_message), NOT at storage. The full content stays in the event payload
# (and on disk for spilled artifacts) — only the LLM-facing message is trimmed.
# Reversible: the model re-runs the tool or file_reads the artifact for the rest.
_OBS_SNIP_CHARS = 8_000
_OBS_SNIP_HEAD = 5_000
_OBS_SNIP_TAIL = 2_000

# CW-6 — per-build override for the observation snip cap. to_llm_message() is a pure
# projection method with no tier/window access, but the snip MUST be raised for the
# assist-OFF (capable) tier in tandem with the read budget — otherwise a large
# file_read observation is snipped to a corrupted head/tail and the model re-reads
# forever. ViewBuilder.build sets this from the derived ContextCaps for the duration
# of a build; default None → the byte-identical assist-ON 8k snip. Task-local
# (ContextVar) so concurrent conversations of different tiers never cross-contaminate.
obs_snip_override: ContextVar[int | None] = ContextVar("disco_obs_snip_override", default=None)

# H2: elide large ARGUMENT values (e.g. a file_write's full body) at RENDER time.
# A written file's content doesn't belong in the action history every turn — it's on
# disk + readable. Re-sending a 59 KB body each action is the dominant cost bloat.
# Pure + deterministic (preserves View.of purity); reversible (the marker tells the
# model the file exists + to file_read it).
_ARG_SNIP_CHARS = 1_500

# CW-3 — the leading marker of the always-fresh workspace snapshot block (the pinned
# CURRENT WORKSPACE message rendered by view_render.workspace_snapshot_message). Shared
# here (the lowest common module both the loop renderer and the provider import) so the
# provider can locate the block to place an Anthropic cache breakpoint after it, and so
# recovery/pointer prose can reference it by a LOCATION-INDEPENDENT name ("the CURRENT
# WORKSPACE block in this prompt" — never "below"/"above": the block moved to the
# cacheable prefix, so directional words are wrong).
WORKSPACE_SNAPSHOT_SENTINEL = "# CURRENT WORKSPACE"

# K1 — the elision-marker family. `_snip_args` renders an over-long arg as a
# placeholder in the action history. A weak model can COPY that placeholder back
# into a REAL tool argument (e.g. a file_write body), which — if executed — would
# write the marker over real content (DATA LOSS) and re-feed the marker into the
# next file_read (an 88× read loop, reproduced live). The emitted marker is an
# instruction-like sentinel, not flowing prose. The detector below matches BOTH
# the canonical sentinel and historical angle-bracket markers, so old histories
# stay guarded while new histories are less copyable.
_ELISION_MARKER_RE = re.compile(
    r"(?:"
    r"\[\[\s*DISCO-ELIDED:\s*\d[\d,]*\s*chars\b[^\]]*?\]\]"
    r"|"
    r"<\s*\d[\d,]*\s*chars\b[^>]*?\b(?:elided|full content)\b[^>]*>"
    r")"
)
# F01 — fail closed on protocol fragments too.  The historical poisoned file
# contained ``[[DISCO-ELIDED: see above — 6837 char file content ...]]``, which
# is recognizably the reserved sentinel but is not the canonical count-first
# rendering above.  Stream/provider truncation can also drop the final brackets.
# Requiring the reserved bracketed prefix avoids matching ordinary prose that
# merely uses the word "elided"; an optional single opening bracket covers a
# partially emitted prefix.
_ELISION_PARTIAL_RE = re.compile(r"\[{1,2}\s*DISCO-ELIDED\s*:", re.IGNORECASE)
# Historical angle-bracket markers can likewise arrive without their closing
# ``>``.  Require both the count/content anchor and an elision signature within
# one bounded line, so benign HTML and phrases such as ``<5 chars>`` stay valid.
_ELISION_ANGLE_PARTIAL_RE = re.compile(
    r"<\s*(?:\d[\d,]*\s*chars?|content)\b[^\r\n>]{0,500}"
    r"\b(?:elided|placeholder|full\s+content)\b",
    re.IGNORECASE,
)
# CW P1-a — capture the char count from an existing marker so the assist-OFF retarget
# pass can re-render it (pinned vs non-pinned) without re-deriving the original length.
_ELISION_COUNT_RE = re.compile(r"(?:<\s*|\[\[\s*DISCO-ELIDED:\s*)(\d[\d,]*)\s*chars\b")

# BW-02 (trace conv_20fa8482) — a model can PARAPHRASE the neutral marker, dropping the
# leading "<N chars …>" anchor while copying the marker's stable TAIL prose verbatim into
# a real tool argument (observed: "<content elided — re-issue the call or file_read the
# path for the full content; do not copy this placeholder into a tool argument>"). With
# no digit anchor, `_ELISION_MARKER_RE` misses it, so K1 passed it and a 132-byte
# placeholder overwrote a real file (DATA LOSS → build corruption → degenerate tool-less
# resumes). This SECOND detector is for the REJECTION path ONLY: it matches a bounded
# angle-bracket placeholder `<…>` (no greedy cross-`>`) that carries BOTH the marker's
# signature TAIL phrase AND an elision keyword — requiring BOTH so it does NOT
# false-positive on ordinary file content that merely says "elided" in prose. It is NOT
# used by the RETARGET pass (which still needs the char count from `_ELISION_COUNT_RE` to
# reconstruct the neutral marker, and a paraphrase carries no count to reconstruct).
_ELISION_PARAPHRASE_RE = re.compile(
    r"<"
    r"(?=[^>]*\b(?:elided|full content|placeholder)\b)"
    r"[^>]*"
    # signature TAIL phrases — kept in lockstep with `_arg_snip_marker_neutral`. Legacy
    # phrases stay (back-compat for any in-flight marker); the de-temptified wording adds
    # "do not copy or re-send" + "already applied to the workspace".
    r"(?:re-issue the call or file_read the path|do not copy this placeholder"
    r"|do not copy or re-send|already applied to the workspace)"
    r"[^>]*>"
)

# Execution-receipt trailer: bound on the trusted exit_code magnitude. Real OS
# exit statuses fit in a byte and signal-death encodings in ~3 digits; anything
# outside this window is not a plausible exit status, so the trailer is omitted
# rather than rendering an unbounded (or hostile) payload value to the model.
_EXIT_RECEIPT_MAX_ABS = 999_999

# The per-sub-question ROUND cap ("rounds") bounds iteration DEPTH, not coverage —
# every sub-question still produces a full section — so it is NOT a truncation and must
# never surface as "bounded by / some sub-questions were not covered". Only genuine
# coverage truncations (e.g. sources / wall_clock / subquestions) are surfaced.
_NON_TRUNCATING_BOUNDS = frozenset({"rounds"})


def snip_content(content: str, *, max_chars: int, head: int, tail: int) -> str:
    """Trim an over-long string to head + tail with a recoverable marker. Pure +
    deterministic (same input → same output) so it preserves View.of purity."""
    if len(content) <= max_chars:
        return content
    dropped = len(content) - head - tail
    return (
        f"{content[:head]}\n"
        f"… [snipped {dropped:,} chars — re-run the tool or use file_read for the full output] …\n"
        f"{content[-tail:]}"
    )


def _arg_snip_marker(n: int) -> str:
    return (
        f"[[DISCO-ELIDED: {n:,} chars — history display only; metadata, not file content; "
        "this historical tool argument was already submitted. Do not copy or "
        "re-send this marker. Use current resource state and request only the "
        "minimal range needed for the next action.]]"
    )


# Back-compat helper name: callers/tests may still import the old assist-ON marker
# constructor, but the emitted format is now one canonical sentinel.
def _arg_snip_marker_below(n: int) -> str:
    return _arg_snip_marker(n)


# Back-compat helper name: the assist-OFF retarget pass now also emits the same
# sentinel, preserving one canonical marker across render paths.
def _arg_snip_marker_neutral(n: int) -> str:
    return _arg_snip_marker(n)


def _snip_args(arguments: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in arguments.items():
        if isinstance(v, str) and len(v) > _ARG_SNIP_CHARS:
            out[k] = _arg_snip_marker(len(v))
        else:
            out[k] = v
    return out


def retarget_elided_arg_markers(messages: list[LLMMessage]) -> list[LLMMessage]:
    """CW P1-a (round-2) — assist-OFF render pass: rewrite each elided tool-call
    ARGUMENT marker to the NEUTRAL, non-dangling marker.

    Historical render paths emitted tier-specific angle-bracket markers. This
    pass now rewrites any count-bearing elision marker to the canonical sentinel
    with NO per-arg pinned-vs-omitted guessing. Pure: returns a new list; input
    unchanged.
    """
    out: list[LLMMessage] = []
    for msg in messages:
        if msg.role != "assistant" or not msg.tool_calls:
            out.append(msg)
            continue
        new_tcs: list[dict[str, Any]] = []
        mutated = False
        for tc in msg.tool_calls:
            if not isinstance(tc, dict):
                new_tcs.append(tc)
                continue
            args = tc.get("arguments")
            if not isinstance(args, dict):
                new_tcs.append(tc)
                continue
            new_args: dict[str, Any] | None = None
            for k, v in args.items():
                if not isinstance(v, str):
                    continue
                count = _ELISION_COUNT_RE.match(v)
                if count is None or _ELISION_MARKER_RE.search(v) is None:
                    continue  # not one of our markers — leave the model's arg alone
                n = int(count.group(1).replace(",", ""))
                replacement = _arg_snip_marker_neutral(n)
                if replacement == v:
                    continue
                if new_args is None:
                    new_args = dict(args)
                new_args[k] = replacement
            if new_args is not None:
                new_tc = dict(tc)
                new_tc["arguments"] = new_args
                new_tcs.append(new_tc)
                mutated = True
            else:
                new_tcs.append(tc)
        out.append(msg.model_copy(update={"tool_calls": new_tcs}) if mutated else msg)
    return out


def find_elided_arg_markers(arguments: dict[str, object]) -> list[str]:
    """K1 execution guard: return argument paths whose string value carries an
    elision placeholder (the `_snip_args` marker copied back by a weak model).
    Empty list ⇒ the arguments are clean and safe to execute. Pure + deterministic.

    Matches BOTH the canonical `[[DISCO-ELIDED: N chars ...]]` marker, historical
    structural `<N chars … {elided|full content} …>` markers, and a model-
    PARAPHRASED placeholder that dropped the count anchor but kept the marker's
    signature tail prose (BW-02), and reserved/malformed protocol fragments (F01).
    Recurses through JSON objects and arrays so nested edit batches, JSON Patch,
    semantic app content, and future structured mutators cannot bypass the
    universal executor boundary. Rejection-only — retargeting remains count-based."""

    def _walk(value: object, path: str) -> list[str]:
        if isinstance(value, str):
            if any(
                pattern.search(value) is not None
                for pattern in (
                    _ELISION_MARKER_RE,
                    _ELISION_PARAPHRASE_RE,
                    _ELISION_PARTIAL_RE,
                    _ELISION_ANGLE_PARTIAL_RE,
                )
            ):
                return [path]
            return []
        if isinstance(value, dict):
            found: list[str] = []
            for key, nested in value.items():
                child = f"{path}.{key}" if path else str(key)
                found.extend(_walk(nested, child))
            return found
        if isinstance(value, (list, tuple)):
            found = []
            for index, nested in enumerate(value):
                found.extend(_walk(nested, f"{path}[{index}]"))
            return found
        return []

    return _walk(arguments, "")


def value_is_only_elision_marker(value: object) -> bool:
    """True when `value` is a string consisting of NOTHING BUT an elision
    placeholder (plus surrounding whitespace) — i.e. the model copied the
    `_snip_args` marker back verbatim with no real content of its own around it.

    Used by the K1 recovery path: a PURE marker copy-back can be safely
    re-expanded to the original content the marker stood in for (the engine still
    holds it in the event log), because re-expansion can't clobber any real text
    the model authored — there is none. A marker EMBEDDED in real text (the model
    wrote a header, a marker, a footer) returns False so the recovery never
    silently drops the model's surrounding edits; that case falls through to the
    rejection-and-re-read path instead. Pure + deterministic."""
    if not isinstance(value, str):
        return False
    stripped = _ELISION_ANGLE_PARTIAL_RE.sub(
        "",
        _ELISION_PARTIAL_RE.sub(
            "", _ELISION_PARAPHRASE_RE.sub("", _ELISION_MARKER_RE.sub("", value))
        ),
    )
    # Nothing matched ⇒ not a marker at all; or matched but real text remains.
    return value != stripped and stripped.strip() == ""


def _execution_receipt_trailer(structured: dict[str, Any] | None) -> str | None:
    """Render the compact execution receipt (e.g. ``[exit 0]``) for an exec-family
    observation, or None when the structured payload carries no exit status.

    Success-path exec observations render only stdout to the model, while the
    exit status the host already proved lives solely in ``structured`` — so the
    model would re-run a command purely to learn whether it succeeded (the
    TOOL_CALL_THRASH failure). Surfacing the receipt at RENDER time keeps the
    durable event bytes (and everything downstream of them: frontend transcript,
    exact-read receipt matching, spill caps, output-truth stdout comparisons)
    byte-identical, and applies retroactively to already-stored events.

    Strictly sourced from the tool's own structured payload — never synthesized:
    a payload without a plausible integer ``exit_code`` yields no trailer.
    Tool-agnostic by construction (any tool whose structured result reports an
    exit_code benefits); pure + deterministic so View.of purity is preserved.
    """
    if not structured:
        return None
    exit_code = structured.get("exit_code")
    # bool is an int subclass — a True/False "exit_code" is not a real status.
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return None
    if abs(exit_code) > _EXIT_RECEIPT_MAX_ABS:
        return None
    if structured.get("timed_out") is True:
        return f"[exit {exit_code}, timed out]"
    return f"[exit {exit_code}]"


def report_truncation(bounded_by: str | None) -> str | None:
    """The ``bounded_by`` value to SURFACE as a real truncation, or None when coverage
    completed (a natural finish, or the non-truncating 'rounds' depth cap). Shared by
    every consumer (screen notice, PDF/markdown export, LLM-context header) so they
    never diverge on what counts as a truncation."""
    b = (bounded_by or "").strip()
    return b if b and b not in _NON_TRUNCATING_BOUNDS else None


__all__ = [
    "WORKSPACE_SNAPSHOT_SENTINEL",
    "_ARG_SNIP_CHARS",
    "_ELISION_ANGLE_PARTIAL_RE",
    "_ELISION_COUNT_RE",
    "_ELISION_MARKER_RE",
    "_ELISION_PARAPHRASE_RE",
    "_ELISION_PARTIAL_RE",
    "_EXIT_RECEIPT_MAX_ABS",
    "_NON_TRUNCATING_BOUNDS",
    "_OBS_SNIP_CHARS",
    "_OBS_SNIP_HEAD",
    "_OBS_SNIP_TAIL",
    "_arg_snip_marker",
    "_arg_snip_marker_below",
    "_arg_snip_marker_neutral",
    "_execution_receipt_trailer",
    "_snip_args",
    "find_elided_arg_markers",
    "obs_snip_override",
    "report_truncation",
    "retarget_elided_arg_markers",
    "snip_content",
    "value_is_only_elision_marker",
]
