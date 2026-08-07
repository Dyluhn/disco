"""Live-preview detection, driven browser probes, and honest-unverifiable finish.

Owns: resolving the URL the deliverable currently serves on (backend-aware,
never the agent-server's own port), driving one structured `verify_web_app`/
`verify_appkit_app` call when the agent declares done without a fresh
verdict, the CXT-7 verifier-failure context write, and the Bug 6 "delivered
but this backend cannot run a browser" honest-finish paths (both the
finish-gate and actionless-valve twins).
"""

from __future__ import annotations

from typing import Any, cast

from ... import signals
from ..common import (
    _LOG,
    _PREVIEW_PORTS,
    ActionEvent,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    _browser_unavailable_observed,
    _missing_steps_all_verify,
    _nonbrowser_static_validation_passed,
    _real_web_failure_evidence,
    active_managed_preview_ports,
    backend_shares_host_network,
    parse_port_ownership,
    port_ownership_probe_command,
    resolve_preview_port,
)
from .probe_events import latest_event_for_action


async def detect_preview_url(gate: Any) -> str | None:
    """P1-1 — detect the URL the live deliverable currently serves on, using the
    SAME backend-aware resolver the `verify_web_app` tool uses
    (`preview_target.resolve_preview_port`). Duck-typed over the executor's
    sandbox (`exec_shell`): core never imports `tools`, so the gate runs the
    shared in-sandbox ownership probe and feeds the result to the shared
    resolver.

    On a SHARED-host backend (process/local — `sandbox.workspace_path` is set)
    the resolver NEVER returns a reserved control port (8000 = the agent-server):
    it prefers a CONVERSATION-OWNED served port, else any non-reserved owned
    port, else None — so a stale verdict about `:8000` (the agent-server, Bug 7)
    can never bind the finish gate. On an ISOLATED backend (gVisor/Podman —
    `workspace_path` is None) `:8000` IS the app, so the legacy first-reachable
    socket probe is kept.

    Returns `http://127.0.0.1:<port>/` or None (no sandbox / no exec_shell /
    nothing detected), in which case binding is NOT enforced (see
    `_verdict_targets_preview`) and the gate drives a fresh verify against the
    real preview. Never raises (a detection failure must not wedge the gate).
    """
    sbx = getattr(gate._loop.executor, "sandbox", None)
    if sbx is None or not hasattr(sbx, "exec_shell"):
        return None
    preview_ports = tuple(dict.fromkeys((*_PREVIEW_PORTS, *active_managed_preview_ports(sbx))))
    host_shared = backend_shares_host_network(sbx)
    if host_shared:
        # Process backend (shares host net): ownership-aware — never bind the
        # agent-server's 8000. Isolated containers fall to the branch below where
        # 8000 IS the app (the old `workspace_path` heuristic wrongly sent them here).
        try:
            res = await sbx.exec_shell(port_ownership_probe_command(preview_ports), timeout_s=10)
        except Exception:  # noqa: BLE001 — detection failure → no binding (degrade safe)
            return None
        owned = parse_port_ownership(str(getattr(res, "stdout", "") or ""))
        port = resolve_preview_port(
            host_shared=True,
            owned=owned,
            conversation_id=str(getattr(sbx, "conversation_id", "") or ""),
            preview_ports=preview_ports,
        )
        return f"http://127.0.0.1:{port}/" if port is not None else None

    # Isolated backend: 8000 is the app inside the box — first reachable wins.
    import shlex

    ports = list(preview_ports)
    script = (
        "import socket,sys\n"
        f"for p in {ports!r}:\n"
        "    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
        "    s.settimeout(0.3)\n"
        "    try:\n"
        "        s.connect(('127.0.0.1',p))\n"
        "        print(p)\n"
        "        sys.exit(0)\n"
        "    except Exception:\n"
        "        pass\n"
        "    finally:\n"
        "        s.close()\n"
    )
    try:
        res = await sbx.exec_shell(f"python3 -c {shlex.quote(script)}", timeout_s=10)
    except Exception:  # noqa: BLE001 — detection failure → no binding (degrade safe)
        return None
    for line in str(getattr(res, "stdout", "") or "").splitlines():
        line = line.strip()
        if line.isdigit():
            return f"http://127.0.0.1:{int(line)}/"
    return None


