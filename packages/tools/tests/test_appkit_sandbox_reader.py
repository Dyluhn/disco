"""Remote verifier reads retain exact bytes, failure handling, and bounded work."""

import asyncio
from collections import Counter

import pytest
from _appkit_verify_doubles import FakeSandbox, _build_tree, _ctx
from disco.core.appkit import load_app_spec_from_bytes, load_design_spec_from_bytes
from disco.tools.builtin.verify_appkit_parts.sandbox_reader import SandboxReader


class SlowReadSandbox(FakeSandbox):
    def __init__(self, files):
        super().__init__(files)
        self.active = 0
        self.peak = 0
        self.calls = Counter()
        self.four_reads = asyncio.Event()
        self.release = asyncio.Event()

    async def file_exists(self, path):
        raise AssertionError("tree reads must not perform a redundant existence round trip")

    async def read_file(self, path):
        self.calls[path] += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == 4:
            self.four_reads.set()
        try:
            await self.release.wait()
            return await super().read_file(path)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_tree_reads_overlap_boundedly_and_use_current_disk_bytes():
    files, _ = _build_tree()
    app = load_app_spec_from_bytes(files[".disco/appspec.json"])
    design = load_design_spec_from_bytes(files[".disco/designspec.json"])
    files["worker/index.ts"] = b"broken, model-edited worker bytes"
    files["src/components/Extra.tsx"] = b"export const Extra = 'not generated';"
    sandbox = SlowReadSandbox(files)
    reader = SandboxReader()
    task = asyncio.create_task(reader.assemble_tree(_ctx(sandbox), app, design))
    await asyncio.wait_for(sandbox.four_reads.wait(), 2)
    assert not task.done() and sandbox.active == 4
    sandbox.release.set()
    tree = await task
    assert sandbox.peak == 4 and sandbox.active == 0
    assert set(sandbox.calls.values()) == {1}
    assert tree["worker/index.ts"] == files["worker/index.ts"].decode()
    assert tree["src/components/Extra.tsx"] == files["src/components/Extra.tsx"].decode()
    assert list(tree) == sorted(tree)
    # Same reader, next verification: no cross-run cache hiding a mutation.
    sandbox._files["worker/index.ts"] = b"a second set of disk bytes"
    del sandbox._files["src/components/Extra.tsx"]
    fresh = await reader.assemble_tree(_ctx(sandbox), app, design)
    assert fresh["worker/index.ts"] == "a second set of disk bytes"
    assert "src/components/Extra.tsx" not in fresh


@pytest.mark.asyncio
async def test_cancellation_drains_all_started_reads():
    files, _ = _build_tree()
    sandbox = SlowReadSandbox(files)
    task = asyncio.create_task(SandboxReader().assemble_tree(_ctx(sandbox), None, None))
    await asyncio.wait_for(sandbox.four_reads.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sandbox.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [FileNotFoundError, IsADirectoryError, PermissionError])
async def test_failed_jailed_read_is_absent_without_probe_or_cached_content(error):
    class DeniedSandbox(FakeSandbox):
        async def read_file(self, path):
            raise error(path)

        async def file_exists(self, path):
            raise AssertionError("redundant probe")

    assert await SandboxReader().read_bytes(_ctx(DeniedSandbox({})), "denied") is None


@pytest.mark.asyncio
async def test_real_file_jail_and_partial_failures_preserve_other_tree_entries(tmp_path):
    from disco.tools.sandbox.base import SandboxSpec
    from disco.tools.sandbox.process import ProcessSandboxInstance

    files, _ = _build_tree()
    app = load_app_spec_from_bytes(files[".disco/appspec.json"])
    design = load_design_spec_from_bytes(files[".disco/designspec.json"])
    root = tmp_path / "workspace"
    root.mkdir()
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside-secret-must-never-enter-verifier-tree")
    (root / "src/components/Outside.tsx").symlink_to(outside)
    (root / "src/components/ValidAlias.tsx").symlink_to(root / "worker/index.ts")
    (root / "schema.sql").unlink()
    sandbox = ProcessSandboxInstance("reader-test", "owner", "conv_reader", SandboxSpec(), root)
    reader = SandboxReader()
    tree = await reader.assemble_tree(_ctx(sandbox), app, design)
    assert "src/components/Outside.tsx" not in tree
    assert "schema.sql" not in tree
    assert tree["worker/index.ts"] == files["worker/index.ts"].decode()
    assert "src/components/ValidAlias.tsx" not in tree
    assert not any("outside-secret" in text for text in tree.values())
    assert await reader.read_bytes(_ctx(sandbox), "src/components") is None
    assert await reader.read_bytes(_ctx(sandbox), "missing") is None
