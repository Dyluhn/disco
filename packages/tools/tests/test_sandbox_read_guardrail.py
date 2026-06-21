"""W10 — static guardrail locking in W1 (typed sandbox read errors).

Prevents the whack-a-mole regression codex warned about: a NEW sandbox backend — or an edit
to an existing one — that raises a BARE ``SandboxError`` on a failed read re-opens the
silent-hang trigger, because the ~8 ``except (FileNotFoundError, OSError)`` handlers across
the loop will not catch it. Every backend read MUST funnel through ``raise_read_error`` (which
types a missing file as ``SandboxFileNotFoundError``). The ONLY legitimate
``raise SandboxError(... read_file ...)`` is inside ``raise_read_error`` itself (base.py).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.boundary_contract

_SANDBOX_DIR = pathlib.Path(__file__).resolve().parents[1] / "src/disco/tools/sandbox"
_BARE_READ_RAISE = re.compile(r"""raise\s+SandboxError\(\s*f?["']read_file""")


def test_no_backend_raises_bare_sandbox_error_on_read():
    offenders: list[str] = []
    for f in sorted(_SANDBOX_DIR.glob("*.py")):
        if f.name == "base.py":
            continue  # the boundary helper (raise_read_error) legitimately raises here
        for lineno, line in enumerate(f.read_text().splitlines(), 1):
            if _BARE_READ_RAISE.search(line):
                offenders.append(f"{f.name}:{lineno}")
    assert not offenders, (
        "A bare SandboxError on a read defeats the W1 typed-error contract — the loop's "
        "`except (FileNotFoundError, OSError)` bookkeeping handlers won't catch it and the "
        f"build hangs at RUNNING. Use raise_read_error() instead. Offenders: {offenders}"
    )


def test_raise_read_error_is_the_shared_boundary():
    """raise_read_error must still exist + type missing files (the guardrail's premise)."""
    import pytest
    from disco.tools.sandbox.base import SandboxFileNotFoundError, raise_read_error

    with pytest.raises(SandboxFileNotFoundError):
        raise_read_error("x", b"cat: x: No such file or directory")
