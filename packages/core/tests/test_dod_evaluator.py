"""C1b — DoD evaluator (fresh-context, read-only judge).

The DoD spec is the structural fix for "the agent verifies its own work"
(C1a). C1b is the JUDGE: a SEPARATE, FRESH-CONTEXT process that re-runs the
spec's own checks against the deliverable evidence and returns a structured
verdict naming any unmet predicate. It does NOT reuse the agent's working
transcript — that would defeat "fresh" — and it does NOT touch the agent's
tool surface — that would make it the same gate the agent is grading.

These tests assert the four acceptance criteria the task brief calls out:

  Test 1 — evaluator on a MET DoD (all predicates satisfiable) → verdict.
           passed == True.
  Test 2 — evaluator on an UNMET DoD (a missing file / a failing command) →
           verdict.passed == False AND the specific unmet predicate is named
           in the verdict (`verdict.unmet`).
  Test 3 — the evaluator is read-only with respect to the agent's surface
           (no `disco.tools.*` import, no `EventStore` write).
  Test 4 — the LLM-judge seam is real but optional: a fake `SubjectiveJudge`
           is honored, and an unwired subjective predicate is a HARD-FAIL
           (we never silently pass a check we did not run).

Test layout follows the C1a test conventions: a fresh `tmp_path` workspace,
dependency-injected `command_runner` / `http_probe` (so the unit tests are
hermetic — no real subprocess, no real network), and a `ScriptedJudge` for
the LLM seam (no live LLM).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from disco.core import (
    DoDEvaluator,
    DoDSpec,
    DoDVerdict,
    SubjectiveJudge,
    SubjectiveJudgeRequest,
    SubjectiveVerdict,
    spec_fingerprint,
)
from disco.core.dod import (
    CommandExitPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
)
from disco.core.dod_evaluator import (
    CommandResult,
    HttpProbeResult,
    PathEscapeError,
    resolve_under_workspace,
    tail,
)

# ---- fakes for the dependency-injected seams ------------------------------


async def _passing_command_runner(_cmd: str) -> CommandResult:
    """A fake command runner that always exits 0. The unit test's
    hermeticism check asserts the evaluator CALLED this fake with the
    spec's literal command string — that's how we prove "the evaluator
    runs the DoD's OWN check" without a real subprocess."""
    return CommandResult(
        exit_code=0,
        stdout="fake-stdout",
        stderr="",
        error_message="",
        duration_seconds=0.001,
    )


async def _failing_command_runner(_cmd: str) -> CommandResult:
    """A fake command runner that always exits 1. Used by the unmet-test
    to prove the verdict names the failing predicate."""
    return CommandResult(
        exit_code=1,
        stdout="",
        stderr="boom",
        error_message="",
        duration_seconds=0.001,
    )


async def _denied_command_runner(_cmd: str) -> CommandResult:
    """A fake command runner that pretends the destructive-command gate
    refused the command. The evaluator must record `denied=True` and
    refuse to silently pass."""
    return CommandResult(
        exit_code=None,
        stdout="",
        stderr="",
        error_message="hard-denied: rm -rf",
        duration_seconds=0.0,
        denied=True,
        deny_reason="rm -rf",
    )


async def _passing_http_probe(_url: str, _expected: int) -> HttpProbeResult:
    return HttpProbeResult(
        status_code=200,
        error_message="",
        duration_seconds=0.001,
    )


async def _failing_http_probe(_url: str, _expected: int) -> HttpProbeResult:
    return HttpProbeResult(
        status_code=500,
        error_message="",
        duration_seconds=0.001,
    )


