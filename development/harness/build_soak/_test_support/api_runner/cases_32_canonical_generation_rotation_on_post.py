"""Moved canonical generation rotation on post collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    pytest,
)
from .helpers_01 import (
    _client,
    _seed_db,
)
from .helpers_02 import _RotatingPreviewTransport


@pytest.mark.asyncio
async def _impl_test_collect_preview_rebootstraps_once_across_generation_rotation(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [])
    transport = _RotatingPreviewTransport(
        db,
        states=["FINISHED"],
        sequence=[(409, "preview generation changed"), (200, "<h1>Sealed OK</h1>")],
    )
    client = _client(transport, tmp_path)

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 200
    assert preview["content"] == "<h1>Sealed OK</h1>"
    assert preview["available"] is True
    assert transport.preview_fetches == 2


@pytest.mark.asyncio
async def _impl_test_collect_preview_retains_persistent_rotation_failure(tmp_path):
    """Bounded: a second rotation in a row is the truthful retained failure."""
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [])
    transport = _RotatingPreviewTransport(
        db,
        states=["FINISHED"],
        sequence=[(409, "preview generation changed"), (409, "preview generation changed")],
    )
    client = _client(transport, tmp_path)

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 409
    assert preview["available"] is False
    assert transport.preview_fetches == 2


@pytest.mark.asyncio
async def _impl_test_collect_preview_never_retries_other_conflicts(tmp_path):
    """Any non-rotation 409 (or other failure) is retained on the first read."""
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [])
    transport = _RotatingPreviewTransport(
        db,
        states=["FINISHED"],
        sequence=[(409, "preview_authority_unavailable"), (200, "never fetched")],
    )
    client = _client(transport, tmp_path)

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 409
    assert preview["content"] == "preview_authority_unavailable"
    assert transport.preview_fetches == 1
