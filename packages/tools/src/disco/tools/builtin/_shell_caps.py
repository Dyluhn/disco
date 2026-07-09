"""Bounded model-facing shell output."""

from __future__ import annotations

from typing import Any

_SHELL_OUTPUT_HEAD_CHARS = 1_000
_SHELL_OUTPUT_TAIL_CHARS = 3_000
_SHELL_OUTPUT_CAP_CHARS = _SHELL_OUTPUT_HEAD_CHARS + _SHELL_OUTPUT_TAIL_CHARS


def cap_shell_observation(text: str) -> tuple[str, dict[str, Any] | None]:
    """Keep head+tail shell output with an explicit recovery hint."""
    if len(text) <= _SHELL_OUTPUT_CAP_CHARS:
        return text, None
    dropped = len(text) - _SHELL_OUTPUT_CAP_CHARS
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
        },
    )
