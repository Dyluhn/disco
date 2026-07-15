"""`_release_process_memory` — the retention half of the OOM fix.

Proves the helper actually hands freed heap pages back to the OS (not a mock):
allocate a large buffer, free it, and assert RSS measurably drops after the
trim. Skipped where it cannot be proven (non-Linux, or a libc without
malloc_trim) — there the helper degrades to a best-effort gc pass.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

import pytest
from disco.agent_server.runtime import _release_process_memory


def _vmrss_kb() -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    raise RuntimeError("VmRSS not found")


def _has_malloc_trim() -> bool:
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        return hasattr(libc, "malloc_trim")
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not _has_malloc_trim(),
    reason="RSS-drop proof requires Linux + glibc malloc_trim",
)


def test_release_returns_freed_pages_to_os() -> None:
    """Allocate ~400 MB, free it, then trim → RSS drops by a large fraction of
    the allocation. Without the trim, glibc would retain those arena pages and
    RSS would stay at the high-water mark."""
    baseline = _vmrss_kb()
    # Touch every page so the allocation is RESIDENT, not just reserved.
    blob = bytearray(400 * 1024 * 1024)
    for i in range(0, len(blob), 4096):
        blob[i] = 1
    high = _vmrss_kb()
    assert high - baseline > 300 * 1024, "allocation did not become resident"

    del blob
    _release_process_memory()  # gc.collect() + malloc_trim(0)

    after = _vmrss_kb()
    # At least 250 MB of the 400 MB returned to the OS.
    assert high - after > 250 * 1024, (
        f"RSS not released: high={high}kB after={after}kB (expected a drop > 256MB)"
    )


def test_release_is_safe_when_disabled(monkeypatch) -> None:
    """The disable knob short-circuits the trim but still runs gc — never raises."""
    monkeypatch.setenv("DISCO_DR_MALLOC_TRIM", "0")
    _release_process_memory()  # must not raise
