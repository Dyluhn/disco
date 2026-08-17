"""The verdict-consuming browser-verify gate sequence.

Owns: `gate_verify_web_app` (W-45 — consume the latest structured verifier
verdict since the last productive edit), whether a governed/legacy host
verdict already covers this finish so the inline browser gate can defer to
it, and `gate_browser_verify` — the top-level dispatcher that arms the whole
sequence only for a web-shaped deliverable.
"""

from __future__ import annotations

from typing import Any

from ....verification import AdmittedVerificationContract
from ..common import (
    _VERIFY_MARKER_PREFIX,
    AgentStep,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    HostVerificationDeliverable,
    LLMMessage,
    MessageEvent,
    OperatingMode,
    StatusEvent,
    VerifierStartedEvent,
    _browser_content_meaningful,
    _browser_verified,
    _is_web_deliverable,
    _last_verification_authority_seq,
    _latest_app_deliverable_event,
    _latest_browser_error,
    _latest_browser_screenshot,
    _latest_browser_structured,
    _latest_deliverable_event,
    _latest_host_verifier_event,
    _latest_verify_verdict,
    _preview_key,
    _prior_verify_marker_fp,
    _tc_components_installed,
    _vision_mode,
)
from .appkit_typed import appkit_typed_gate_disposition
from .browser_probe import drive_verify_web_app, verifier_unavailable_disposition
from .host_claims import (
    governed_structured_browser_target,
    governed_verification_contract,
    governed_verification_required,
    latest_matching_appkit_start,
    strict_appkit_contract,
)
from .semantic_claims import finish_verification_medium, verification_failure_details


def _gate_verify_web_app_since_seq(
    events: list[Event], contract: AdmittedVerificationContract | None, governed_appkit: bool
) -> int:
    since_seq = _last_verification_authority_seq(events)
    if not (governed_appkit and contract is not None):
        return since_seq
    started = latest_matching_appkit_start(events, contract)
    if started is not None and type(started.seq) is int:
        return max(since_seq, started.seq)
    return since_seq


async def _gate_verify_web_app_verdict(
    gate: Any,
    events: list[Event],
    *,
    since_seq: int,
    target_url: str | None,
    tool_name: str,
    medium: str,
) -> tuple[dict[str, Any] | None, list[Event]]:
    # P1-1: bind the accepted verdict to the CURRENT preview target. Detect the
    # live preview (same _PREVIEW_PORTS detection the tool uses) and accept only
    # a verdict whose url matches it; a stale / foreign-port (or url-less) PASS
    # must NOT satisfy the gate — it drives a fresh verify against the real
    # preview instead. target_url=None (preview undetectable) disables binding.
    verdict = _latest_verify_verdict(events, since_seq, target_url, tool_name)
    if verdict is not None:
        return verdict, events
    # No fresh verdict bound to the current preview — the agent may have
    # overclaimed, or only a stale/foreign-url verdict exists. Drive ONE
    # against the resolved real preview (target_url is backend-aware — never
    # the agent-server's 8000 on the process backend, Bug 7).
    if not await drive_verify_web_app(gate, target_url, tool_name, medium=medium):
        return None, events
    events = await gate._loop._events()
    # The freshly driven verify auto-detected + tested the CURRENT
    # preview, so its verdict IS bound by construction — read it
    # unconditionally (target_url=None) rather than re-binding against a
    # detection that could disagree with the tool's own auto-detect.
    verdict = _latest_verify_verdict(events, since_seq, target_url=None, tool_name=tool_name)
    return verdict, events


async def _gate_verify_web_app_unavailable_disposition(
    gate: Any,
    *,
    events: list[Event],
    governed_appkit: bool,
    tool_name: str,
    appkit_prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable] | None,
) -> Disp:
    # A missing ordinary verdict terminalizes as an explicit unverified release.
    # Governed AppKit remains fail-closed through its typed contract path.
    if not governed_appkit:
        return await verifier_unavailable_disposition(gate, tool_name)
    current_events = await gate._loop._events()
    typed_disp = await appkit_typed_gate_disposition(
        gate,
        events=current_events,
        verdict=None,
        tool_name=tool_name,
        prepared=appkit_prepared,
    )
    if typed_disp is not None:
        return typed_disp
    return await gate._governed_contract_refusal(
        events,
        failure_key=f"{tool_name}:unavailable",
        guidance=(
            f"{tool_name} did not return a usable strict target verdict. "
            "Repair the target verifier/runtime; unavailable verification "
            "cannot release this governed build."
        ),
    )


