"""Register #7 — a module that is SHIPPED STANDALONE and executed as a script
must not acquire package-relative imports.

This class of defect is invisible to every gate the campaign already runs, which
is why it survived four sub-epics:

* basedpyright resolves a relative import perfectly well — the defect exists
  only under script execution, never under module import;
* the daemon's own unit tests import it as ``disco.tools.builtin._browser_daemon``,
  i.e. as a package member, which is not how the product runs it;
* nothing in the lane battery ships a file into a sandbox.

So the only honest check is to execute the artifact the way it actually ships.
These tests do that, and they take the shipped file set from the PRODUCT'S OWN
SHIPPING CODE rather than restating it — a test that shipped its own idea of the
file set could agree with itself while the product shipped something else, which
is precisely the failure being guarded against.

Occurrence 1 (noted, never violated): the trusted-component ``probe/probe.py``
files, which the AppKit adjudication of 2026-08-01 records as needing to stay
"single self-contained files — they execute standalone in a sandboxed target app
where no sibling module exists".

Occurrence 2 (violated in fact): campaign commit ``cb19126a`` gave
``_browser_daemon.py`` package-relative imports while
``browser_parts.ensure_daemon._ship_daemon_files`` kept shipping it as a lone
script, so it died at line 22 before any browser logic ran.

The campaign's core activity is decomposition, so new standalone artifacts are
expected. Add them to ``_SELF_CONTAINED_ARTIFACTS`` (or give them a shipper case
of their own) rather than trusting review to notice.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

# Executes an artifact with the import semantics of ``python <artifact>``: no
# parent package and its own directory leading sys.path. ``run_name`` is
# deliberately NOT ``__main__`` — the artifact's entrypoint guard must stay shut
# so this remains a pure import-time check that starts no browser and binds no
# port, while ``__package__`` is still the empty string a script really gets.
_STANDALONE_RUNNER = """
import os
import runpy
import sys

target = sys.argv[1]
sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
runpy.run_path(target, run_name="__disco_standalone_import_probe__")
"""

_IMPORT_FAILURE_MARKERS = ("ImportError", "ModuleNotFoundError")

def _registry_data_root() -> pathlib.Path:
    """Resolved through the package itself, never by counting path parents."""
    from disco.core import trusted_components

    return pathlib.Path(trusted_components.__file__).parent / "registry_data"


def _discover_self_contained_artifacts() -> list[pathlib.Path]:
    """Every trusted-component probe, discovered rather than transcribed.

    These execute standalone from where they already live, so their "shipped
    file set" is exactly the one file. Discovering them means a component added
    later is covered without anyone remembering to add it here, and no version
    number is pinned into a test that has no opinion about versions.
    """
    return sorted(_registry_data_root().glob("*/*/probe/probe.py"))


_SELF_CONTAINED_ARTIFACTS = _discover_self_contained_artifacts()


def _execute_standalone(artifact: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _STANDALONE_RUNNER, str(artifact)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_no_import_failure(done: subprocess.CompletedProcess[str], artifact: str) -> None:
    """The artifact's module body must run to completion.

    A non-import failure is out of scope here and is deliberately NOT asserted
    against: this guard owns one invariant, and widening it into a smoke test
    would make it fail for reasons that have nothing to do with register #7.
    """
    output = f"{done.stdout}\n{done.stderr}"
    for marker in _IMPORT_FAILURE_MARKERS:
        assert marker not in output, (
            f"{artifact} died at import when executed standalone "
            f"(register #7): {output.strip()[-800:]}"
        )
    assert done.returncode == 0, (
        f"{artifact} did not execute standalone: {output.strip()[-800:]}"
    )


class _RecordingSandbox:
    """Captures exactly the file set the product ships, under a temp root."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.written: list[str] = []

    async def write_file(self, path: str, data: bytes) -> None:
        self.written.append(path)
        target = self.root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class _ShipContext:
    def __init__(self, sandbox: _RecordingSandbox) -> None:
        self.sandbox = sandbox


@pytest.mark.asyncio
async def test_shipped_browser_daemon_executes_standalone(tmp_path):
    """The daemon's SHIPPED file set — not the repo tree — must import cleanly.

    Shipped by ``_ship_daemon_files`` itself, so the daemon and its shipper are
    proven to agree on the interior-parts package name at runtime instead of by
    comparing two string literals that can drift apart silently.
    """
    from disco.tools.builtin import browser
    from disco.tools.builtin.browser_parts import ensure_daemon

    sandbox = _RecordingSandbox(tmp_path)
    builtin_dir = pathlib.Path(browser.__file__).parent
    await ensure_daemon._ship_daemon_files(
        _ShipContext(sandbox),
        builtin_dir / "_browser_daemon.py",
        builtin_dir / "live_view.py",
        browser._DAEMON_PATH,
    )

    shipped_daemon = tmp_path / browser._DAEMON_PATH.lstrip("/")
    assert shipped_daemon.is_file(), f"shipper wrote {sandbox.written}"
    _assert_no_import_failure(_execute_standalone(shipped_daemon), "_browser_daemon.py")


def test_self_contained_artifact_discovery_is_not_empty():
    """Zero-denominator guard.

    A glob that matched nothing would make the parametrized test below pass by
    having no work to do, which reads identically to passing on real evidence.
    """
    assert _SELF_CONTAINED_ARTIFACTS, (
        f"no trusted-component probes discovered under {_registry_data_root()}"
    )


@pytest.mark.parametrize(
    "artifact",
    _SELF_CONTAINED_ARTIFACTS,
    ids=lambda path: f"{path.parents[2].name}-{path.parents[1].name}",
)
def test_self_contained_artifacts_execute_standalone(artifact):
    _assert_no_import_failure(_execute_standalone(artifact), str(artifact))


def test_standalone_runner_rejects_a_package_relative_import(tmp_path):
    """Positive control — the runner must be able to FAIL.

    Without this, a runner that silently stopped executing anything would report
    every artifact clean, and the guard would be a green light wired to nothing.
    The synthetic artifact below is the exact shape of the ``cb19126a``
    regression: a lone script importing a sibling package relatively.
    """
    parts = tmp_path / "_synthetic_parts"
    parts.mkdir()
    (parts / "__init__.py").write_text("", encoding="utf-8")
    (parts / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    artifact = tmp_path / "_synthetic_daemon.py"
    artifact.write_text("from ._synthetic_parts import helper as helper\n", encoding="utf-8")

    done = _execute_standalone(artifact)

    assert done.returncode != 0
    assert "attempted relative import with no known parent package" in done.stderr