async def drive_verify_web_app(
    gate: Any,
    target_url: str | None = None,
    tool_name: str = "verify_web_app",
    *,
    medium: str = "web",
) -> bool:
    """W-45 ACTIVE verify: DRIVE one structured verifier call when the agent
    declares done without a fresh verdict. Mirrors `_drive_finish_browser_probe`
    — the probe ActionEvent is tagged `verify_probe` so it never counts as
    agent work. Returns True iff the call produced a usable observation.

    When the gate has already resolved the real preview target (`target_url`,
    backend-aware — never the agent-server's 8000 on the process backend), it is
    passed EXPLICITLY so the tool verifies the build's actual served port instead
    of repeating its own auto-detect (Bug 7). None ⇒ `{}` ⇒ the tool auto-detects
    (which itself uses the same backend-aware resolver).
    """
    arguments: dict[str, str] = {}
    if target_url:
        arguments["url"] = target_url
    if medium != "web" and tool_name == "verify_web_app":
        arguments["medium"] = medium
    call = ToolCall(tool_name=tool_name, arguments=arguments)
    action = ActionEvent(
        thought=f"Verifying the app: running {tool_name} on the running preview.",
        tool_call=call,
        meta={"verify_probe": True},
    )
    if signals.hard_deny_reason(action) is not None:
        return False
    action = cast(ActionEvent, await gate._loop._emit(action))
    await gate._loop._execute_and_observe(action)
    obs = await latest_event_for_action(gate._loop, action.id)
    return isinstance(obs, ObservationEvent) and obs.tool_result.success


def browser_verification_unavailable(gate: Any) -> bool:
    """True when this backend cannot run browser-based verification (the process
    backend ships no `browser` tool). Mirrors `_drive_finish_browser_probe`'s
    availability check — the precise "the render/console check cannot run here"
    signal that distinguishes an UNVERIFIABLE delivery from a BROKEN app.
    """
    try:
        tool_names = {getattr(t, "name", None) for t in gate._loop.executor.available_tools()}
    except Exception:  # noqa: BLE001 — introspection failure → assume available (cautious)
        return False
    return "browser" not in tool_names


async def static_deliverable_present(gate: Any) -> bool:
    """True when the static web deliverable (index.html) exists on disk in the
    sandbox workspace — the file-truth half of the honest unverifiable finish (a
    zero-action run wrote nothing, so this is False and it cannot finish).
    """
    sbx = getattr(gate._loop.executor, "sandbox", None)
    if sbx is None or not hasattr(sbx, "file_exists"):
        return False
    try:
        return bool(await sbx.file_exists("index.html"))
    except Exception:  # noqa: BLE001 — existence probe failure → cannot confirm
        return False


async def record_verifier_failure_to_context(
    gate: Any, *, message: str, rel_path: str | None
) -> None:
    """CXT-7 — persist a verify_web_app failure to .disco/context/verifier_failures.json
    (best-effort; never alters the gate flow). Survives truncation/resume so the
    CXT-4 assembler can surface the unresolved failure to the model later.
    """
    sbx = getattr(gate._loop.executor, "sandbox", None)
    if sbx is None:
        return
    try:
        from ....context import ArtifactMemoryStore, Severity, VerifierFailureRef

        await ArtifactMemoryStore(sbx).record_verifier_failures(
            (
                VerifierFailureRef(
                    kind="verify_web_app",
                    message=(message or "verify_web_app did not pass")[:500],
                    rel_path=rel_path or None,
                    severity=Severity.ERROR,
                ),
            )
        )
    except Exception:
        _LOG.warning(
            "CXT-7 verifier-failure context write failed for %s",
            gate._loop.conversation_id,
            exc_info=True,
        )


async def maybe_honest_unverifiable_static_finish(gate: Any, verdict: dict) -> Disp | None:
    """Bug 6 — when a web build's verify FAILS ONLY because nothing is serving
    (no console/network errors, no blank-render judgement: the browser never ran)
    AND this backend cannot run a headless browser AND index.html exists on disk,
    return FALLTHROUGH with an explicit honest-unverifiable marker so a delivered
    static build FINISHES instead of pausing/STUCKing. Returns None (let the
    normal refuse/loop-break path run) for every other case — a real fail
    (console/network/blank) or a missing deliverable is NEVER converted to a
    success (W-45 preserved).
    """
    http_ok = 200 <= int(verdict.get("http_status") or 0) < 400
    not_serving = (
        str(verdict.get("verdict")) == "fail"
        and not (verdict.get("console_errors") or [])
        and not (verdict.get("network_failures") or [])
        and not http_ok
    )
    if not not_serving:
        return None
    if not browser_verification_unavailable(gate):
        return None
    if not await static_deliverable_present(gate):
        return None
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="unverifiable_static_finish",
        )
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "⚠ Finished WITHOUT a live browser verification — the static "
                    "deliverable (index.html) exists but no preview server is "
                    "reachable and this backend cannot run a headless browser, so "
                    "the render could not be checked here. The verifier verdict on "
                    f"record is {str(verdict.get('verdict') or 'none')!r}"
                    f"{': ' + str(verdict.get('summary')) if verdict.get('summary') else ''}. "
                    "The files are delivered; note clearly in your summary that the "
                    "build is UNVERIFIED."
                ),
            ),
        )
    )
    gate._loop._browser_verify_refusals = 0
    return Disp.FALLTHROUGH