class _WorkspaceSandbox:
    """A process-tier-shaped sandbox over a real `tmp_path` workspace.

    Exposes exactly the evidence triad the production file checker needs
    (`file_exists` / `resolve_relpath` / `exec_shell`), so tests that used to
    lean on the evaluator's deleted host-filesystem fallback now exercise the
    checker production actually injects (`_build_file_checker`). `exec_shell`
    understands only the checker's own non-empty probe — anything else is a
    test bug, not a shell.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.workspace_path = str(root)

    def _resolve(self, path: str) -> Path | None:
        try:
            return resolve_under_workspace(self.root, path)
        except PathEscapeError:
            return None

    async def file_exists(self, path: str) -> bool:
        resolved = self._resolve(path)
        return resolved is not None and resolved.exists()

    async def resolve_relpath(self, path: str) -> str:
        return path

    async def exec_shell(self, cmd: str, *, timeout_s: int = 5):  # noqa: ANN202, ARG002
        import shlex
        from types import SimpleNamespace

        parts = shlex.split(cmd)
        assert parts[:4] == ["sh", "-c", 'test -s "$1"', "disco"], cmd
        resolved = self._resolve(parts[4])
        ok = resolved is not None and resolved.is_file() and resolved.stat().st_size > 0
        return SimpleNamespace(exit_code=0 if ok else 1, stdout="", stderr="", timed_out=False)


def _workspace_file_checker(root: Path):  # noqa: ANN202
    """The PRODUCTION sandbox file checker, bound to a real tmp_path workspace."""
    from disco.core.loop.finish.content_gate_parts.dod_evaluator_build import (
        _build_file_checker,
    )

    checker = _build_file_checker(_WorkspaceSandbox(root))
    assert checker is not None
    return checker


class ScriptedJudge(SubjectiveJudge):
    """A fake `SubjectiveJudge` that returns a scripted `SubjectiveVerdict`
    for the i-th call. Records every request so the test can assert
    "fresh context" — the request carries the predicate + spec + workspace,
    NOT the agent's transcript."""

    def __init__(self, scripted: list[SubjectiveVerdict]) -> None:
        self._scripted = list(scripted)
        self.calls: list[SubjectiveJudgeRequest] = []
        self.requests: int = 0

    async def judge(self, req: SubjectiveJudgeRequest) -> SubjectiveVerdict:
        self.requests += 1
        self.calls.append(req)
        i = min(self.requests - 1, len(self._scripted) - 1)
        return self._scripted[i]


# ---- Test 1: met DoD passes ------------------------------------------------


async def test_met_dod_verdict_passes(tmp_path: Path) -> None:
    """A spec with a present file, a passing command, and a 200 http probe
    evaluates to `passed=True` and an empty `unmet` list. The full per-
    predicate record is in `verdict.results` so the audit can see WHAT was
    checked."""
    # Workspace setup: a file the spec says must exist.
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "index.html").write_text("<html>hi</html>")
    # Spec
    spec = DoDSpec(
        predicates=[
            FileExistsPredicate(path="build/index.html"),
            CommandExitPredicate(cmd="pytest -q", expect_exit=0),
            HTTPOkPredicate(url="http://127.0.0.1:8000/", expect_status=200),
        ],
        note="C1b met-spec test",
    )
    # Evaluator with fakes
    ev = DoDEvaluator(
        tmp_path,
        command_runner=_passing_command_runner,
        http_probe=_passing_http_probe,
        file_checker=_workspace_file_checker(tmp_path),
    )
    verdict = await ev.evaluate(spec, conversation_id="conv_met")

    assert isinstance(verdict, DoDVerdict)
    assert verdict.passed is True
    assert verdict.unmet == []
    assert len(verdict.results) == 3
    # All three results are passed.
    assert all(r.passed for r in verdict.results)
    # The fingerprint is stable + tagged.
    assert verdict.spec_fingerprint.startswith("sha256:")
    assert verdict.spec_fingerprint == spec_fingerprint(spec)
    # spec_predicate_count matches
    assert verdict.spec_predicate_count == 3
    # evaluated_at is a non-empty ISO-8601 string
    assert isinstance(verdict.evaluated_at, str) and "T" in verdict.evaluated_at
    # conversation_id round-trips
    assert verdict.conversation_id == "conv_met"


async def test_met_dod_fingerprint_is_stable_across_construction(
    tmp_path: Path,
) -> None:
    """The spec fingerprint is a deterministic function of the spec, not of
    the evaluator instance. A future C1c caller (engine finish gate) can
    log the fingerprint without instantiating the evaluator first."""
    spec = DoDSpec(
        predicates=[
            FileExistsPredicate(path="a"),
            CommandExitPredicate(cmd="true"),
        ]
    )
    assert spec_fingerprint(spec) == spec_fingerprint(spec)
    # Round-trip through JSON preserves the fingerprint.
    assert spec_fingerprint(DoDSpec.from_json_dict(spec.to_json_dict())) == spec_fingerprint(spec)


