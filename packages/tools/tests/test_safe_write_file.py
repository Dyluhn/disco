"""CD-TOOLS-3 — safe_write_file. A guarded whole-file writer: rejects elision markers, a >50%
shrink (the weak-model clobber), writes to the host-governed .disco/ namespace (even via a
symlink), a binary clobber, and a syntax-introducing write — then commits atomically. Negatives
assert BOTH the code AND that the file is unchanged."""

from __future__ import annotations

import hashlib

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import FileReadTool, SafeWriteFileTool
from disco.tools.builtin.files import reset_read_tracker
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_tracker():
    reset_read_tracker()
    yield
    reset_read_tracker()


async def _ctx():
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    ctx = ToolContext(
        sandbox=inst, workspace_path=".", timeout_s=30,
        capabilities={Capability.FILESYSTEM}, owner_id="local", conversation_id="c",
    )
    return ctx, inst


def _a(**kw):
    return SafeWriteFileTool.definition.args_model(**kw)


async def _read(inst, path):
    return (await inst.read_file(path)).decode("utf-8")


async def _ground(ctx, path):
    await FileReadTool().run(FileReadTool.definition.args_model(path=path), ctx)


async def test_new_file_write_ok():
    ctx, inst = await _ctx()
    res = await SafeWriteFileTool().run(_a(path="new.txt", content="hello\n"), ctx)
    assert res.success, res.content
    assert await _read(inst, "new.txt") == "hello\n"
    assert res.structured["sha256"] == hashlib.sha256(b"hello\n").hexdigest()
    await inst.destroy()


async def test_overwrite_after_read_ok():
    ctx, inst = await _ctx()
    await inst.write_file("f.txt", b"original content that is long enough\n")
    await _ground(ctx, "f.txt")
    res = await SafeWriteFileTool().run(_a(path="f.txt", content="replacement content equally long enough\n"), ctx)
    assert res.success, res.content
    await inst.destroy()


async def test_shrink_over_50pct_rejected_no_change():
    ctx, inst = await _ctx()
    body = b"x" * 1000 + b"\n"
    await inst.write_file("big.txt", body)
    await _ground(ctx, "big.txt")
    res = await SafeWriteFileTool().run(_a(path="big.txt", content="tiny\n"), ctx)  # 5 chars « 50% of 1001
    assert not res.success and res.error == "SAFE_WRITE_SHRINK_REJECTED"
    assert (await inst.read_file("big.txt")) == body  # untouched
    await inst.destroy()


async def test_shrink_with_allow_shrink_applies():
    ctx, inst = await _ctx()
    body = b"x" * 1000 + b"\n"
    await inst.write_file("big.txt", body)
    await _ground(ctx, "big.txt")
    res = await SafeWriteFileTool().run(_a(path="big.txt", content="tiny\n", allow_shrink=True), ctx)
    assert res.success, res.content
    assert await _read(inst, "big.txt") == "tiny\n"
    await inst.destroy()


async def test_matching_sha_bypasses_shrink_and_read_gate():
    ctx, inst = await _ctx()
    body = b"x" * 1000 + b"\n"
    await inst.write_file("big.txt", body)
    # no file_read at all; a matching sha proves the model has the current version.
    res = await SafeWriteFileTool().run(
        _a(path="big.txt", content="tiny\n", expected_sha256=hashlib.sha256(body).hexdigest()), ctx
    )
    assert res.success, res.content
    await inst.destroy()


async def test_governed_disco_path_rejected_no_write():
    ctx, inst = await _ctx()
    res = await SafeWriteFileTool().run(_a(path=".disco/appspec.json", content='{"x":1}'), ctx)
    assert not res.success and res.error == "SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED"
    assert not await inst.file_exists(".disco/appspec.json")
    await inst.destroy()


async def test_governed_via_symlink_rejected():
    # a symlink that resolves into .disco/ must be caught by the REAL-path governed check.
    ctx, inst = await _ctx()
    await inst.write_file(".disco/appspec.json", b'{"real":1}')
    # create a symlink link.json -> .disco/appspec.json inside the jail
    import os
    ws = inst._workspace  # type: ignore[attr-defined]
    os.symlink(".disco/appspec.json", ws / "link.json")
    res = await SafeWriteFileTool().run(_a(path="link.json", content='{"x":2}'), ctx)
    assert not res.success and res.error == "SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED"
    assert (await inst.read_file(".disco/appspec.json")) == b'{"real":1}'  # untouched
    await inst.destroy()


async def test_elision_marker_rejected_no_write():
    ctx, inst = await _ctx()
    res = await SafeWriteFileTool().run(_a(path="e.txt", content="a <4500 chars elided> b"), ctx)
    assert not res.success and res.error == "ELISION_MARKER_REJECTED"
    assert not await inst.file_exists("e.txt")
    await inst.destroy()


async def test_syntax_introducing_write_rejected_no_change():
    ctx, inst = await _ctx()
    await inst.write_file("m.py", b"x = 1\ny = 2\n")
    await _ground(ctx, "m.py")
    res = await SafeWriteFileTool().run(_a(path="m.py", content="x = 1\ny = (\n"), ctx)
    assert not res.success and res.error == "syntax_gate_rejected"
    assert await _read(inst, "m.py") == "x = 1\ny = 2\n"
    await inst.destroy()


async def test_existing_binary_clobber_refused():
    ctx, inst = await _ctx()
    await inst.write_file("deck.pdf", b"%PDF-1.4 binary bytes here padding padding padding\n")
    await _ground(ctx, "deck.pdf")
    res = await SafeWriteFileTool().run(_a(path="deck.pdf", content="text clobber"), ctx)
    assert not res.success and res.error == "binary_deliverable_clobber"
    await inst.destroy()


async def test_overwrite_without_read_requires_fresh_read():
    ctx, inst = await _ctx()
    await inst.write_file("f.txt", b"some existing content here that is fine\n")
    res = await SafeWriteFileTool().run(_a(path="f.txt", content="new content that is fine here too\n"), ctx)
    assert not res.success and res.error == "FRESH_READ_REQUIRED"
    await inst.destroy()


async def test_atomic_write_leaves_no_leftover_tmp():
    ctx, inst = await _ctx()
    res = await SafeWriteFileTool().run(_a(path="sub/dir/a.txt", content="content\n"), ctx)
    assert res.success, res.content
    listing = await inst.list_dir("sub/dir")
    assert not any(name.startswith(".disco-tmp") for name in listing), listing
    await inst.destroy()


async def test_predictable_tmp_symlink_cannot_clobber_governed(tmp_path):
    # the OLD predictable tmp name (<target>.disco-tmp) pre-symlinked into .disco/ must NOT be
    # followed by the write (mkstemp uses a random O_EXCL name) — governed content stays intact.
    ctx, inst = await _ctx()
    await inst.write_file(".disco/secret.json", b'{"real":1}')
    await inst.write_file("t.txt", b"some existing grounded content here\n")
    await _ground(ctx, "t.txt")
    import os
    ws = inst._workspace  # type: ignore[attr-defined]
    os.symlink(".disco/secret.json", ws / "t.txt.disco-tmp")  # the old predictable name
    res = await SafeWriteFileTool().run(_a(path="t.txt", content="brand new grounded content here now\n"), ctx)
    assert res.success, res.content
    assert await _read(inst, "t.txt") == "brand new grounded content here now\n"
    assert (await inst.read_file(".disco/secret.json")) == b'{"real":1}'  # NOT clobbered
    await inst.destroy()