async def maybe_honest_unverifiable_static_actionless_finish(
    gate: Any, events: list[Event]
) -> bool:
    """Bug 6 — the ACTIONLESS-VALVE twin of `maybe_honest_unverifiable_static_finish`.

    The finish-gate honest path only runs when the model REACHES the finish gate.
    On a browserless backend with a final browser-verify plan step the model never
    does — it churns on the unsatisfiable step and the actionless valve would PAUSE
    a substantively-complete build. This applies the SAME honest-finish concept at
    the valve: when (and ONLY when) the conservative conditions below all hold, emit
    the honest marker + a clean terminal FINISHED and return True; otherwise return
    False so the valve keeps its existing pause/stuck behavior.

    Conservative conditions (ALL required — any failure ⇒ False ⇒ no honest finish):
      1. a plan exists, is INCOMPLETE, and EVERY not-done step is verify-only;
      2. real productive work happened since approval (APPROVE_PLAN_NO_EXECUTION —
         a zero-action run can never honest-finish here);
      3. the static deliverable (index.html) exists on disk;
      4. a NON-browser validation PASSED after the last write/edit (a failed or
         absent validation blocks);
      5. browser verification is GENUINELY unavailable (no browser tool, OR a
         browser observation/error carried the unavailable signal);
      6. NO real web-failure evidence (console/network errors or a served-but-blank
         render) — W-45: a genuinely BROKEN app is never converted to a success.
    """
    if not _missing_steps_all_verify(events):
        return False
    if signals.productive_actions_since_approval(events) <= 0:
        return False
    if not await static_deliverable_present(gate):
        return False
    if not _nonbrowser_static_validation_passed(events):
        return False
    if not (browser_verification_unavailable(gate) or _browser_unavailable_observed(events)):
        return False
    if _real_web_failure_evidence(events):
        return False
    # REL-27 — the honest static finish is still an affirmative delivery of
    # workspace files, so it must clear the finish-time sealability gate. On
    # refusal (exact-paths reminder already emitted) return False: the valve
    # keeps its existing pause/stuck behavior and never lands a FINISHED
    # whose deliverable cannot be sealed.
    if not await gate._coordinator.verification.seal_gate_allows_finish():
        return False
    # All guards hold — finish honestly instead of pausing actionless. Same honest
    # marker as the finish-gate path, then a clean terminal FINISHED (NOT PAUSED).
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="unverifiable_static_finish",
        )
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "⚠ Finished WITHOUT a live browser verification — the static "
                    "deliverable (index.html) exists and a non-browser validation "
                    "passed, but this backend cannot run a headless browser and no "
                    "preview server is reachable, so the only remaining plan step "
                    "(browser verification) could not run here. This run reached "
                    f"the honest-unverifiable finish after {signals.actionless_pause_count_current_execution_segment(events)} "
                    "actionless pause(s). The files are "
                    "delivered; note clearly in your summary that the render is "
                    "UNVERIFIED."
                ),
            ),
        )
    )
    await gate._loop._emit(StatusEvent(status=ConversationStatus.FINISHED))
    gate._loop._browser_verify_refusals = 0
    return True


async def verifier_unavailable_disposition(gate: Any, tool_name: str = "verify_web_app") -> Disp:
    """P1-2 — disposition when the structured verifier is advertised but produced NO
    usable verdict (verifier execution error / empty / the driven verify
    failed). On the build/web surface a clean FINISH requires a real PASS
    verdict, so this must NOT fall through to finalization (the W-32 regression
    codex found). Refuse-and-continue with a "verification could not run"
    reminder while under the cap; at the cap, release EXPLICITLY as unverified
    (distinct status marker + visible message) rather than a silent clean
    finish. Bounded by the shared `_browser_verify_refusals` cap so a verifier
    that can never run still terminates.
    """
    if gate._loop._browser_verify_refusals < 3:
        gate._loop._browser_verify_refusals += 1
        await gate._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        f"verification could not run: {tool_name} did not return a "
                        "usable verdict (the verifier failed to execute, or the preview "
                        "server is not reachable on its port). The build is NOT verified "
                        "— start/repair the dev server on the preview port, then finish "
                        "again and it will re-verify."
                    ),
                ),
            )
        )
        return Disp.CONTINUE
    # Cap reached — bounded release, but EXPLICITLY unverified (never a clean
    # done): distinct terminal marker + visible message, mirroring the FAIL
    # release above.
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="unverified_release",
        )
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    f"⚠ Finished WITHOUT a {tool_name} verdict — the verifier could "
                    "not run after 3 attempts, so the deliverable is UNVERIFIED and may "
                    "be INCOMPLETE. Note this clearly in your summary."
                ),
            ),
        )
    )
    gate._loop._browser_verify_refusals = 0
    return Disp.FALLTHROUGH
