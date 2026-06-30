"""CD-TOOLS-7 — run_project_script: a BUFFERED, TRANSACTIONAL batch of file transforms. All ops
commit together or NONE do; every CD-TOOLS guard (governed/elision/shrink/no-match/grounding/
syntax) applies, and a failure rolls back the whole batch (nothing written)."""

from __future__ import annotations

import hashlib

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import RunProjectScriptTool
from disco.tools.builtin.files import reset_read_tracker
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean():
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


def _run(ops):
    return RunProjectScriptTool().run(RunProjectScriptTool.definition.args_model(operations=ops), None)  # type: ignore


async def _go(ctx, ops):
    return await RunProjectScriptTool().run(RunProjectScriptTool.definition.args_model(operations=ops), ctx)


async def _read(inst, p):
    return (await inst.read_file(p)).decode("utf-8")


async def test_all_writes_commit_together():
    ctx, inst = await _ctx()
    await inst.write_file("b.txt", b"hello world here\n")
    res = await _go(ctx, [
        {"op": "save", "path": "a.txt", "content": "brand new\n"},
        {"op": "read", "path": "b.txt"},
        {"op": "replace_text", "path": "b.txt", "old": "world", "new": "there"},
    ])
    assert res.success, res.content
    assert await _read(inst, "a.txt") == "brand new\n"
    assert await _read(inst, "b.txt") == "hello there here\n"
    assert set(res.structured["applied"]) == {"a.txt", "b.txt"}
    await inst.destroy()


async def test_throw_rolls_back_all_writes():
    # a later failing op must roll back EARLIER successful ops — nothing is written.
    ctx, inst = await _ctx()
    await inst.write_file("b.txt", b"content\n")
    res = await _go(ctx, [
        {"op": "save", "path": "new.txt", "content": "should not persist\n"},
        {"op": "replace_text", "path": "b.txt", "old": "ABSENT", "new": "x"},  # no match → abort
    ])
    assert not res.success and res.error == "SCRIPT_NO_MATCH"
    assert not await inst.file_exists("new.txt")  # earlier save rolled back
    await inst.destroy()


async def test_shrink_guard_rejects_truncation_no_write():
    ctx, inst = await _ctx()
    body = b"x" * 1000 + b"\n"
    await inst.write_file("big.txt", body)
    res = await _go(ctx, [
        {"op": "read", "path": "big.txt"},  # ground it
        {"op": "save", "path": "big.txt", "content": "tiny\n"},
    ])
    assert not res.success and res.error == "SAFE_WRITE_SHRINK_REJECTED"
    assert (await inst.read_file("big.txt")) == body
    await inst.destroy()


async def test_replace_text_is_literal():
    ctx, inst = await _ctx()
    await inst.write_file("t.txt", b"PH and padding here\n")
    literal = "$1 ${x} \\1 costs $5"
    res = await _go(ctx, [{"op": "replace_text", "path": "t.txt", "old": "PH", "new": literal}])
    assert res.success, res.content
    assert literal in await _read(inst, "t.txt")
    await inst.destroy()


async def test_governed_disco_path_rejected_no_write():
    ctx, inst = await _ctx()
    res = await _go(ctx, [{"op": "save", "path": ".disco/appspec.json", "content": "{}"}])
    assert not res.success and res.error in ("GOVERNED_ARTIFACT_REJECTED", "SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED")
    assert not await inst.file_exists(".disco/appspec.json")
    await inst.destroy()


async def test_elision_marker_rejected_no_write():
    ctx, inst = await _ctx()
    res = await _go(ctx, [{"op": "save", "path": "e.txt", "content": "a <4500 chars elided> b"}])
    assert not res.success and res.error == "ELISION_MARKER_REJECTED"
    assert not await inst.file_exists("e.txt")
    await inst.destroy()


async def test_syntax_introducing_batch_rejected_no_write():
    ctx, inst = await _ctx()
    await inst.write_file("m.py", b"x = 1\ny = 2\n")
    res = await _go(ctx, [
        {"op": "replace_text", "path": "m.py", "old": "y = 2", "new": "y = ("},
    ])
    assert not res.success and res.error == "SCRIPT_BATCH_FAILED"
    assert await _read(inst, "m.py") == "x = 1\ny = 2\n"
    await inst.destroy()


async def test_over_cap_rejected():
    ctx, inst = await _ctx()
    ops = [{"op": "ls", "path": "."} for _ in range(201)]
    res = await _go(ctx, ops)
    assert not res.success and res.error == "SCRIPT_TOO_LARGE"
    await inst.destroy()


