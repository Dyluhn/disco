from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from harness.reliability.fresh_device import (
    ProductError,
    _assert_verify_output,
    _project_ids,
    _safe_device_label,
    _validate_zip,
)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_device_label_is_only_safe_human_metadata() -> None:
    assert _safe_device_label(" Dylan's Workstation #4 ") == "dylan-s-workstation-4"
    assert _safe_device_label("!!!") == "device"
    assert len(_safe_device_label("x" * 100)) == 32


def test_verify_requires_real_grounding_pass() -> None:
    base = "PASS config\nPASS completion\nPASS tool-calling\n"
    _assert_verify_output(base, grounding=False)
    with pytest.raises(ProductError, match="SKIP never counts"):
        _assert_verify_output(base + "SKIP grounding\n", grounding=True)
    _assert_verify_output(base + "PASS grounding\n", grounding=True)


def test_project_ids_reject_bad_shape_and_missing_workspaces() -> None:
    assert _project_ids(
        {
            "projects": [
                {"id": "conv_ok", "files_missing": False},
                {"id": "conv_missing", "files_missing": True},
                {"title": "no identity"},
            ]
        }
    ) == ["conv_ok"]
    with pytest.raises(ProductError, match="wrong shape"):
        _project_ids([])


def test_project_zip_must_be_nonempty_and_uncorrupt(tmp_path: Path) -> None:
    data = _zip_bytes({"index.html": b"<h1>fresh</h1>", "src/app.js": b"ok"})
    target = tmp_path / "project.zip"
    assert _validate_zip(data, target) == ["index.html", "src/app.js"]
    assert target.read_bytes() == data
    with pytest.raises(ProductError, match="not a ZIP"):
        _validate_zip(b"not-a-zip", tmp_path / "bad.zip")
    with pytest.raises(ProductError, match="contains no files"):
        _validate_zip(_zip_bytes({}), tmp_path / "empty.zip")
