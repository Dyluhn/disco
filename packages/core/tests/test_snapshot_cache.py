"""The per-turn workspace snapshot hashes once and reads only what changed.

Contract:
  (a) turn 1 reads every working-set file; turn 2 with nothing changed does one hash exec
      and zero reads, and the rendered block is byte-identical;
  (b) a changed file is read again (exactly that one); a file the hash exec cannot see is
      read the old way;
  (c) when the hash exec fails the snapshot falls back to reading everything (never a
      wrong "unchanged");
  (d) the cache keeps only the working set; `stale_paths` uses the same hash map and
      reads nothing.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from disco.core.loop.file_state import FileStateTracker
from disco.core.loop.view_snapshot import (
    SnapshotCache,
    _parse_sha256sum,
    working_set_hashes,
    workspace_snapshot_message,
)
from event_fakes import action, observation, with_seqs


class _Sandbox:
    """Files in a dict; counts hash execs and per-file reads; `fail_hash` breaks the exec."""

    def __init__(self, files: dict[str, bytes], *, fail_hash: bool = False) -> None:
        self.files = dict(files)
        self.reads: list[str] = []
        self.execs = 0
        self.fail_hash = fail_hash

    async def exec_shell(self, cmd: str, *, timeout_s: int):
        self.execs += 1
        if self.fail_hash:
            raise RuntimeError("no shell")
        assert cmd.startswith("sha256sum -- ")
        lines = []
        for path in self.files:
            if f"{path} " in cmd + " " or cmd.endswith(path) or path in cmd:
                lines.append(f"{hashlib.sha256(self.files[path]).hexdigest()}  {path}")
        return SimpleNamespace(exit_code=0, stdout="\n".join(lines) + "\n", stderr="", timed_out=False)

    async def read_file(self, path: str) -> bytes:
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def _events(*paths: str):
    evs = []
    for p in paths:
        a = action(tool="file_write", args={"path": p, "content": "x"})
        evs += [a, observation(action_id=a.id, tool="file_write")]
    return with_seqs(evs)


async def _turn(sbx, events, cache, tracker):
    hashes = await working_set_hashes(sbx, ["a.py", "b.py"])
    stale = await tracker.stale_paths(sbx, ["a.py", "b.py"], hashes=hashes)
    msg = await workspace_snapshot_message(
        sbx, events, tracker=tracker, hashes=hashes, cache=cache, pin_full=True
    )
    return msg, stale, hashes


@pytest.mark.asyncio
async def test_unchanged_turn_hashes_once_and_reads_nothing() -> None:
    sbx = _Sandbox({"a.py": b"print(1)\n", "b.py": b"print(2)\n"})
    events = _events("a.py", "b.py")
    cache, tracker = SnapshotCache(), FileStateTracker()

    first, _, _ = await _turn(sbx, events, cache, tracker)
    assert sbx.execs == 1 and sorted(sbx.reads) == ["a.py", "b.py"]

    second, stale, hashes = await _turn(sbx, events, cache, tracker)
    assert sbx.execs == 2 and sorted(sbx.reads) == ["a.py", "b.py"], "no re-read on an unchanged turn"
    assert second is not None and first is not None and second.content == first.content
    assert stale == [] and hashes is not None and set(hashes) == {"a.py", "b.py"}


@pytest.mark.asyncio
async def test_only_the_changed_file_is_read_again_and_reported_stale() -> None:
    sbx = _Sandbox({"a.py": b"a1", "b.py": b"b1"})
    events = _events("a.py", "b.py")
    cache, tracker = SnapshotCache(), FileStateTracker()
    await _turn(sbx, events, cache, tracker)
    sbx.reads.clear()

    sbx.files["b.py"] = b"b2 changed on disk"
    msg, stale, _ = await _turn(sbx, events, cache, tracker)
    assert sbx.reads == ["b.py"]
    assert stale == ["b.py"]
    assert msg is not None and "b2 changed on disk" in msg.content


@pytest.mark.asyncio
async def test_hash_failure_falls_back_to_reading_everything() -> None:
    sbx = _Sandbox({"a.py": b"a", "b.py": b"b"}, fail_hash=True)
    events = _events("a.py", "b.py")
    cache, tracker = SnapshotCache(), FileStateTracker()
    await _turn(sbx, events, cache, tracker)
    sbx.reads.clear()
    _, stale, hashes = await _turn(sbx, events, cache, tracker)
    assert hashes is None
    assert sorted(sbx.reads) == ["a.py", "b.py"]  # the old per-file path, twice over
    assert stale == []


@pytest.mark.asyncio
async def test_file_missing_from_the_hash_map_is_read_the_old_way() -> None:
    sbx = _Sandbox({"a.py": b"a"})  # b.py never existed
    events = _events("a.py", "b.py")
    cache, tracker = SnapshotCache(), FileStateTracker()
    msg, _, hashes = await _turn(sbx, events, cache, tracker)
    assert hashes == {"a.py": hashlib.sha256(b"a").hexdigest()}
    assert "b.py" in sbx.reads  # attempted, reported as gone
    assert msg is not None and "[file gone: b.py]" in msg.content


@pytest.mark.asyncio
async def test_cache_keeps_only_the_working_set() -> None:
    sbx = _Sandbox({"a.py": b"a", "b.py": b"b", "old.py": b"o"})
    cache, tracker = SnapshotCache(), FileStateTracker()
    await workspace_snapshot_message(sbx, _events("old.py"), tracker=tracker, hashes=None, cache=cache)
    assert len(cache) == 1
    await workspace_snapshot_message(sbx, _events("a.py", "b.py"), tracker=tracker, hashes=None, cache=cache)
    assert len(cache) == 2 and cache.get("old.py", hashlib.sha256(b"o").hexdigest()) is None


def test_parse_sha256sum_handles_missing_and_escaped_lines() -> None:
    out = (
        "0" * 64 + "  src/a.py\n"
        "\\" + "1" * 64 + "  weird\\nname.py\n"
        "sha256sum: gone.py: No such file or directory\n"
    )
    assert _parse_sha256sum(out) == {"src/a.py": "0" * 64, "weird\\nname.py": "1" * 64}


@pytest.mark.asyncio
async def test_no_shell_means_no_hashes() -> None:
    assert await working_set_hashes(SimpleNamespace(), ["a.py"]) is None
    assert await working_set_hashes(_Sandbox({}), []) is None
