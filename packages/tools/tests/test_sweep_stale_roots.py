"""Unit tests for ProcessSandboxService.sweep_stale_roots (B5).

Verifies that the startup sweep:
- removes stale pmx-sbx-* sibling roots older than the threshold
- NEVER removes the live _root
- NEVER touches dirs not matching the pmx-sbx- prefix
- is symlink-safe (symlinks left untouched)
- is best-effort: a busy / un-removable dir does NOT raise
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from disco.tools.sandbox.process import ProcessSandboxService


def _make_dir(parent: Path, name: str, age_s: float) -> Path:
    """Create a directory under *parent* with an mtime *age_s* seconds in the past."""
    d = parent / name
    d.mkdir()
    old_time = time.time() - age_s
    os.utime(d, (old_time, old_time))
    return d


@pytest.mark.asyncio
async def test_sweep_stale_roots_removes_old_sibling_roots():
    """Main acceptance: 2 old pmx-sbx-* roots removed, live root and unrelated dir
    both survive."""
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)

        # Live root — give it to ProcessSandboxService directly so _root == live_root.
        live_root = parent / "pmx-sbx-live"
        live_root.mkdir()
        svc = ProcessSandboxService(root=str(live_root))
        assert svc._root == live_root.resolve()

        # Two stale sibling roots (2 days old — well past the 1-day default threshold).
        old1 = _make_dir(parent, "pmx-sbx-old1", age_s=2 * 86400)
        old2 = _make_dir(parent, "pmx-sbx-old2", age_s=3 * 86400)

        # A non-pmx-sbx- directory that must NEVER be touched.
        unrelated = _make_dir(parent, "other-service-dir", age_s=5 * 86400)

        # Run the sweep with a 1-day threshold.
        removed = await svc.sweep_stale_roots(max_age_s=86400)

        assert removed == 2, f"expected 2 removed, got {removed}"
        assert not old1.exists(), "old1 should have been removed"
        assert not old2.exists(), "old2 should have been removed"
        assert live_root.exists(), "live root must not be removed"
        assert unrelated.exists(), "non-prefix dir must not be touched"


@pytest.mark.asyncio
async def test_sweep_stale_roots_skips_fresh_sibling_roots():
    """A recently created sibling root (within threshold) is not swept."""
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)

        live_root = parent / "pmx-sbx-live"
        live_root.mkdir()
        svc = ProcessSandboxService(root=str(live_root))

        # Fresh sibling — only 1 minute old, threshold is 1 day.
        fresh = _make_dir(parent, "pmx-sbx-fresh", age_s=60)

        removed = await svc.sweep_stale_roots(max_age_s=86400)

        assert removed == 0, "fresh sibling should not be swept"
        assert fresh.exists(), "fresh sibling must survive"
        assert live_root.exists(), "live root must survive"


@pytest.mark.asyncio
async def test_sweep_stale_roots_skips_symlinks():
    """Symlinks matching the prefix are left untouched (never followed/deleted)."""
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)

        # Real target dir (old, so it would match by age if it were a real dir).
        target = _make_dir(parent, "pmx-sbx-real-target", age_s=2 * 86400)

        live_root = parent / "pmx-sbx-live"
        live_root.mkdir()
        svc = ProcessSandboxService(root=str(live_root))

        # Symlink with matching prefix pointing at the target.
        link = parent / "pmx-sbx-link"
        link.symlink_to(target)
        # Age the target enough that it would be swept if treated as a real dir.
        # (link itself is freshly created, but lstat would show link's own mtime.)
        old_time = time.time() - 2 * 86400
        os.utime(link, (old_time, old_time), follow_symlinks=False)

        removed = await svc.sweep_stale_roots(max_age_s=86400)

        # The symlink must not be removed (symlink guard), the target may or may not
        # be swept (it IS a real dir matching prefix), but the symlink itself survives.
        assert link.exists() or link.is_symlink(), "symlink itself must not be removed"
        assert live_root.exists()


@pytest.mark.asyncio
async def test_sweep_stale_roots_does_not_raise_on_busy_dir():
    """Best-effort: a pmx-sbx-* dir that cannot be removed (parent read-only,
    contents un-removable) must NOT raise — the sweep continues, the busy dir
    survives, and a non-matching neighbor is untouched.

    The pattern mirrors sweep_stale_workspaces: per-entry errors are swallowed
    by the ``shutil.rmtree(..., ignore_errors=True)`` + outer try/except, so a
    single unremovable root can never abort the startup sweep.
    """
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)

        live_root = parent / "pmx-sbx-live"
        live_root.mkdir()
        svc = ProcessSandboxService(root=str(live_root))

        # Busy dir: parent dir is read-only (0o555) so the contained file cannot
        # be unlinked, and rmtree(ignore_errors=True) will silently fail. Age it
        # past the threshold so it WOULD be a candidate if it were removable.
        busy = _make_dir(parent, "pmx-sbx-busy", age_s=2 * 86400)
        (busy / "inner.txt").write_text("x")
        os.chmod(busy, 0o555)  # no write bit → cannot delete inner.txt

        # An unrelated dir that must also be untouched.
        unrelated = _make_dir(parent, "other-thing", age_s=5 * 86400)

        try:
            # The sweep MUST complete without raising.
            removed = await svc.sweep_stale_roots(max_age_s=86400)

            # The busy dir survives (rmtree failed and we ignored it).
            assert busy.exists(), "busy pmx-sbx-* dir must survive a failed sweep"
            # The unrelated dir is untouched (prefix guard).
            assert unrelated.exists(), "non-prefix dir must not be touched"
            # The live root survives (live-root guard).
            assert live_root.exists(), "live root must survive"
            # The returned count reflects only the REMOVED roots (0 here — busy
            # dir failed silently and the others were skipped or are live).
            assert removed == 0, f"expected 0 removed, got {removed}"
        finally:
            # Restore perms so TemporaryDirectory cleanup can finish.
            os.chmod(busy, 0o755)
