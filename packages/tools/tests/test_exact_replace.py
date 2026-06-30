"""CD-TOOLS-2 — the atomic exact_replace tool. Targeted string edits that apply all-or-nothing,
fail closed, never let a render elision marker into source, and write literally (no regex). Every
negative asserts BOTH the failure code AND that the file is byte-for-byte unchanged (atomicity)."""

from __future__ import annotations

import hashlib

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import ExactReplaceTool, FileReadTool
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
        sandbox=inst,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="c",
    )
    return ctx, inst


def _a(**kw):
    return ExactReplaceTool.definition.args_model(**kw)


async def _read(inst, path):
    return (await inst.read_file(path)).decode("utf-8")


async def test_single_replace_applies():
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"hello world\n")
    res = await ExactReplaceTool().run(_a(path="a.txt", edits=[{"old_string": "world", "new_string": "there"}]), ctx)
    assert res.success, res.content
    assert await _read(inst, "a.txt") == "hello there\n"
    await inst.destroy()


async def test_multi_true_replaces_all_occurrences():
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"x x x\n")
    res = await ExactReplaceTool().run(
        _a(path="a.txt", edits=[{"old_string": "x", "new_string": "y"}], multi=True), ctx
    )
    assert res.success, res.content
    assert await _read(inst, "a.txt") == "y y y\n"
    assert res.structured["applied"][0]["replaced"] == 3
    await inst.destroy()


async def test_no_match_no_change():
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"hello\n")
    res = await ExactReplaceTool().run(_a(path="a.txt", edits=[{"old_string": "NOPE", "new_string": "x"}]), ctx)
    assert not res.success and res.error == "EXACT_REPLACE_NO_MATCH"
    assert await _read(inst, "a.txt") == "hello\n"
    await inst.destroy()


async def test_duplicate_without_multi_no_change():
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"x x\n")
    res = await ExactReplaceTool().run(_a(path="a.txt", edits=[{"old_string": "x", "new_string": "y"}]), ctx)
    assert not res.success and res.error == "EXACT_REPLACE_DUPLICATE_MATCH"
    assert await _read(inst, "a.txt") == "x x\n"  # untouched
    await inst.destroy()


async def test_one_failed_edit_in_batch_is_atomic_no_change():
    # the FIRST edit matches; the SECOND does not — the WHOLE batch must not apply (atomicity).
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"alpha beta\n")
    res = await ExactReplaceTool().run(
        _a(path="a.txt", edits=[
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "MISSING", "new_string": "x"},
        ]),
        ctx,
    )
    assert not res.success and res.error == "EXACT_REPLACE_NO_MATCH"
    assert await _read(inst, "a.txt") == "alpha beta\n"  # the valid first edit was NOT applied
    await inst.destroy()


async def test_overlapping_edits_no_change():
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"abcdef\n")
    res = await ExactReplaceTool().run(
        _a(path="a.txt", edits=[
            {"old_string": "abcd", "new_string": "X"},
            {"old_string": "cdef", "new_string": "Y"},
        ]),
        ctx,
    )
    assert not res.success and res.error == "EXACT_REPLACE_BATCH_FAILED"
    assert await _read(inst, "a.txt") == "abcdef\n"
    await inst.destroy()


async def test_stale_expected_sha256_no_change():
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"hello\n")
    res = await ExactReplaceTool().run(
        _a(path="a.txt", edits=[{"old_string": "hello", "new_string": "hi"}], expected_sha256="deadbeef" * 8),
        ctx,
    )
    assert not res.success and res.error == "STALE_FILE_CONTEXT"
    assert await _read(inst, "a.txt") == "hello\n"
    await inst.destroy()


async def test_correct_expected_sha256_applies():
    ctx, inst = await _ctx()
    body = b"hello\n"
    await inst.write_file("a.txt", body)
    res = await ExactReplaceTool().run(
        _a(path="a.txt", edits=[{"old_string": "hello", "new_string": "hi"}],
           expected_sha256=hashlib.sha256(body).hexdigest()),
        ctx,
    )
    assert res.success, res.content
    assert await _read(inst, "a.txt") == "hi\n"
    await inst.destroy()


