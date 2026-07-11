"""Bounded model-facing shell output."""

from __future__ import annotations

from typing import Any

_SHELL_OUTPUT_HEAD_CHARS = 1_000
_SHELL_OUTPUT_TAIL_CHARS = 3_000
_SHELL_OUTPUT_CAP_CHARS = _SHELL_OUTPUT_HEAD_CHARS + _SHELL_OUTPUT_TAIL_CHARS


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
