"""REL-27 (F-27) — the finish-time workspace sealability gate.

Counted trial 590005 (react_steer): the model symlinked root paths to satisfy
done-conditions, every pre-terminal layer accepted them (sandbox wrote the
links, build + web verification resolved them live), the FINISHED terminal
landed, and only THEN did the strict final seal refuse — leaving a finished
build unattributable/unexportable, disclosed one event too late for anyone to
act. The gate moves that exact strict-seal judgement to the last moment the
model CAN act: an affirmative finish is refused with the exact blocking
entries and the remedy; a model that cannot repair still reaches a terminal
through the loud, honest ``unsealed_release`` valve.

Forced terminals (``noop_limit`` and kin) never claimed a deliverable finish
and deliberately bypass the gate — the commit-time seal plus the typed
post-terminal disclosure remain their backstop.
"""

from __future__ import annotations

from disco.core import ConversationStatus, MessageEvent, StatusEvent
from disco.core.llm import OperatingMode
from disco.core.loop import SealabilityProbeResult
from disco.core.loop.stuck import StuckThresholds
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step

CID = "conv"

# The exact live skip signature from the 590005 dossier (seq 305 / classification).
LIVE_BLOCKING = (
    "dist: symlink excluded",
    "index.html: symlink excluded",
    "package.json: symlink excluded",
    "src/App.jsx: symlink excluded",
)


class ProbeFake:
    """Scripted sealability probe: pops results in order; repeats the last."""

    def __init__(self, *results: SealabilityProbeResult | Exception) -> None:
        self._results = list(results)
        self.calls = 0

    async def __call__(self) -> SealabilityProbeResult:
        self.calls += 1
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(result, Exception):
            raise result
        return result


def _blocking() -> SealabilityProbeResult:
    return SealabilityProbeResult(sealable=False, blocking=LIVE_BLOCKING)


def _sealable() -> SealabilityProbeResult:
    return SealabilityProbeResult(sealable=True)


def _refusals(events) -> list[MessageEvent]:
    return [
        e
        for e in events
        if isinstance(e, MessageEvent) and e.meta.get("blocking") == "finish_seal_refused"
    ]


def _releases(events) -> list[MessageEvent]:
    return [
        e for e in events if isinstance(e, MessageEvent) and e.meta.get("unsealed_release") is True
    ]


def _finished_seqs(events) -> list[int]:
    return [
        e.seq
        for e in events
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
    ]


async def test_blocking_probe_refuses_finish_then_fix_seals():
    """The red→green family test on the exact 590005 signature.

    finish #1 against symlinked content → REFUSED, with the exact blocking
    entries and the remedy in the reminder (the model can still act — the
    contract F-27 broke). The model removes the links (probe now sealable),
    finishes again → clean FINISHED. No terminal ever lands on unsealable
    content from a model finish."""
    probe = ProbeFake(_blocking(), _sealable())
    agent = ScriptedAgent(
        [
            finish_step("all done"),
            action_step("shell", {"command": "rm dist index.html package.json src/App.jsx"}),
            finish_step("links replaced with real files"),
        ]
    )
    loop, store = build_loop(agent, finish_sealability_probe=probe)
    await loop.send_message("build it")
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    refusals = _refusals(events)
    assert len(refusals) == 1
    # The refusal names the EXACT blocking entries, typed and in prose.
    assert tuple(refusals[0].meta["seal_blocking"]) == LIVE_BLOCKING
    content = refusals[0].message.content
    assert "dist: symlink excluded" in content
    assert "cannot be sealed" in content
    assert "finish again" in content  # the remedy is actionable
    # The refusal landed BEFORE the (single) FINISHED terminal — the constraint
    # reached the model while it could still act.
    finished = _finished_seqs(events)
    assert len(finished) == 1
    assert refusals[0].seq < finished[0]
    assert not _releases(events)
    assert probe.calls == 2