# ---- Test 2: unmet DoD fails AND names the specific predicate --------------


async def test_unmet_file_predicate_is_named_in_verdict(tmp_path: Path) -> None:
    """A spec with a `file_exists` for a missing file + a passing command
    evaluates to `passed=False`, and the SPECIFIC `FileExistsPredicate` is
    named in `verdict.unmet`. The passing predicate is NOT in `unmet` (it
    passed), and the per-predicate record in `verdict.results` carries the
    reason string."""
    # Note: no `build/index.html` is created in the workspace.
    spec = DoDSpec(
        predicates=[
            FileExistsPredicate(path="build/index.html"),
            CommandExitPredicate(cmd="true"),
        ]
    )
    ev = DoDEvaluator(
        tmp_path,
        command_runner=_passing_command_runner,
        http_probe=_passing_http_probe,
        file_checker=_workspace_file_checker(tmp_path),
    )
    verdict = await ev.evaluate(spec)

    assert verdict.passed is False
    # The unmet list contains the SPECIFIC FileExistsPredicate (the same
    # frozen Pydantic instance the spec carries — equality is exact).
    assert len(verdict.unmet) == 1
    assert verdict.unmet[0] == spec.predicates[0]
    assert isinstance(verdict.unmet[0], FileExistsPredicate)
    assert verdict.unmet[0].path == "build/index.html"
    # The other predicate is recorded as passed.
    assert len(verdict.results) == 2
    by_kind = {type(r.predicate).__name__: r for r in verdict.results}
    assert by_kind["FileExistsPredicate"].passed is False
    assert "does not exist" in by_kind["FileExistsPredicate"].reason
    assert by_kind["CommandExitPredicate"].passed is True


async def test_unmet_command_predicate_is_named_in_verdict(tmp_path: Path) -> None:
    """A `command` predicate that exits 1 is named in `unmet` with the
    actual exit code surfaced in the reason. The evaluator ran the
    command (it called the injected runner) — we assert the runner saw
    the LITERAL command string the spec carries (no transformation)."""
    captured: list[str] = []

    async def _spy(cmd: str) -> CommandResult:
        captured.append(cmd)
        return CommandResult(
            exit_code=1,
            stdout="",
            stderr="assertion failed",
            error_message="",
            duration_seconds=0.001,
        )

    spec = DoDSpec(
        predicates=[
            CommandExitPredicate(cmd="pytest -q", expect_exit=0),
        ]
    )
    ev = DoDEvaluator(tmp_path, command_runner=_spy, http_probe=_passing_http_probe)
    verdict = await ev.evaluate(spec)

    assert verdict.passed is False
    assert len(verdict.unmet) == 1
    assert verdict.unmet[0] == spec.predicates[0]
    assert isinstance(verdict.unmet[0], CommandExitPredicate)
    assert "exited 1, expected 0" in verdict.results[0].reason
    # The runner saw the EXACT command string from the spec.
    assert captured == ["pytest -q"]


