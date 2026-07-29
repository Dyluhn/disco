"""Moved post terminal version publication seed collection implementations."""

from __future__ import annotations

from ._shared import Any, DiscoApiClient, cast
from .helpers_02 import (
    _FailingRestoreTransport,
    _LateVersionsTransport,
)


async def _impl_test_restore_version_waits_for_post_terminal_publication(tmp_path):
    """The restore drive applies the snapshot-wait doctrine to the versions
    route: a read that lands in the finalization gap polls to the deadline
    instead of adjudicating a just-finished build as version-less."""
    transport = _LateVersionsTransport(empty_reads=2)
    client = DiscoApiClient(
        cast(Any, transport),
        db_path=str(tmp_path / "disco.db"),
        snapshot_wait_s=10.0,
    )
    out = await client.restore_workspace_version("conv_x", selector="previous")
    assert out["ok"] is True
    assert out["selected_seq"] == 1
    assert out["new_version"] == 3
    assert transport.reads == 3
    assert transport.restores == ["/conversations/conv_x/versions/1/restore"]


async def _impl_test_restore_version_still_fails_closed_when_never_published(tmp_path):
    """Fail-closed control: a build whose versions never publish within the
    budget still reports versions_unavailable — the wait fixes the race, it
    does not grant availability."""
    transport = _LateVersionsTransport(empty_reads=10**9)
    client = DiscoApiClient(
        cast(Any, transport),
        db_path=str(tmp_path / "disco.db"),
        snapshot_wait_s=1.2,
    )
    out = await client.restore_workspace_version("conv_x", selector="previous")
    assert out == {"ok": False, "http_status": 200, "reason": "versions_unavailable"}
    assert transport.reads >= 2
    assert transport.restores == []


async def _impl_test_restore_version_failure_retains_product_error_body(tmp_path):
    """EVIDENCE PIN (F-28) — a failed restore keeps the product's response
    body verbatim under `error` so a future dossier names the cause. Never
    parsed, never adjudicated on — retention only."""
    client = DiscoApiClient(
        cast(Any, _FailingRestoreTransport()),
        db_path=str(tmp_path / "disco.db"),
        snapshot_wait_s=1.0,
    )
    out = await client.restore_workspace_version("conv_x", selector="previous")
    assert out["ok"] is False
    assert out["http_status"] == 503
    assert out["error"] == {"reason": "storage_error", "message": "sandbox unavailable"}