async def _gate_verify_web_app_typed_disposition(
    gate: Any,
    *,
    events: list[Event],
    verdict: dict[str, Any] | None,
    tool_name: str,
    appkit_prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable] | None,
) -> Disp | None:
    current_events = await gate._loop._events()
    return await appkit_typed_gate_disposition(
        gate,
        events=current_events,
        verdict=verdict,
        tool_name=tool_name,
        prepared=appkit_prepared,
    )


async def _gate_verify_web_app_vision_artifact(gate: Any, verdict: dict[str, Any]) -> None:
    # W6 vision artifact: when the browser observation includes a
    # screenshot, emit a StatusEvent so the UI / post-run harness can
    # find it. Only emitted when vision mode is active.
    if not _vision_mode():
        return
    shot = str(verdict.get("screenshot_path") or "")
    if not shot:
        return
    await gate._loop._emit(
        StatusEvent(status=ConversationStatus.RUNNING, detail=f"vision_artifact:{shot}")
    )


async def _gate_verify_web_app_failure_disposition(
    gate: Any,
    *,
    events: list[Event],
    verdict: dict[str, Any],
    tool_name: str,
    since_seq: int,
    governed_appkit: bool,
) -> Disp:
    # FAIL / DEGRADED. Build the concrete next-step payload from the verdict.
    fp, summary, next_action, screenshot, first_error = verification_failure_details(
        verdict, tool_name
    )

    # CXT-7: record the failure to durable context (.disco/context/verifier_failures.json),
    # best-effort — NEVER alters the gate verdict/flow. CXT-4's assembler surfaces these
    # unresolved failures into the model's ContextPack on later turns/resume.
    await gate._record_verifier_failure_to_context(
        message=(first_error or summary), rel_path=(screenshot or None)
    )

    if not governed_appkit:
        await gate._loop._emit(
            StatusEvent(status=ConversationStatus.RUNNING, detail="unverified_release")
        )
        warning = (
            f"⚠ Finished WITHOUT a passing {tool_name} verdict — the deliverable "
            f"is UNVERIFIED and may be INCOMPLETE. {summary}"
            + (f" First failure: {first_error}" if first_error else "")
            + (f" Outstanding: {next_action}" if next_action else "")
            + " Note this clearly in your summary."
        )
        await gate._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=warning),
            )
        )
        gate._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    prior_fp = _prior_verify_marker_fp(events, since_seq)
    if prior_fp is not None and prior_fp == fp:
        # LOOP BREAKER: the SAME failure verdict has recurred since the last
        # productive edit and the gate already nudged for it — no new
        # information. Halt STUCK (named) instead of re-loading forever.
        await gate._loop._land_blocked(
            reason=f"{_VERIFY_MARKER_PREFIX}{fp}",
            guidance=(
                f"{tool_name} kept returning the same failure with no "
                f"progress since the last edit. {summary}"
                + (f" Error: {first_error}" if first_error else "")
                + (f" Next: {next_action}" if next_action else "")
            ),
            legacy_status=ConversationStatus.STUCK,
            legacy_detail=f"{_VERIFY_MARKER_PREFIX}{fp}",
        )
        return Disp.HALT

    gate._loop._browser_verify_refusals += 1
    payload = (
        f"{tool_name} did not pass ({verdict.get('verdict')}). {summary}\n"
        + (f"first error: {first_error}\n" if first_error else "")
        + (f"screenshot: {screenshot}\n" if screenshot else "")
        + (f"next step: {next_action}" if next_action else "Fix the issue, then finish.")
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=payload),
        )
    )
    # Stamp the fingerprint so a repeat WITHOUT a productive edit trips the
    # governed loop breaker above on the next finish attempt.
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail=f"{_VERIFY_MARKER_PREFIX}{fp}",
        )
    )
    return Disp.CONTINUE


