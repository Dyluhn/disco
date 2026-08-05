"""Source-locality negative control — Next.js / Vercel vocabulary must not
leak into target-neutral Core directories.

This is the 15-L1 tier-L locality guard. It scans the ACTUAL prohibited
directories (``loop/``, ``loop/finish/``, ``store/``, and ``build_platform/``)
and asserts they contain no Next.js- or provider-specific command-recognition
vocabulary.

Two things the contract demands this guard distinguish:

1. **Generic Python ``next_*`` identifiers are NOT target vocabulary.** The
   loop uses ``next_plan_revision``, ``next_action``, ``.next_action`` and
   ``current.next_action`` routinely; a naive substring scan would flag them.
   This test only flags the exact prohibited tokens: ``nextjs``, ``.next``,
   ``next build``, ``next start``, ``vercel``, and a literal
   ``head == "next"``/``head == 'next'`` command branch.
2. **Target vocabulary is adapter-owned.** The Next.js target adapters and
   connectors under ``disco.core.targets`` legitimately carry this vocabulary;
   this guard intentionally does not scan them.

A negative control must fail when the prohibited branch is reintroduced, so
the final test also drives ``_segment_launches_local_server`` directly and
asserts the build-tool seam no longer special-cases a ``next`` command token.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from disco.core.loop import plan_command_validation as pv

_SOURCE_ROOT = Path(inspect.getfile(pv)).resolve().parent.parent

# ``_SOURCE_ROOT`` resolves to ``.../packages/core/src/disco/core``.
_PROHIBITED_DIRECTORIES = (
    _SOURCE_ROOT / "loop",
    _SOURCE_ROOT / "loop" / "finish",
    _SOURCE_ROOT / "store",
    _SOURCE_ROOT / "build_platform",
)

# ``.next`` (the output directory) must be a standalone token, not a substring:
# ``.next_plan_revision`` / ``.next_action`` are generic member accesses that
# legitimately appear throughout the loop. Only a bare ``.next`` preceded by a
# non-identifier boundary and followed by a non-identifier boundary is flagged.
_PROHIBITED_RE = re.compile(r"(?<![A-Za-z0-9_.])\.next(?![A-Za-z0-9_])", re.IGNORECASE)

# Exact command-branch spellings that would reintroduce a target-specific
# server-start recognition into the Core command classifier.
_PROHIBITED_BRANCHES = ('head == "next"', "head == 'next'")


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for directory in _PROHIBITED_DIRECTORIES:
        if not directory.is_dir():
            continue
        files.extend(directory.rglob("*.py"))
    return sorted(files)


def _is_generic_identifier(token: str) -> bool:
    """Generic Python ``next*`` identifiers are not target vocabulary."""
    lowered = token.casefold()
    if lowered == "next":
        return True
    if lowered.startswith("next_") or lowered.startswith(".next_"):
        return True
    return lowered.endswith(".next_action")


def test_locality_scan_covers_non_empty_source_directories() -> None:
    """Zero-denominator guard: the scan must find real Python modules."""
    files = _iter_source_files()
    assert len(files) >= 10, (
        f"locality scan found only {len(files)} modules; the source directories "
        f"are not being scanned"
    )


def test_prohibited_target_vocabulary_is_absent_from_core_directories() -> None:
    """No Next.js/Vercel string appears in the prohibited Core directories."""
    offenders: list[str] = []
    for path in _iter_source_files():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            lowered = line.casefold()
            for token in ("nextjs", "next build", "next start", "vercel"):
                if token in lowered:
                    offenders.append(f"{path}:{number}: {line.strip()}")
            if _PROHIBITED_RE.search(lowered):
                offenders.append(f"{path}:{number}: {line.strip()}")
    assert not offenders, "target vocabulary leaked into Core:\n" + "\n".join(offenders)


def test_target_specific_command_branch_is_absent_from_core_classifier() -> None:
    """No ``head == \"next\"`` command-recognition branch remains in Core."""
    target = _SOURCE_ROOT / "loop" / "plan_command_validation.py"
    assert target.is_file(), f"expected {target} to exist"
    for number, line in enumerate(target.read_text().splitlines(), 1):
        for branch in _PROHIBITED_BRANCHES:
            assert branch not in line, (
                f"target-specific command branch reintroduced at {target}:{number}"
            )


def test_build_tool_classifier_no_longer_special_cases_next_command() -> None:
    """Negative control: the build-tool seam treats ``next`` like any other tool.

    A bare ``next`` with no server-start verb is NOT a local-server launch, while
    ``next <server-start-verb>`` still is via the generic rule.
    """
    assert pv._segment_launches_local_server(["next", "build"]) is False
    assert pv._segment_launches_local_server(["next", "start"]) is True
    assert pv._segment_launches_local_server(["next", "dev"]) is True
    assert pv._segment_launches_local_server(["next", "info"]) is False
    assert pv._segment_launches_local_server(["next", "telemetry"]) is False
