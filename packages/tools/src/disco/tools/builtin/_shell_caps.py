"""Bounded model-facing shell output."""

from __future__ import annotations

from typing import Any

_SHELL_OUTPUT_HEAD_CHARS = 1_000
_SHELL_OUTPUT_TAIL_CHARS = 3_000
_SHELL_OUTPUT_CAP_CHARS = _SHELL_OUTPUT_HEAD_CHARS + _SHELL_OUTPUT_TAIL_CHARS

# UTF-8 subprocess output has already crossed a backend decoder by the time it
# reaches a tool.  These are the characters that prove the decoded value was not
# ordinary terminal text: forbidden C0/C1 controls (line-oriented whitespace is
# intentionally allowed) or U+FFFD inserted for undecodable bytes.  Sanitizing at
# this boundary keeps the original value out of ToolOutcome content, structured
# fields, event details, and overflow spill files.
_TEXT_CONTROLS = frozenset({"\t", "\n", "\r"})
_DECODE_REPLACEMENT = "\ufffd"


def sanitize_execution_output(text: str, *, stream: str) -> tuple[str, dict[str, int] | None]:
    """Replace binary/control-bearing execution output with bounded metadata.

    Backends decode process bytes before constructing ``ExecResult``.  A decode
    replacement or a control character other than normal terminal whitespace is
    therefore a fail-closed signal: do not quote, truncate, or spill any part of
    that stream.  The replacement tells the model how to retain binary output as
    a workspace artifact without exposing its bytes through the event log.

    Valid UTF-8 text, including non-ASCII text and ordinary tabs/newlines, is
    returned byte-for-byte unchanged.
    """
    control_chars = sum(
        1
        for char in text
        if (ord(char) < 32 or 127 <= ord(char) <= 159) and char not in _TEXT_CONTROLS
    )
    decode_replacements = text.count(_DECODE_REPLACEMENT)
    if control_chars == 0 and decode_replacements == 0:
        return text, None

    metadata = {
        "decoded_chars": len(text),
        "control_chars": control_chars,
        "decode_replacements": decode_replacements,
    }
    replacement = (
        f"[disco: binary/control {stream} omitted; {len(text)} decoded chars, "
        f"{control_chars} control chars, {decode_replacements} decode replacements. "
        "Redirect the command output to a workspace artifact (for example, "
        "`... > output.bin 2>&1`) and inspect the artifact's metadata instead.]"
    )
    return replacement, metadata


def cap_shell_observation(
    text: str, *, spill_path: str | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Keep head+tail shell output with an explicit recovery hint.

    When the caller SAVED the full output (`spill_path`), the marker points at
    it — evidence is preserved, never discarded. Only when no spill was
    possible does the marker fall back to the rerun-redirected hint (a rerun
    is not always reproducible, so the spill path is strongly preferred)."""
    if len(text) <= _SHELL_OUTPUT_CAP_CHARS:
        return text, None
    dropped = len(text) - _SHELL_OUTPUT_CAP_CHARS
    if spill_path:
        marker = (
            f"\n[... omitted {dropped} chars — FULL output saved to "
            f"{spill_path}; file_read it if you need the rest.]\n"
        )
    else:
        marker = (
            f"\n[... omitted {dropped} chars; full output not retained — rerun "
            "redirected to a file (e.g. `... > out.log 2>&1`) and file_read it if "
            "you need it all.]\n"
        )
    return (
        text[:_SHELL_OUTPUT_HEAD_CHARS] + marker + text[-_SHELL_OUTPUT_TAIL_CHARS:],
        {
            "output_truncated": True,
            "output_chars": len(text),
            "dropped_chars": dropped,
            **({"spill_path": spill_path} if spill_path else {}),
        },
    )