async def gate_verify_web_app(
    gate: Any,
    events: list[Event],
    tool_name: str = "verify_web_app",
    *,
    step: AgentStep | None = None,
    appkit_prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable] | None = None,
) -> Disp:
    """Consume one current structured-verifier verdict at finish.

    Ordinary builds pass cleanly or terminalize with an explicit unverified
    result; they never reopen the builder. Governed AppKit targets retain the
    progress-sensitive refusal and repeated-failure halt required by their
    admitted contract.
    """
    contract = governed_verification_contract(events)
    governed_appkit = bool(
        strict_appkit_contract(contract) and governed_verification_required(events)
    )
    since_seq = _gate_verify_web_app_since_seq(events, contract, governed_appkit)
    target_url = await gate._detect_preview_url()
    medium = await finish_verification_medium(gate, step, events)
    verdict, events = await _gate_verify_web_app_verdict(
        gate,
        events,
        since_seq=since_seq,
        target_url=target_url,
        tool_name=tool_name,
        medium=medium,
    )
    if verdict is None:
        return await _gate_verify_web_app_unavailable_disposition(
            gate,
            events=events,
            governed_appkit=governed_appkit,
            tool_name=tool_name,
            appkit_prepared=appkit_prepared,
        )

    if governed_appkit:
        typed_disp = await _gate_verify_web_app_typed_disposition(
            gate,
            events=events,
            verdict=verdict,
            tool_name=tool_name,
            appkit_prepared=appkit_prepared,
        )
        if typed_disp is not None:
            return typed_disp

    if verdict.get("passed") is True:
        gate._loop._browser_verify_refusals = 0  # clean pass → reset the streak
        await _gate_verify_web_app_vision_artifact(gate, verdict)
        return Disp.FALLTHROUGH

    # Bug 6 — HONEST unverifiable finish for a delivered-but-unverifiable static
    # build. If the ONLY failure is "not serving" (server unreachable; the
    # browser never even ran ⇒ no console/network errors and no blank-render
    # judgement) AND this backend cannot run a headless browser AND the static
    # deliverable file exists on disk, then the build is UNVERIFIABLE (infra),
    # not BROKEN — finish honestly with an explicit marker instead of refusing →
    # STUCK. A REAL fail (console errors, network failures, blank render, or a
    # MISSING deliverable) never reaches here, so W-45 is not weakened. This runs
    # only after `gate_execution_nudge` (so an approved-but-unexecuted plan still
    # STUCKs there) and requires index.html on disk (so a zero-action run cannot
    # finish).
    honest = await gate._maybe_honest_unverifiable_static_finish(verdict)
    if honest is not None:
        return honest

    return await _gate_verify_web_app_failure_disposition(
        gate,
        events=events,
        verdict=verdict,
        tool_name=tool_name,
        since_seq=since_seq,
        governed_appkit=governed_appkit,
    )


def _delegated_to_host_governed_check(
    gate: Any, contract: AdmittedVerificationContract, events: list[Event]
) -> bool:
    handoff = _latest_deliverable_event(events)
    expected_key = (
        contract.digest,
        handoff.id if handoff is not None else "",
        _last_verification_authority_seq(events),
    )
    return getattr(gate._loop, "_governed_host_pass_key", None) == expected_key


def _delegated_to_host_unavailable_released(events: list[Event], latest: Any) -> bool:
    return any(
        isinstance(event, StatusEvent)
        and event.detail == "unverified_release"
        and (event.seq or 0) > (latest.seq or 0)
        for event in events
    )


def _delegated_to_host_legacy_check(
    deliverable: HostVerificationDeliverable, events: list[Event]
) -> bool:
    if deliverable.artifact_kind != "app":
        return False
    latest = _latest_host_verifier_event(events, _last_verification_authority_seq(events))
    if latest is None or latest.verification_result is None:
        return False
    result = latest.verification_result
    if (
        latest.artifact_path != deliverable.artifact_path
        or latest.artifact_kind != deliverable.artifact_kind
        or not result.is_current_for(deliverable, observed_url=result.observed_url)
    ):
        return False
    if latest.verdict in ("pass", "fail"):
        return True
    if latest.verdict == "unavailable":
        return _delegated_to_host_unavailable_released(events, latest)
    return False