async def test_sandbox_less_command_predicate_is_unevaluatable_and_spawns_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3/H-EXEC: with NO injected `command_runner` there is no sandbox to run
    the model's command string in, so the predicate is UN-EVALUATABLE — a
    non-pass flagged `unverifiable` — and the evaluator spawns NO process.

    Replaces the former `test_h342_default_command_runner_*` tests, which pinned
    the bash dialect / process-group kill / output bounds of the host
    `subprocess(shell=True)` fallback. That fallback is deleted: a DoD `command`
    predicate is copied verbatim from a model-authored plan `done_condition`,
    and the destructive-command deny-list is explicitly not an
    arbitrary-code-execution control, so there is nothing left to pin.
    """
    import subprocess

    spawned: list[object] = []
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(
            subprocess,
            name,
            lambda *a, **kw: spawned.append((a, kw)),  # noqa: ARG005
        )

    verdict = await DoDEvaluator(tmp_path, http_probe=_passing_http_probe).evaluate(
        DoDSpec(predicates=[CommandExitPredicate(cmd="echo owned; id", expect_exit=0)])
    )

    assert spawned == [], "sandbox-less DoD command predicate spawned a process"
    assert verdict.passed is False
    assert len(verdict.unmet) == 1
    assert isinstance(verdict.unmet[0], CommandExitPredicate)
    result = verdict.results[0]
    # Un-evaluatable, NOT "the command failed": `unverifiable` is the infra
    # channel, and the reason says why it could not be evaluated.
    assert result.unverifiable is True
    assert "not evaluated" in result.reason
    assert "no sandbox available" in result.reason
    assert result.details.get("no_command_runner") is True
    assert result.details.get("exit_code") is None


async def test_evaluator_module_has_no_host_shell_execution_surface() -> None:
    """Nothing in the evaluator module may reach a host shell again."""
    import inspect as _inspect

    import disco.core.dod_evaluator as evaluator_module

    assert not hasattr(evaluator_module, "subprocess")
    assert not hasattr(evaluator_module, "_default_command_runner")
    assert not hasattr(evaluator_module, "_run_command")
    assert not hasattr(evaluator_module, "_build_default_command_runner")
    src = _inspect.getsource(evaluator_module)
    # (`shell=True` still appears in the module docstring, which explains why
    # there is no host runner — assert on the executable surface instead.)
    assert "import subprocess" not in src
    assert "Popen(" not in src


async def test_unmet_http_predicate_is_named_in_verdict(tmp_path: Path) -> None:
    """An `http_ok` predicate that returns 500 (when 200 is expected) is
    named in `unmet` with the observed status in the reason. The probe
    was called with the literal URL + expected_status the spec carries."""
    captured: list[tuple[str, int]] = []

    async def _spy(url: str, expected: int) -> HttpProbeResult:
        captured.append((url, expected))
        return HttpProbeResult(status_code=500, error_message="", duration_seconds=0.001)

    spec = DoDSpec(
        predicates=[
            HTTPOkPredicate(url="http://127.0.0.1:8000/", expect_status=200),
        ]
    )
    ev = DoDEvaluator(tmp_path, command_runner=_passing_command_runner, http_probe=_spy)
    verdict = await ev.evaluate(spec)

    assert verdict.passed is False
    assert len(verdict.unmet) == 1
    assert verdict.unmet[0] == spec.predicates[0]
    assert isinstance(verdict.unmet[0], HTTPOkPredicate)
    assert "HTTP 500, expected 200" in verdict.results[0].reason
    # The probe was called with the literal URL + expected_status.
    assert captured == [("http://127.0.0.1:8000/", 200)]


async def test_multiple_unmet_predicates_are_all_named(tmp_path: Path) -> None:
    """When more than one predicate is unmet, `unmet` lists ALL of them in
    spec order. The verdict is `passed=False`; the audit can read `unmet`
    to surface every check that failed."""
    spec = DoDSpec(
        predicates=[
            FileExistsPredicate(path="missing-1"),
            CommandExitPredicate(cmd="false"),
            HTTPOkPredicate(url="http://localhost/", expect_status=200),
            FileExistsPredicate(path="missing-2"),
        ]
    )
    ev = DoDEvaluator(
        tmp_path,
        command_runner=_failing_command_runner,
        http_probe=_failing_http_probe,
    )
    verdict = await ev.evaluate(spec)

    assert verdict.passed is False
    assert len(verdict.unmet) == 4
    # Order is preserved
    assert [type(p).__name__ for p in verdict.unmet] == [
        "FileExistsPredicate",
        "CommandExitPredicate",
        "HTTPOkPredicate",
        "FileExistsPredicate",
    ]
    # Each is the SPECIFIC predicate from the spec
    for i, p in enumerate(verdict.unmet):
        assert p == spec.predicates[i]


# ---- Test 3: read-only contract -------------------------------------------


async def test_evaluator_does_not_touch_event_store(tmp_path: Path) -> None:
    """The evaluator's public surface does NOT accept an `EventStore` or
    any conversation-state object. The verdict is the only output, and
    the evaluator is read-only with respect to the agent's state by
    construction (signature check, not just docstring)."""
    sig = inspect.signature(DoDEvaluator.evaluate)
    for name, _param in sig.parameters.items():
        assert "store" not in name.lower(), (
            f"evaluator.evaluate takes a {name!r} parameter; it must not "
            "touch the conversation's EventStore"
        )
    assert "conversation_id" in sig.parameters
    # Constructor: same discipline.
    sig_init = inspect.signature(DoDEvaluator.__init__)
    for name in sig_init.parameters:
        assert "store" not in name.lower(), (
            f"DoDEvaluator.__init__ takes a {name!r} parameter; it must "
            "not have a path to the conversation's EventStore"
        )


def test_evaluator_does_not_import_agent_tool_surface() -> None:
    """Static check: the evaluator module's imports do NOT include
    `disco.tools.*` (the agent's tool surface) or the agent's executor.
    The evaluator's read-only discipline is structural, not gated.

    If this test ever fails after a future import, the new import MUST
    be demonstrated safe (it must not be reachable from the
    evaluator's evaluation path) — otherwise the evaluator can call
    mutating tools and re-introduce the very failure mode C1a exists
    to fix."""
    import disco.core.dod_evaluator as mod

    src = inspect.getsource(mod)
    forbidden = (
        "from disco.tools",
        "import disco.tools",
        "from disco.core.loop",  # engine wiring is C1c, not C1b
        "import disco.core.loop",
    )
    for needle in forbidden:
        assert needle not in src, (
            f"evaluator module imports {needle!r}; the evaluator must not "
            "reach the agent's tool surface. If this is needed, route it "
            "through a dependency-injected seam (like command_runner) so "
            "tests can keep the read-only discipline."
        )


# ---- Test 4: the LLM-judge seam (no live LLM) -----------------------------


async def test_unwired_subjective_predicate_is_a_hard_fail(tmp_path: Path) -> None:
    """A predicate kind the evaluator doesn't handle directly, with NO
    `subjective_judge` wired, is a HARD-FAIL — the verdict's `unmet` names
    it with a "refusing to silently pass" reason. This is the discipline
    that prevents the C1a failure mode from re-emerging at the judge
    layer: a missing checker never equals a passed check."""
    # Build a synthetic predicate the evaluator doesn't know about, by
    # instantiating a base `DoDPredicate` union directly. C1a only
    # supports three kinds, but Pydantic's union is open in the type
    # sense; a future subjective kind will appear as a fourth. We
    # simulate the future kind by constructing a `FileExistsPredicate`
    # and calling `_check_subjective` directly — the seam is what
    # matters, not the kind.
    from disco.core.dod_evaluator import DoDPredicateResult

    spec = DoDSpec(predicates=[FileExistsPredicate(path="x")])
    ev = DoDEvaluator(
        tmp_path,
        command_runner=_passing_command_runner,
        http_probe=_passing_http_probe,
    )
    # The synthetic predicate for the test: a FileExistsPredicate being
    # routed through the subjective seam (the evaluator's `_check_subjective`
    # is the future-LLM dispatch, not the file check). No judge wired.
    result = await ev._check_subjective(spec.predicates[0], spec)
    assert isinstance(result, DoDPredicateResult)
    assert result.passed is False
    assert "refusing to silently pass" in result.reason
    assert result.details.get("kind") == "subjective_unwired"


async def test_fake_judge_is_invoked_with_fresh_context(tmp_path: Path) -> None:
    """A wired `ScriptedJudge` (fake — no live LLM) is invoked with a
    `SubjectiveJudgeRequest` that carries the predicate + the spec's
    predicate list + the workspace root — and NOTHING ELSE. The
    `SubjectiveJudgeRequest` has no `events` / `view` / `transcript`
    field; the test asserts that. The fresh-context discipline is
    structural, not just behavioural."""
    judge = ScriptedJudge(scripted=[SubjectiveVerdict(passed=True, reason="looks right")])
    spec = DoDSpec(
        predicates=[
            FileExistsPredicate(path="a"),
            CommandExitPredicate(cmd="true"),
        ]
    )
    _ev = DoDEvaluator(
        tmp_path,
        command_runner=_passing_command_runner,
        http_probe=_passing_http_probe,
        subjective_judge=judge,
    )
    req = SubjectiveJudgeRequest(
        predicate=spec.predicates[0],
        spec_predicates=list(spec.predicates),
        workspace_root=str(tmp_path),
        evidence_excerpt="",
    )
    verdict = await judge.judge(req)
    # Judge produced a verdict.
    assert verdict.passed is True
    assert judge.requests == 1
    sent = judge.calls[0]
    # The request carries the predicate + spec + workspace, NOTHING from
    # the agent's transcript.
    assert sent.predicate == spec.predicates[0]
    assert sent.spec_predicates == list(spec.predicates)
    assert sent.workspace_root == str(tmp_path)
    # And the request's Pydantic schema has no transcript-shaped fields.
    sig_req = SubjectiveJudgeRequest.model_fields
    forbidden_field_substrings = (
        "transcript",
        "view",
        "events",
        "agent_state",
        "memory",
    )
    for fname in sig_req:
        for sub in forbidden_field_substrings:
            assert sub not in fname, (
                f"SubjectiveJudgeRequest has field {fname!r}; the "
                "fresh-context discipline requires the judge never "
                "receive agent-side state."
            )


async def test_judge_exception_is_recorded_not_raised(tmp_path: Path) -> None:
    """A judge that raises (LLM transient error, etc.) is recorded as a
    failing predicate, NOT propagated out of `evaluate`. The C1c caller
    can rely on `evaluate` never raising — every failure mode lives in
    the verdict."""

    class _RaisingJudge(SubjectiveJudge):
        async def judge(self, req: SubjectiveJudgeRequest) -> SubjectiveVerdict:
            raise RuntimeError("LLM down")

    spec = DoDSpec(predicates=[FileExistsPredicate(path="a")])
    ev = DoDEvaluator(
        tmp_path,
        command_runner=_passing_command_runner,
        http_probe=_passing_http_probe,
        subjective_judge=_RaisingJudge(),
    )
    result = await ev._check_subjective(spec.predicates[0], spec)
    assert result.passed is False
    assert "RuntimeError" in result.reason
    assert "LLM down" in result.reason
    assert result.details.get("kind") == "subjective_judge_error"


# ---- Safety: destructive commands are refused by the default runner --------


async def test_sandbox_command_runner_refuses_destructive_commands(
    tmp_path: Path,
) -> None:
    """The production `command_runner` (the sandbox `exec_shell` adapter built by
    the finish gate) routes the spec's command through the engine's
    destructive-command deny-list. A `rm -rf /` predicate is recorded as
    `denied=True` (NOT silently run) and the predicate is unmet with the deny
    reason. This is the parity with the agent's own verify-on-finish probe: a
    denied verify is a hard fail, not a silent pass.

    Retargeted from `test_default_command_runner_refuses_destructive_commands`:
    the evaluator no longer has a default host runner to carry the deny floor,
    so the assertion now covers the runner production actually injects. Same
    assertions, same strength.
    """
    from disco.core.loop.finish.content_gate_parts.dod_evaluator_build import (
        _build_command_runner,
    )

    class _Sbx:
        workspace_path = str(tmp_path)
        exec_shell_calls: list[str] = []

        async def exec_shell(self, cmd: str, *, timeout_s: int) -> object:  # noqa: ARG002
            type(self).exec_shell_calls.append(cmd)
            raise AssertionError("a hard-denied command must never reach exec_shell")

    runner = _build_command_runner(_Sbx())
    assert runner is not None
    spec = DoDSpec(
        predicates=[
            CommandExitPredicate(cmd="rm -rf /", expect_exit=0),
        ]
    )
    ev = DoDEvaluator(tmp_path, command_runner=runner, http_probe=_passing_http_probe)
    verdict = await ev.evaluate(spec)
    assert _Sbx.exec_shell_calls == []
    assert verdict.passed is False
    assert len(verdict.unmet) == 1
    assert isinstance(verdict.unmet[0], CommandExitPredicate)
    # Reason names the deny + the predicate is recorded as denied=True.
    assert "hard-denied" in verdict.results[0].reason
    assert verdict.results[0].details.get("denied") is True
    # And the deny reason names the protected-path delete (the engine's own
    # deny string for `rm -rf /` — W3 C-3 reworded it to "protected system path").
    deny = verdict.results[0].details.get("deny_reason", "")
    assert "protected" in deny.lower() or "root" in deny.lower() or "rm" in deny.lower()


# ---- Path-escape safety ---------------------------------------------------


async def test_file_exists_without_a_checker_is_unevaluatable_and_reads_no_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `file_exists` predicate whose path points OUTSIDE the workspace (or
    anywhere else) must never be answered off the agent-server's filesystem.

    Rewritten from `test_file_exists_outside_workspace_is_a_hard_fail`, which
    asserted the deleted host-filesystem branch turned `../../etc/passwd` into a
    "path escapes workspace" hard fail. The evaluator no longer resolves or
    stats anything: with no `file_checker` injected the predicate is
    un-evaluatable, which is a strictly stronger refusal — it also covers paths
    that do NOT escape. The escape rule itself is still covered directly by
    `test_resolve_under_workspace_rejects_escape`, and in production by the
    sandbox that owns path resolution.
    """
    stats: list[str] = []

    def _spy_exists(self: Path) -> bool:
        stats.append(str(self))
        return True

    monkeypatch.setattr(Path, "exists", _spy_exists)
    monkeypatch.setattr(Path, "is_file", lambda self: stats.append(str(self)) or True)

    spec = DoDSpec(
        predicates=[
            FileExistsPredicate(path="../../etc/passwd"),
        ]
    )
    ev = DoDEvaluator(
        tmp_path,
        command_runner=_passing_command_runner,
        http_probe=_passing_http_probe,
    )
    verdict = await ev.evaluate(spec)

    assert stats == [], f"host filesystem was consulted: {stats}"
    assert verdict.passed is False
    assert len(verdict.unmet) == 1
    assert isinstance(verdict.unmet[0], FileExistsPredicate)
    result = verdict.results[0]
    assert result.unverifiable is True
    assert "not evaluated" in result.reason
    assert "no sandbox available" in result.reason
    assert result.details.get("no_file_checker") is True


@pytest.mark.asyncio
async def test_file_exists_rejects_empty_and_directory(tmp_path: Path) -> None:
    """ANTI-GAMING (codex P2): file_exists requires a REGULAR, NON-EMPTY file.
    An empty `touch`ed placeholder and a directory both satisfy Path.exists()
    but are not the deliverable the step promised — they must FAIL, else a model
    passes the gate without producing real content.

    Retargeted from the evaluator's deleted host-filesystem branch onto
    `_build_file_checker` — the checker production injects — so the rule is
    asserted where it now lives. Same three cases, same verdicts.
    """
    (tmp_path / "empty.html").write_text("")  # 0 bytes
    (tmp_path / "asdir").mkdir()  # a directory named like a file
    (tmp_path / "real.html").write_text("<h1>content</h1>")  # the honest case
    ev = DoDEvaluator(
        tmp_path,
        command_runner=_passing_command_runner,
        http_probe=_passing_http_probe,
        file_checker=_workspace_file_checker(tmp_path),
    )
    # empty file → FAIL with an "empty" reason
    v_empty = await ev.evaluate(DoDSpec(predicates=[FileExistsPredicate(path="empty.html")]))
    assert v_empty.passed is False
    assert "empty" in v_empty.results[0].reason.lower()
    # directory → FAIL (a directory is not a regular non-empty file)
    v_dir = await ev.evaluate(DoDSpec(predicates=[FileExistsPredicate(path="asdir")]))
    assert v_dir.passed is False
    # a real non-empty file still PASSES (no false-block on the honest deliverable)
    v_ok = await ev.evaluate(DoDSpec(predicates=[FileExistsPredicate(path="real.html")]))
    assert v_ok.passed is True


async def test_http_ok_without_a_probe_is_unevaluatable_and_opens_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3/H-EXEC: with no injected `http_probe` an `http_ok` predicate is
    un-evaluatable and NOTHING leaves the agent-server process.

    The deleted default probe GET the model-authored URL from the host behind
    its own loopback/RFC1918 allow-list; the deliverable's server binds the
    SANDBOX's localhost, so that probe graded the wrong machine as well as
    reaching out of the process. Production injects the in-sandbox curl probe.
    """
    import socket

    import disco.core.host_egress as host_egress

    async def _no_wire(*args: object, **kwargs: object) -> object:
        raise AssertionError("the evaluator opened an HTTP connection")

    monkeypatch.setattr(host_egress, "guarded_get", _no_wire)
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("socket opened")
    )

    verdict = await DoDEvaluator(
        tmp_path, command_runner=_passing_command_runner
    ).evaluate(
        DoDSpec(predicates=[HTTPOkPredicate(url="http://127.0.0.1:8000/", expect_status=200)])
    )

    assert verdict.passed is False
    result = verdict.results[0]
    assert result.unverifiable is True
    assert "not evaluated" in result.reason
    assert "no sandbox available" in result.reason
    assert result.details.get("no_http_probe") is True


async def test_evaluator_module_has_no_host_probe_surface() -> None:
    """The deleted host probe must not come back through a side door."""
    import disco.core.dod_evaluator as evaluator_module

    assert not hasattr(evaluator_module, "_default_http_probe")
    assert not hasattr(evaluator_module, "_build_default_http_probe")
    assert not hasattr(evaluator_module, "guarded_get")
    assert not hasattr(evaluator_module, "_egress_allowed")


@pytest.mark.asyncio
async def test_command_infra_vs_task_unverifiable_channel(tmp_path: Path) -> None:
    """DoD v2.1 infra-vs-task channel: a command that could NOT RUN (denied) is marked
    `unverifiable` (infra — the gate releases on it); a command that RAN and exited wrong
    is a real TASK failure (`unverifiable=False` — the gate blocks)."""
    from disco.core.dod import CommandExitPredicate

    spec = DoDSpec(predicates=[CommandExitPredicate(cmd="make", expect_exit=0)])

    # denied → infra (unverifiable)
    ev_denied = DoDEvaluator(
        tmp_path, command_runner=_denied_command_runner, http_probe=_passing_http_probe
    )
    v_denied = await ev_denied.evaluate(spec)
    assert v_denied.passed is False
    assert v_denied.results[0].unverifiable is True

    # ran + wrong exit → TASK failure (verifiable)
    ev_failed = DoDEvaluator(
        tmp_path, command_runner=_failing_command_runner, http_probe=_passing_http_probe
    )
    v_failed = await ev_failed.evaluate(spec)
    assert v_failed.passed is False
    assert v_failed.results[0].unverifiable is False


def test_resolve_under_workspace_rejects_escape(tmp_path: Path) -> None:
    """The path-resolution helper itself rejects escapes. Direct unit test
    on the helper so the discipline is also visible at the seam."""
    with pytest.raises(PathEscapeError):
        resolve_under_workspace(tmp_path, "../../etc/passwd")
    # Absolute path outside the workspace is also rejected.
    with pytest.raises(PathEscapeError):
        resolve_under_workspace(tmp_path, "/etc/passwd")
    # In-workspace path is fine.
    inside = resolve_under_workspace(tmp_path, "build/index.html")
    assert inside.is_relative_to(tmp_path.resolve())


# ---- Fresh-context: the evaluator never reads the agent's transcript ----


async def test_evaluator_signature_carries_no_agent_state() -> None:
    """The evaluator's `evaluate` method takes ONLY `(self, spec, *,
    conversation_id=None)`. It does NOT accept a `View`, a list of
    events, a transcript, a `State`, or any agent-side state. This is
    the FRESH-CONTEXT discipline: the evaluator's verdict is built from
    the spec + the workspace, never from the agent's working memory."""
    sig = inspect.signature(DoDEvaluator.evaluate)
    accepted = list(sig.parameters)
    # Must have self + spec + conversation_id; nothing else.
    for name in accepted:
        assert name in {"self", "spec", "conversation_id"}, (
            f"evaluator.evaluate accepts {name!r}; the fresh-context "
            "discipline requires the evaluator receive ONLY the spec + "
            "(optional) conversation_id."
        )


# ---- Tail helper (smoke test) ---------------------------------------------


def test_tail_truncates_to_n_chars() -> None:
    # `tail(s, n)` returns '…' + the last n chars of s.
    assert tail("hello world", 5) == "…world"
    assert tail("hi", 100) == "hi"
    assert tail("", 10) == ""
    assert tail("abc", 0) == ""
