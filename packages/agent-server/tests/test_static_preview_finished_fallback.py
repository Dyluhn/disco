"""Finished preview fallback variants of the complete sealed-runtime roundtrip."""

from pathlib import Path

import pytest
from test_static_preview_capability import (
    test_sealed_dynamic_preview_restarts_from_immutable_bytes_and_rotates_authority as _roundtrip,
)


@pytest.mark.parametrize("published_transport", ["unpublished", "refused"])
async def test_sealed_dynamic_preview_fallback_preserves_bytes_and_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published_transport: str,
) -> None:
    """Exercise every original roundtrip assertion over each fallback transport."""
    await _roundtrip(tmp_path, monkeypatch, published_transport)