async def browser_verify_delegated_to_host(gate: Any, step: AgentStep, events: list[Event]) -> bool:
    contract = governed_verification_contract(events)
    if strict_appkit_contract(contract):
        return False
    governed = governed_verification_required(events)
    if not (gate._host_verify_authoritative() or governed):
        return False
    deliverable = await gate._host_verify_deliverable(step, events)
    if deliverable is None:
        return False
    # A contractless host-unavailable verdict is terminal evidence when paired
    # with unverified_release, even if the verifier implementation itself was
    # absent. Do not follow that result with an inline repair loop.
    if contract is None:
        return _delegated_to_host_legacy_check(deliverable, events)
    if getattr(gate._loop, "_host_verifier", None) is None:
        return False
    return _delegated_to_host_governed_check(gate, contract, events)


def _gate_browser_verify_is_appkit(
    gate: Any, contract: AdmittedVerificationContract | None, verify_tool: str | None
) -> bool:
    # Strict AppKit mode is detected from the EXECUTOR (duck-typed phase
    # attribute), not only the advertised tool set: before a successful
    # app_create the phase allowlist hides verify_appkit_app, and a finish
    # in that window must still be gated (a zero-work appkit FINISH slipped
    # through here, live 2026-07-03).
    if contract is not None:
        return strict_appkit_contract(contract)
    return (
        verify_tool == "verify_appkit_app"
        or getattr(gate._loop.executor, "appkit_phase", None) is not None
    )


def _gate_browser_verify_armed(
    gate: Any, events: list[Event], *, is_appkit: bool, tc_installed: bool
) -> bool:
    # Governed structured-browser target with an app handoff: mandatory target
    # claims (HTTP ready, rendered content, console/network clean, …) require a
    # structured browser runtime. The legacy ``_is_web_deliverable`` detector
    # recognizes index.html writes, preview_start pairs, and server_status
    # records — it does NOT recognize a governed ``app`` handoff for a non-
    # index entry (e.g. ``app.py`` after cancel/recovery). Without this
    # disjunct the inline gate never arms and a host-unavailable verdict falls
    # through to FINISHED with zero claim enforcement. This is claim-driven
    # (target-owned), not filename/framework-specific: any governed admission
    # whose mandatory claims require a browser runtime arms the gate when an
    # app handoff exists, independently of the legacy detector.
    governed_browser_target = (
        governed_structured_browser_target(events)
        and not is_appkit
        and _latest_app_deliverable_event(events) is not None
    )
    if gate._loop.mode == OperatingMode.PLANNING:
        return False
    # WO-TC3: a build that installed trusted components must pass through this
    # gate even without a web-deliverable marker — the component integrity/deps/
    # probe checks ride the verify_web_app verdict.
    return bool(
        tc_installed
        or governed_browser_target
        or (gate._loop._planning_tools and (is_appkit or _is_web_deliverable(events)))
    )


async def _gate_browser_verify_probe_result(
    gate: Any,
    events: list[Event],
    *,
    since_seq: int,
    target_url: str | None,
    target_key: tuple[str, int] | None,
) -> tuple[bool, list[Event]]:
    ok, _ = _browser_verified(events, since_seq, target_key)
    if ok:
        return True, events
    # ACTIVE verify (verification-overclaim fix): the AGENT has NOT
    # produced a clean preview browser observation since the last edit.
    # Rather than wait/trust it to browse (it may have overclaimed and
    # never looked), DRIVE the browse ourselves (against the resolved
    # preview, not a dead :8000) and judge the probe on ground truth —
    # zero console errors AND a non-blank render (a page can serve 200
    # with a clean console yet mount nothing). Degrades to the prior
    # explicit unverified release on browserless backends (the probe is a
    # no-op there).
    if not await gate._drive_finish_browser_probe(target_url):
        return False, events
    events = await gate._loop._events()
    probe = _latest_browser_structured(events, target_key)
    if probe is None:
        return False, events
    probe_console_clean = not any(c.get("level") == "error" for c in probe.get("console", []))
    return probe_console_clean and _browser_content_meaningful(probe), events