async def test_elision_marker_in_new_no_change():
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"hello\n")
    res = await ExactReplaceTool().run(
        _a(path="a.txt", edits=[{"old_string": "hello", "new_string": "x <4500 chars elided> y"}]), ctx
    )
    assert not res.success and res.error == "ELISION_MARKER_REJECTED"
    assert await _read(inst, "a.txt") == "hello\n"
    await inst.destroy()


async def test_new_string_is_literal_not_regex():
    # '$', '${x}', backrefs must be written verbatim (str slicing, not re.sub).
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"PLACEHOLDER\n")
    literal = "$1 ${x} \\1 costs $5"
    res = await ExactReplaceTool().run(
        _a(path="a.txt", edits=[{"old_string": "PLACEHOLDER", "new_string": literal}]), ctx
    )
    assert res.success, res.content
    assert await _read(inst, "a.txt") == literal + "\n"
    await inst.destroy()


async def test_syntax_introducing_batch_no_change():
    ctx, inst = await _ctx()
    await inst.write_file("m.py", b"x = 1\n")
    res = await ExactReplaceTool().run(
        _a(path="m.py", edits=[{"old_string": "x = 1", "new_string": "x = ("}]), ctx
    )
    assert not res.success and res.error == "EXACT_REPLACE_BATCH_FAILED"
    assert await _read(inst, "m.py") == "x = 1\n"
    await inst.destroy()


async def test_large_file_without_read_requires_fresh_read():
    ctx, inst = await _ctx()
    body = ("\n".join(f"line {i} {'z' * 40}" for i in range(1, 80))).encode()  # >1500 bytes
    await inst.write_file("big.py", body)
    res = await ExactReplaceTool().run(
        _a(path="big.py", edits=[{"old_string": "line 40", "new_string": "line FORTY"}]), ctx
    )
    assert not res.success and res.error == "FRESH_READ_REQUIRED"
    assert (await inst.read_file("big.py")) == body  # untouched
    await inst.destroy()


async def test_large_file_after_fresh_read_applies():
    ctx, inst = await _ctx()
    body = ("\n".join(f"line {i} {'z' * 40}" for i in range(1, 80))).encode()
    await inst.write_file("big.py", body)
    await FileReadTool().run(FileReadTool.definition.args_model(path="big.py"), ctx)  # ground it
    res = await ExactReplaceTool().run(
        _a(path="big.py", edits=[{"old_string": "line 40 " + "z" * 40, "new_string": "line FORTY"}]), ctx
    )
    assert res.success, res.content
    assert "line FORTY" in await _read(inst, "big.py")
    await inst.destroy()


async def test_offset_past_eof_does_not_grant_grounding():
    # a file_read PAST end-of-file shows nothing → it must NOT count as grounding, or it would
    # bypass the fresh-edit guard on a large unread file (Codex CD-TOOLS-2 round-2).
    ctx, inst = await _ctx()
    body = ("\n".join(f"line {i} {'z' * 40}" for i in range(1, 80))).encode()  # >1500B
    await inst.write_file("big.py", body)
    await FileReadTool().run(FileReadTool.definition.args_model(path="big.py", offset=99999), ctx)
    res = await ExactReplaceTool().run(
        _a(path="big.py", edits=[{"old_string": "line 40 " + "z" * 40, "new_string": "X"}]), ctx
    )
    assert not res.success and res.error == "FRESH_READ_REQUIRED"
    assert (await inst.read_file("big.py")) == body
    await inst.destroy()


async def test_small_file_needs_no_read():
    # a small file (<=1500B) is always in context → no fresh read required even with default require_fresh_read.
    ctx, inst = await _ctx()
    await inst.write_file("s.txt", b"tiny content here\n")
    res = await ExactReplaceTool().run(_a(path="s.txt", edits=[{"old_string": "tiny", "new_string": "small"}]), ctx)
    assert res.success, res.content
    await inst.destroy()