async def test_probe_blocking_cap_releases_loudly_unsealed():
    """A model that never repairs still reaches a terminal — loudly.

    Three refusals, then the fourth finish releases with the ``unsealed_release``
    marker + a visible warning naming the blocking entries. The cap breaks the
    loop, never the honesty: the commit-time strict seal still refuses."""
    probe = ProbeFake(_blocking())
    agent = ScriptedAgent([finish_step(f"attempt {i}") for i in range(1, 5)])
    loop, store = build_loop(agent, finish_sealability_probe=probe)
    await loop.send_message("build it")
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    assert len(_refusals(events)) == 3
    releases = _releases(events)
    assert len(releases) == 1
    assert tuple(releases[0].meta["seal_blocking"]) == LIVE_BLOCKING
    assert "UNSEALABLE" in releases[0].message.content
    assert any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.RUNNING
        and e.detail == "unsealed_release"
        for e in events
    )
    assert probe.calls == 4


async def test_sealable_probe_finishes_clean():
    """Positive control — a sealable workspace finishes exactly as before:
    one probe call, no refusal, no release, clean FINISHED."""
    probe = ProbeFake(_sealable())
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent, finish_sealability_probe=probe)
    await loop.send_message("build it")
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    assert not _refusals(events)
    assert not _releases(events)
    assert probe.calls == 1


async def test_no_probe_finish_unchanged():
    """Legacy byte-identity — no probe injected (research loops, existing
    callers) ⇒ no gate artifacts of any kind."""
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("build it")
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    assert not _refusals(events)
    assert not _releases(events)
    assert not any(isinstance(e, StatusEvent) and e.detail == "unsealed_release" for e in events)


async def test_probe_error_never_cages_the_terminal():
    """A broken probe is advisory: the finish proceeds (log-only) and the
    commit-time seal remains the sole publication authority. A probe outage
    must never destroy the route to a valid terminal."""
    probe = ProbeFake(RuntimeError("sandbox transport died"))
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent, finish_sealability_probe=probe)
    await loop.send_message("build it")
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    assert not _refusals(events)
    assert not _releases(events)


async def test_forced_noop_limit_terminal_never_probes():
    """Forced-terminal control — the ``noop_limit`` FINISHED backstop is not an
    affirmative delivery claim and must not invoke the probe at all."""

    class MustNotProbe:
        calls = 0

        async def __call__(self) -> SealabilityProbeResult:
            MustNotProbe.calls += 1
            raise AssertionError("forced terminal must not probe sealability")

    agent = ScriptedAgent(
        [action_step("notify_user", {"message": f"musing {i}"}) for i in range(8)]
    )
    loop, store = build_loop(
        agent,
        stuck_thresholds=StuckThresholds(agent_monologue=100),
        finish_sealability_probe=MustNotProbe(),
    )
    await loop.send_message("go")
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    assert any(isinstance(e, StatusEvent) and e.detail == "noop_limit" for e in events)
    assert MustNotProbe.calls == 0


async def test_notify_completion_path_is_gated_and_recoverable():
    """The completed-via-notify path (W-32 twin) clears the same seal gate.

    A notify-signaled "done" over blocking content is REFUSED (no FINISHED),
    the model acts on the remedy, and the run then finishes sealed via an
    explicit finish."""
    probe = ProbeFake(_blocking(), _sealable())
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {"command": "echo build the site"}),
            action_step("plan_step", {"index": 1, "state": "done"}),
            action_step("notify_user", {"message": "All files are in place, site built."}),
            action_step("notify_user", {"message": "The static site is fully built."}),
            action_step("notify_user", {"message": "All set! The build is complete."}),
            action_step("shell", {"command": "rm dist && cp -r steer-dashboard/dist dist"}),
            finish_step("links replaced"),
        ]
    )
    loop, store = build_loop(agent)
    loop._finish_sealability_probe = probe
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()  # halts at plan approval
    await loop.approve_plan()
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    refusals = _refusals(events)
    assert refusals, "the notify-completion attempt must hit the seal gate"
    finished = _finished_seqs(events)
    assert len(finished) == 1
    assert all(r.seq < finished[0] for r in refusals)
    # The refused notify attempt did NOT land completed_via_notify FINISHED.
    assert not any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.FINISHED
        and e.detail == "completed_via_notify"
        for e in events
    )
