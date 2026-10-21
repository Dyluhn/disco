"""Capture identities survive daemon restarts without rewriting old evidence."""

import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest
from disco.tools.builtin._browser_daemon import BrowserState
from disco.tools.builtin._browser_daemon_parts import dispatch
from disco.tools.builtin._browser_daemon_parts.dispatch import _take_screenshot


def capture(workspace, state, content, *, full_page=False):
    directory = workspace / ".pmx/screenshots"

    def write(*, path, full_page):
        # The path must already be reserved exclusively before Playwright writes.
        assert Path(path).is_file()
        Path(path).write_bytes(content)

    page = Mock()
    page.screenshot.side_effect = write
    relative, absolute = _take_screenshot(
        state, page, "navigate", {"full_page": full_page},
        workspace_root=str(workspace), screenshot_dir=str(directory),
    )
    page.screenshot.assert_called_once_with(path=absolute, full_page=full_page)
    assert workspace / relative == Path(absolute)
    assert Path(relative).parts[:2] == (".pmx", "screenshots")
    assert relative.endswith(".png")
    return relative


def test_restarted_browser_daemons_capture_to_distinct_paths_without_overwriting_history(tmp_path):
    directory = tmp_path / ".pmx/screenshots"
    directory.mkdir(parents=True)
    legacy = directory / "0001-navigate.png"
    legacy.write_bytes(b"legacy screenshot evidence")
    first_daemon = BrowserState()
    first = capture(tmp_path, first_daemon, b"first frame", full_page=True)
    second_daemon = BrowserState()
    second = capture(tmp_path, second_daemon, b"second frame")
    assert first_daemon.screenshot_seq == second_daemon.screenshot_seq == 1
    assert first != second
    assert first != ".pmx/screenshots/0001-navigate.png"
    assert (tmp_path / first).read_bytes() == b"first frame"
    assert (tmp_path / second).read_bytes() == b"second frame"
    assert legacy.read_bytes() == b"legacy screenshot evidence"


def test_fresh_workspace_capture_does_not_reuse_an_earlier_daemon_relative_path(tmp_path):
    first_workspace = tmp_path / "first-generation"
    next_workspace = tmp_path / "next-generation"
    first = capture(first_workspace, BrowserState(), b"first workspace frame")
    second = capture(next_workspace, BrowserState(), b"next workspace frame")
    # Conversation event history can survive replacement of its live workspace.
    assert first != second
    assert (first_workspace / first).read_bytes() == b"first workspace frame"
    assert (next_workspace / second).read_bytes() == b"next workspace frame"


@pytest.mark.parametrize("writer_removes_file", [False, True])
def test_failed_capture_removes_only_its_reserved_file_and_preserves_error(
    tmp_path, writer_removes_file,
):
    directory = tmp_path / ".pmx/screenshots"
    directory.mkdir(parents=True)
    legacy = directory / "0001-navigate.png"
    legacy.write_bytes(b"preserved prior frame")

    def fail(*, path, full_page):
        Path(path).write_bytes(b"incomplete new capture")
        if writer_removes_file:
            Path(path).unlink()
        raise RuntimeError("capture failed")

    page = Mock()
    page.screenshot.side_effect = fail
    with pytest.raises(RuntimeError, match="capture failed"):
        _take_screenshot(
            BrowserState(), page, "navigate", {},
            workspace_root=str(tmp_path), screenshot_dir=str(directory),
        )
    assert list(directory.iterdir()) == [legacy]
    assert legacy.read_bytes() == b"preserved prior frame"


@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_capture_preserves_workspace_file_permissions_from_umask(tmp_path, mask):
    previous = os.umask(mask)
    try:
        relative = capture(tmp_path, BrowserState(), b"capture")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / relative).stat().st_mode) == 0o666 & ~mask


def test_capture_identity_collision_preserves_the_existing_frame(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.uuid, "uuid4", lambda: Mock(hex="collision"))
    first = capture(tmp_path, BrowserState(), b"existing evidence")
    with pytest.raises(FileExistsError):
        capture(tmp_path, BrowserState(), b"must not overwrite")
    assert (tmp_path / first).read_bytes() == b"existing evidence"