async def test_read_and_ls_do_not_mutate():
    ctx, inst = await _ctx()
    await inst.write_file("r.txt", b"data\n")
    res = await _go(ctx, [{"op": "read", "path": "r.txt"}, {"op": "ls", "path": "."}])
    assert res.success and res.structured["applied"] == []
    assert res.structured["reads"]["r.txt"] == "data\n"
    assert "r.txt" in res.structured["reads"]["."]
    await inst.destroy()


async def test_save_over_unread_existing_requires_grounding():
    ctx, inst = await _ctx()
    await inst.write_file("f.txt", b"existing content that is fine\n")
    res = await _go(ctx, [{"op": "save", "path": "f.txt", "content": "new content that is also fine\n"}])
    assert not res.success and res.error == "FRESH_READ_REQUIRED"
    assert await _read(inst, "f.txt") == "existing content that is fine\n"
    await inst.destroy()


async def test_save_after_read_is_grounded():
    ctx, inst = await _ctx()
    await inst.write_file("f.txt", b"existing content that is fine\n")
    res = await _go(ctx, [
        {"op": "read", "path": "f.txt"},
        {"op": "save", "path": "f.txt", "content": "new content that is also fine\n"},
    ])
    assert res.success, res.content
    assert await _read(inst, "f.txt") == "new content that is also fine\n"
    await inst.destroy()


async def test_save_with_matching_sha_is_grounded():
    ctx, inst = await _ctx()
    body = b"existing content that is fine\n"
    await inst.write_file("f.txt", body)
    res = await _go(ctx, [
        {"op": "save", "path": "f.txt", "content": "replaced ok here\n",
         "expected_sha256": hashlib.sha256(body).hexdigest()},
    ])
    assert res.success, res.content
    await inst.destroy()


async def test_symlink_alias_cannot_bypass_governed_or_redirect_commit():
    # Codex CD-TOOLS-7 round-1: a read through a symlink alias must NOT let a later save validate one
    # path while committing another. Keying by the REAL path catches the governed target.
    ctx, inst = await _ctx()
    await inst.write_file(".disco/secret.json", b'{"real":1}')
    import os
    ws = inst._workspace  # type: ignore[attr-defined]
    os.symlink(".disco/secret.json", ws / "link.json")
    res = await _go(ctx, [
        {"op": "read", "path": "link.json"},  # grounds the REAL .disco path
        {"op": "save", "path": "link.json", "content": "{}"},  # → governed on the real path
    ])
    assert not res.success and res.error == "GOVERNED_ARTIFACT_REJECTED"
    assert (await inst.read_file(".disco/secret.json")) == b'{"real":1}'  # untouched
    await inst.destroy()


async def test_lexical_alias_commits_to_the_real_file():
    # two different path strings for the SAME real file → one key; a read of one grounds a save of
    # the other, and the commit writes the real file (no divergence).
    ctx, inst = await _ctx()
    await inst.write_file("dir/f.txt", b"original content here ok\n")
    res = await _go(ctx, [
        {"op": "read", "path": "dir/f.txt"},
        {"op": "save", "path": "dir/../dir/f.txt", "content": "rewritten content here ok\n"},
    ])
    assert res.success, res.content
    assert await _read(inst, "dir/f.txt") == "rewritten content here ok\n"
    await inst.destroy()


async def test_binary_clobber_guard_uses_real_extension_through_alias():
    # alias.txt -> deck.pptx: the binary guard must use the REAL extension (pptx), not the typed
    # alias (txt), so the binary deck isn't clobbered (codex CD-TOOLS-7 round-2).
    ctx, inst = await _ctx()
    await inst.write_file("deck.pptx", b"PK\x03\x04 binary pptx bytes padding padding\n")
    import os
    ws = inst._workspace  # type: ignore[attr-defined]
    os.symlink("deck.pptx", ws / "alias.txt")
    res = await _go(ctx, [
        {"op": "read", "path": "alias.txt"},
        {"op": "save", "path": "alias.txt", "content": "text clobber"},
    ])
    assert not res.success and res.error == "binary_deliverable_clobber"
    await inst.destroy()


async def test_save_new_file_needs_no_grounding():
    ctx, inst = await _ctx()
    res = await _go(ctx, [{"op": "save", "path": "fresh.txt", "content": "hi\n"}])
    assert res.success, res.content
    assert await _read(inst, "fresh.txt") == "hi\n"
    await inst.destroy()
