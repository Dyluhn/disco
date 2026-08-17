"""Moved rel 6 finding 2 shell collection implementations."""

from __future__ import annotations

from ._shared import status


def _impl_test_shell_removes_requires_exact_path_and_pure_rm():
    from harness.build_soak.adapters.disco_api import _shell_removes

    # The export-flow false positive: rm of a COPY must not mark the root deliverable absent.
    assert not _shell_removes("rm export/index.html", "index.html")
    # Compound commands are not deterministic deletes.
    assert not _shell_removes("zip site.zip index.html && rm index.html", "index.html")
    assert not _shell_removes("cp index.html /tmp; rm index.html", "index.html")
    # rm not the program.
    assert not _shell_removes("echo rm index.html", "index.html")
    # The genuine case still detects.
    assert _shell_removes("rm index.html", "index.html")
    assert _shell_removes("rm -f index.html", "index.html")
    assert _shell_removes("rm -- 'space path/index page.html'", "space path/index page.html")
    # Recursive parent deletes mark declared children absent, but non-recursive parent rm does not.
    assert _shell_removes("rm -rf 'site output'", "site output/index.html")
    assert _shell_removes("rm -r -- 'site output'", "site output/nested/index.html")
    assert not _shell_removes("rm -f 'site output'", "site output/index.html")
    # A pure two-path mv removes the declared source from its original path.
    assert _shell_removes("mv 'index page.html' archive/index.html", "index page.html")
    assert _shell_removes(
        "mv -- 'space path/index.html' archive/index.html",
        "space path/index.html",
    )
    assert not _shell_removes("mv export/index.html index.html", "index.html")


def _impl_test_verified_is_terminal_in_adapter_and_event_predicates():
    from harness.build_soak.adapters.disco_api import TERMINAL_STATES
    from harness.build_soak.events import (
        EXECUTION_EXPECTED_TERMINALS,
        TERMINAL_STATUSES,
        terminal_status,
    )

    assert "VERIFIED" in TERMINAL_STATES
    assert "VERIFIED" in TERMINAL_STATUSES
    assert "VERIFIED" in EXECUTION_EXPECTED_TERMINALS
    assert terminal_status([status(1, "VERIFIED")]) == "VERIFIED"
