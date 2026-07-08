"""K1 executor-boundary elision guard (Bug 14 — Build Soak repair #7).

# Contract

`events._snip_args` renders an over-long tool argument as a short placeholder in
the action HISTORY (`[[DISCO-ELIDED: N chars ...]]`). A weak
model can COPY that placeholder back into a REAL tool argument on a later turn.

The Observer rejects this before `executor.execute` (see core/test_k1_elision_guard),
but NOT every execution path funnels through the Observer. `DefaultToolExecutor.execute`
is the universal chokepoint every tool call passes through, so it carries a GENERIC
guard: BEFORE arg validation / `tool.run`, any call whose arguments carry an elision
marker is rejected with a recoverable `invalid_arguments` failure and is NEVER run —
so the placeholder can't mutate disk on ANY path, for ANY mutating tool.

# Acceptance (this file)

  1. file_replace_lines whose `new_text` is the (count-anchored) marker → rejected at
     the executor: a failed ToolResult (invalid_arguments), the tool NEVER runs, and
     the target file on disk is UNCHANGED.
  2. file_write whose `content` is a PARAPHRASED marker (no count anchor) → also
     rejected at the executor (structure, not wording).
  3. a clean file_replace_lines executes normally (the guard is invisible).
  4. a benign arg that merely resembles the marker shape but lacks the signature
     phrase → NOT rejected (no false positive).
"""

from __future__ import annotations

from disco.core.events import _snip_args
from disco.core.llm import ModelExecutionPolicy
from disco.tools import DefaultToolExecutor, agent_scope, build_default_registry
from tool_fakes import FakeSandboxInstance, call

_STANDARD = ModelExecutionPolicy.standard()

# The paraphrased marker (count anchor dropped) the model copied live in BW-02.
_PARAPHRASE_MARKER = (
    "<content elided — re-issue the call or file_read the path for the full "
    "content; do not copy this placeholder into a tool argument>"
)


def _executor(sandbox):
    return DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=_STANDARD),
        sandbox=sandbox,
    )


async def test_file_replace_lines_marker_rejected_at_executor_no_mutation():
    """The load-bearing fix: file_replace_lines is a MUTATOR the Observer-only K1
    guard left exposed on a direct executor path. The executor rejects the copied
    marker recoverably and the file is never touched."""
    original = b"line one\nline two\nline three\n"
    sandbox = FakeSandboxInstance()
    await sandbox.write_file("src/app.js", original)
    ex = _executor(sandbox)

    # The marker the shaper would render for a real body the model already wrote.
    marker = _snip_args({"new_text": "x" * 5000})["new_text"]
    res = await ex.execute(
        call(
            "file_replace_lines",
            path="src/app.js",
            start_line=1,
            end_line=2,
            new_text=marker,
        )
    )

    # Recoverable failure — NOT a success, NOT a crash.
    assert res.success is False
    assert res.structured["kind"] == "invalid_arguments"
    # The model-visible message names the offending key + tells it how to recover.
    assert "new_text" in res.content
    assert "file_read" in res.content
    assert "NOT executed" in res.content
    # The file on disk is UNTOUCHED — the placeholder never reached the mutator.
    assert await sandbox.read_file("src/app.js") == original


async def test_file_write_paraphrased_marker_rejected_at_executor_no_mutation():
    """A non-count-anchored paraphrase is also rejected at the executor boundary."""
    original = b"export const real = 1;\n"
    sandbox = FakeSandboxInstance()
    await sandbox.write_file("src/x.js", original)
    ex = _executor(sandbox)

    res = await ex.execute(call("file_write", path="src/x.js", content=_PARAPHRASE_MARKER))

    assert res.success is False
    assert res.structured["kind"] == "invalid_arguments"
    assert "content" in res.content
    # Disk unchanged.
    assert await sandbox.read_file("src/x.js") == original


async def test_clean_file_replace_lines_executes_normally():
    """No marker → the guard is invisible and the mutator runs (real edit applied)."""
    sandbox = FakeSandboxInstance()
    await sandbox.write_file("src/app.js", b"line one\nline two\nline three\n")
    ex = _executor(sandbox)

    res = await ex.execute(
        call(
            "file_replace_lines",
            path="src/app.js",
            start_line=2,
            end_line=2,
            new_text="LINE TWO REPLACED",
        )
    )

    assert res.success is True, res.content
    disk = await sandbox.read_file("src/app.js")
    assert b"LINE TWO REPLACED" in disk


async def test_benign_marker_shaped_arg_not_rejected():
    """An arg that merely resembles the marker shape but lacks the signature phrase
    must NOT be rejected (no false positive). Routed through file_write so the only
    thing that could reject it is the elision guard."""
    sandbox = FakeSandboxInstance()
    ex = _executor(sandbox)

    body = "if (s.length < 5) {} // a <5 chars> guard and an <input> element\n"
    res = await ex.execute(call("file_write", path="src/foo.js", content=body))

    # Not rejected by the elision guard (it either succeeds or fails for a reason
    # OTHER than invalid_arguments-on-elision). The guard contributes no rejection.
    if res.success is False:
        assert "elision placeholder" not in res.content
    else:
        assert await sandbox.read_file("src/foo.js") == body.encode()