async def _gate_browser_verify_ok_disposition(
    gate: Any, events: list[Event], target_key: tuple[str, int] | None
) -> None:
    gate._loop._browser_verify_refusals = 0  # reset on clean pass
    if not _vision_mode():
        return
    shot = _latest_browser_screenshot(events, target_key)
    if shot:
        await gate._loop._emit(
            StatusEvent(status=ConversationStatus.RUNNING, detail=f"vision_artifact:{shot}")
        )


async def _gate_browser_verify_active_check(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    *,
    verify_tool: str | None,
    appkit_prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable] | None,
) -> Disp:
    # W-45: when the structured `verify_web_app` tool is in the execution
    # set (the build surface), consume its VERDICT as the primary check —
    # the clean actionable signal that kills the reload loop. The legacy
    # raw-observation path below stays for browserless backends / tests
    # without the tool (non-web + assist paths are untouched: this whole
    # block is gated on _is_web_deliverable / strict AppKit mode).
    if await browser_verify_delegated_to_host(gate, step, events):
        return Disp.FALLTHROUGH
    if verify_tool is not None:
        return await gate._gate_verify_web_app(
            events,
            verify_tool,
            step=step,
            appkit_prepared=appkit_prepared,
        )
    since_seq = _last_verification_authority_seq(events)
    # The preview platform assigns a RANDOM port — there is NO fixed :8000
    # inside the sandbox (a curl there 404s). Resolve the live preview the
    # SAME backend-aware way the verify_web_app gate does and bind every
    # browser-observation check + the driven probe + the nudge text to it.
    # None ⇒ undetectable (sandbox-less / legacy / isolated where :8000 IS
    # the app) ⇒ the readers fall back to the historical :8000 acceptance.
    target_url = await gate._detect_preview_url()
    target_key = _preview_key(target_url) if target_url else None
    ok, events = await _gate_browser_verify_probe_result(
        gate, events, since_seq=since_seq, target_url=target_url, target_key=target_key
    )
    # Messaging reads the FULL history: a post-browse edit
    # invalidates the verification but not what was seen.
    first_error = _latest_browser_error(events, target_key)
    if ok:
        await _gate_browser_verify_ok_disposition(gate, events, target_key)
        return Disp.FALLTHROUGH
    # This is the ordinary compatibility path. It gets one host-owned check,
    # then terminalizes honestly instead of reopening the builder.
    probe = _latest_browser_structured(events, target_key)
    if first_error:
        detail = f"Last console error: {first_error}."
    elif probe is not None:
        detail = "The browser check ran, but the page rendered no meaningful content."
    elif target_url:
        detail = f"Target {target_url} did not produce a clean, meaningful browser render."
    else:
        detail = "No clean, meaningful browser render was available."
    await gate._loop._emit(
        StatusEvent(status=ConversationStatus.RUNNING, detail="unverified_release")
    )
    warn_msg = (
        "⚠ Finished WITHOUT a clean browser verification — the deliverable "
        f"is UNVERIFIED and may be INCOMPLETE. {detail}"
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=warn_msg),
        )
    )
    gate._loop._browser_verify_refusals = 0
    return Disp.FALLTHROUGH


async def gate_browser_verify(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    *,
    appkit_prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable] | None = None,
) -> Disp:
    # BROWSER-VERIFY GATE — §BP-05. If web deliverable holds, refuse finish
    # until a clean browser observation (zero console errors) exists
    # since the last state-changing edit.
    verify_tool = gate._active_verify_tool()
    contract = governed_verification_contract(events)
    is_appkit = _gate_browser_verify_is_appkit(gate, contract, verify_tool)
    tc_installed = _tc_components_installed(events)
    if _gate_browser_verify_armed(gate, events, is_appkit=is_appkit, tc_installed=tc_installed):
        return await _gate_browser_verify_active_check(
            gate, step, events, verify_tool=verify_tool, appkit_prepared=appkit_prepared
        )
    return Disp.FALLTHROUGH
