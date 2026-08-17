"""Bounded Build Soak cleanup owner."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from typing import Any, cast

from ..adapters.disco_api import (
    TERMINAL_STATES,
    DiscoApiClient,
)
from ..events import NormalizationError, normalize_events
from ..ports import ProductClient
from ..provider_ledger import parse_relay_log, record_applies_to_conversation
from .bindings import CleanupBindings
from .temporal import (
    _min_event_epoch,
    _terminal_status_epoch,
)
from .thrash import (
    _confirmed_live_thrash_stop,
)


async def _release_conversation(
    client: ProductClient, cid: str | None, timeline: list[str] | None = None
) -> None:
    """Tear down the conversation the runner is DONE with — INCLUDING its sandbox + egress
    sidecar CONTAINERS — so nothing leaks between runs. Called from run_once's `finally` AFTER
    evidence has been collected + frozen (the §6 read of the terminal events already happened in
    drive_scenario), so the release never races the dossier.

    [REL-4] We release the conversation even when it is already TERMINAL. MEASURED on the
    production podman backend: a single FINISHED build leaves TWO live containers — the sandbox
    `disco-sbx-sbx_<id>` and the egress sidecar `disco-egr-sbx_<id>` — both still "Up" after the
    terminal event (the sandbox is not destroyed at terminal; it lingers to the idle-TTL). Across
    a 95-run soak that is ~190 orphan containers and FAILS the 0-orphans acceptance. `POST /kill`
    on the terminal conversation tears down BOTH containers (measured: 2 → 0), revokes the provider
    token, and stops the preview, and is IDEMPOTENT + BEST-EFFORT (any error — server gone, already
    torn down — is swallowed so teardown never turns a real verdict into a crash). The FINISHED
    workspace snapshot is already taken (kick post-run) and evidence is frozen, so releasing here
    is safe."""
    if not cid:
        return
    # Every exit path, including cancellation and evidence-collection errors,
    # reaches this fallback.  The idempotent owned inspect finalizer is ordered
    # against the kill by the conversation's ACTIVITY, not by convenience: a
    # terminal conversation can emit no further trace events, so inspect is
    # frozen BEFORE the cleanup kill can release the runtime that owns the
    # bounded source ring; an ACTIVE/unknown conversation is stopped FIRST so
    # the final sample can observe any tail events the still-running model
    # emitted while the kill landed, and an unconfirmed stop taints continuity
    # instead of silently claiming losslessness.
    status = ""
    with contextlib.suppress(Exception):
        status = DiscoApiClient._status_of(await client.get_state(cid))
    if status in TERMINAL_STATES:
        with contextlib.suppress(Exception):
            await client.finish_inspect_collection(cid)
        # Release even though the run is TERMINAL — the orphan-container teardown.
        # kill is idempotent + suppressed so this can never crash a real verdict.
        with contextlib.suppress(Exception):
            resp = await client.kill(cid)
            if timeline is not None:
                timeline.append(
                    f"released terminal conversation {cid} — sandbox + sidecar containers "
                    f"destroyed (was {status or 'unknown'}, http {resp.get('http_status')})"
                )
        return
    kill_confirmed = False
    with contextlib.suppress(Exception):
        resp = await client.kill(cid)
        kill_confirmed = (
            200 <= int(resp.get("http_status", 0)) < 300
            and resp.get("killed") is True
            and DiscoApiClient._status_of(resp.get("state") or {}) == "IDLE"
        )
        if timeline is not None:
            timeline.append(
                f"killed abandoned conversation {cid} — sandbox + sidecar containers "
                f"destroyed (was {status or 'unknown'}, http {resp.get('http_status')})"
            )
    if not kill_confirmed:
        with contextlib.suppress(Exception):
            client.note_inspect_stop_unconfirmed(cid)
    with contextlib.suppress(Exception):
        await client.finish_inspect_collection(cid)


_DISCO_CONTAINER_PREFIXES = ("disco-sbx-", "disco-egr-")


_DISCO_VOLUME_PREFIXES = ("disco-ws-", "pmx-ws-")


def _live_disco_container_names() -> list[str] | None:
    """RUNNING disco sandbox + egress-sidecar container names, or None if unmeasurable."""
    try:
        out = subprocess.run(
            ["podman", "ps", "--format", "{{.Names}}"],  # RUNNING only (no -a)
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    names: list[str] = []
    for line in out.stdout.splitlines():
        for name in line.split(","):
            clean = name.strip()
            if clean.startswith(_DISCO_CONTAINER_PREFIXES):
                names.append(clean)
    return names


def _live_disco_container_count() -> int | None:
    """[REL-5 / Lane A A-B5/M3] Count RUNNING disco sandbox + egress-sidecar containers via the
    podman CLI — the host-side orphan signal for the CleanupOracle on the LOCAL PODMAN iteration
    backend. Uses `podman ps` (RUNNING only) NOT `podman ps -a`: `-a` includes exited-but-not-yet-
    pruned containers, so a correctly-torn-down box still matched the prefix and inflated the count
    on either side of the baseline→after delta (a real leak could net to 0, or a prune could push
    after<baseline). Counting only RUNNING containers measures actual liveness. Returns None if
    podman is unavailable (count UNKNOWN, never faked 0). The gVisor FINAL REL-6 run needs the
    gVisor-equivalent probe (tracked)."""
    names = _live_disco_container_names()
    return None if names is None else len(names)


def _podman_volume_names(args: list[str]) -> list[str] | None:
    try:
        out = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    names: list[str] = []
    for line in out.stdout.splitlines():
        clean = line.strip()
        if clean:
            names.append(clean)
    return names


def _disco_volume_names() -> list[str] | None:
    """All disco workspace volumes, independent of whether their container is running."""
    names = _podman_volume_names(["podman", "volume", "ls", "--format", "{{.Name}}"])
    if names is None:
        return None
    return [name for name in names if name.startswith(_DISCO_VOLUME_PREFIXES)]


def _dangling_volume_names() -> set[str] | None:
    """Global dangling Podman volumes for unnamed-volume fallback accounting."""
    names = _podman_volume_names(
        ["podman", "volume", "ls", "--filter", "dangling=true", "--format", "{{.Name}}"]
    )
    return None if names is None else set(names)


def _scoped_disco_container_count(names: list[str], sandbox_instance_ids: list[str]) -> int:
    """Count live disco containers whose name embeds one of this conversation's sandbox ids."""
    ids = [sid for sid in sandbox_instance_ids if sid]
    return sum(1 for name in names if any(sid in name for sid in ids))


def _scoped_disco_volume_count(names: list[str], sandbox_instance_ids: list[str]) -> int:
    """Count disco workspace volumes whose name embeds one of this conversation's sandbox ids."""
    ids = [sid for sid in sandbox_instance_ids if sid]
    return sum(1 for name in names if any(sid in name for sid in ids))


def _new_dangling_volume_count(
    baseline: set[str] | None,
    after: set[str] | None,
    *,
    exclude_disco_named: bool,
) -> int | None:
    """Count volumes newly dangling since run start.

    Named disco workspace volumes are counted by sandbox id when possible; exclude
    them there to avoid double-counting the same leaked volume as both named and
    dangling.
    """
    if baseline is None or after is None:
        return None
    created = after - baseline
    if exclude_disco_named:
        created = {name for name in created if not name.startswith(_DISCO_VOLUME_PREFIXES)}
    return len(created)


def _extract_sandbox_instance_ids(*payloads: Any) -> list[str]:
    """Harvest sandbox ids from state/kill response shapes without depending on one version."""
    ids: list[str] = []

    def add(raw: Any) -> None:
        if isinstance(raw, str):
            sid = raw.strip()
            if sid and not sid.startswith("session-") and sid not in ids:
                ids.append(sid)
            return
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                add(item)

    def scan(value: Any, *, sandbox_context: bool = False) -> None:
        if not isinstance(value, dict):
            return
        add(value.get("sandbox_instance_id"))
        add(value.get("sandbox_instance_ids"))
        if sandbox_context:
            add(value.get("instance_id"))
            add(value.get("instance_ids"))
            add(value.get("id"))
        scan(value.get("state"))
        scan(value.get("extras"))
        scan(value.get("sandbox"), sandbox_context=True)
        # Frozen event payloads have changed their envelope across API
        # generations (tool_result, verifier selection, diagnostics, ...).
        # Walk the remaining mapping values so ownership extraction follows
        # the durable evidence rather than one event schema.
        for key, child in value.items():
            if key in {"state", "extras", "sandbox"}:
                continue
            scan(child, sandbox_context=key in {"sandbox", "sandbox_state"})

    for payload in payloads:
        scan(payload)
    return ids


def _add_lifecycle_evidence(run: Any, evidence: dict[str, Any]) -> None:
    """Add the frozen terminal/status slice when the terminal is readable."""
    terminal = ""
    with contextlib.suppress(Exception):
        terminal = DiscoApiClient._status_of(getattr(run, "state_final", {}) or {})
    if terminal:
        evidence["lifecycle"] = {
            "terminal": terminal,
            "statuses": list(getattr(run, "timeline", []) or []),
        }


def _cleanup_terminal_epoch(run: Any, events: list[dict[str, Any]]) -> float | None:
    """Select the exact product or diagnostic stop boundary for relay accounting."""
    if getattr(run, "diagnostic_stop", None) == "progressing_hard_cap":
        return getattr(run, "diagnostic_stop_epoch", None)
    return _terminal_status_epoch(
        events,
        allow_killed_idle=_confirmed_live_thrash_stop(run),
    )


def _relay_window_available(
    relay_log: str | None,
    terminal_epoch: float | None,
    run_start_epoch: float | None,
) -> bool:
    return bool(
        relay_log
        and os.path.exists(relay_log)
        and terminal_epoch is not None
        and run_start_epoch is not None
    )


async def _provider_calls_after_terminal(
    run: Any,
    cid: str,
    relay_log: str | None,
    timeline: list[str],
    grace_s: float,
) -> int | None:
    """Return tool-bearing relay calls after terminal when this run proves ledger liveness."""
    events = getattr(run, "events", []) or []
    terminal_epoch = _cleanup_terminal_epoch(run, events)
    run_start_epoch = _min_event_epoch(events)
    if not _relay_window_available(relay_log, terminal_epoch, run_start_epoch):
        return None
    relay_log = cast(str, relay_log)
    terminal_epoch = cast(float, terminal_epoch)
    run_start_epoch = cast(float, run_start_epoch)
    await asyncio.sleep(grace_s)
    try:
        with open(relay_log, encoding="utf-8") as relay:
            records = parse_relay_log(relay.read())
        timed_records = [
            (float(record["ts"]), bool(record.get("has_tools", True)))
            for record in records
            if isinstance(record.get("ts"), (int, float))
            and record_applies_to_conversation(record, cid)
        ]
    except Exception:
        return None
    calls_during_run = sum(
        1 for epoch, _has_tools in timed_records if run_start_epoch <= epoch <= terminal_epoch
    )
    if calls_during_run == 0:
        timeline.append(
            "REL-5: relay ledger captured 0 calls in this run's window — provider-after-"
            "terminal not adjudicable (relay not wired for this run)"
        )
        return None
    return sum(1 for epoch, has_tools in timed_records if epoch > terminal_epoch and has_tools)


async def _release_for_cleanup(
    client: ProductClient,
    cid: str,
    run: Any,
    timeline: list[str],
) -> tuple[dict[str, Any], bool]:
    """Reuse an already-proven stop or issue the single owned cleanup release."""
    release_response: dict[str, Any] = {}
    diagnostic_reused = bool(
        getattr(run, "diagnostic_stop", None) == "progressing_hard_cap"
        and getattr(run, "diagnostic_release_confirmed", False)
    )
    thrash_reused = _confirmed_live_thrash_stop(run)
    if diagnostic_reused:
        timeline.append("REL-5 cleanup observed the diagnostic stop's durable killed state")
        return release_response, True
    if thrash_reused:
        timeline.append("REL-5 cleanup observed the live-thrash monitor's durable killed state")
        return release_response, True
    try:
        release_response = await client.kill(cid)
    except Exception:
        return release_response, False
    http_status = int(release_response.get("http_status", 0))
    released = (
        200 <= http_status < 300
        and release_response.get("killed") is True
        and DiscoApiClient._status_of(release_response.get("state") or {}) == "IDLE"
    )
    if released:
        timeline.append(f"REL-5 released {cid} for cleanup adjudication (http {http_status})")
    else:
        timeline.append(
            f"REL-5 cleanup release was not acknowledged as killed IDLE (http {http_status})"
        )
    return release_response, released


def _scoped_cleanup_slice(
    sandbox_ids: list[str],
    container_names: list[str],
    volume_names: list[str] | None,
    baseline_dangling: set[str] | None,
    after_dangling: set[str] | None,
    *,
    released: bool,
    allow_global_fallback: bool,
) -> dict[str, Any] | None:
    """Measure resources attributable to this conversation's sandbox identifiers."""
    container_orphans = _scoped_disco_container_count(container_names, sandbox_ids)
    named_volume_orphans = (
        _scoped_disco_volume_count(volume_names, sandbox_ids) if volume_names is not None else None
    )
    unnamed_volume_orphans = _new_dangling_volume_count(
        baseline_dangling,
        after_dangling,
        exclude_disco_named=True,
    )
    if not allow_global_fallback:
        unnamed_volume_orphans = None
    complete = named_volume_orphans is not None and (
        not allow_global_fallback or unnamed_volume_orphans is not None
    )
    if not complete:
        return None
    volume_orphans = int(named_volume_orphans or 0) + int(unnamed_volume_orphans or 0)
    total_orphans = container_orphans + volume_orphans
    return {
        "orphans": total_orphans,
        "workspace_released": released and total_orphans == 0,
        "scope": "conversation",
        "container_orphans": container_orphans,
        "volume_orphans": volume_orphans,
        "volume_scope": "conversation",
    }


def _global_cleanup_slice(
    baseline_containers: int,
    container_names: list[str],
    baseline_dangling: set[str] | None,
    after_dangling: set[str] | None,
    *,
    released: bool,
) -> dict[str, Any] | None:
    """Measure the serial fallback from host-global before/after deltas."""
    container_orphans = max(0, len(container_names) - baseline_containers)
    volume_orphans = _new_dangling_volume_count(
        baseline_dangling,
        after_dangling,
        exclude_disco_named=False,
    )
    if volume_orphans is None:
        return None
    total_orphans = container_orphans + volume_orphans
    return {
        "orphans": total_orphans,
        "workspace_released": released and total_orphans == 0,
        "scope": "global",
        "container_orphans": container_orphans,
        "volume_orphans": volume_orphans,
        "volume_scope": "global_dangling",
    }


def _add_cleanup_evidence(
    evidence: dict[str, Any],
    run: Any,
    release_response: dict[str, Any],
    probes: CleanupBindings,
    *,
    baseline_containers: int | None,
    baseline_dangling: set[str] | None,
    released: bool,
    allow_global_fallback: bool,
    timeline: list[str],
) -> None:
    """Probe post-release resources and add the strongest attributable cleanup slice."""
    container_names = probes.live_disco_container_names()
    volume_names = probes.disco_volume_names()
    after_dangling = probes.dangling_volume_names()
    # The terminal state is not a complete ownership record after a restart:
    # the durable event stream is the frozen source of truth and may contain
    # both pre- and post-restart sandbox generations.  Feed each event as its
    # own payload so nested tool results / verifier selections are harvested
    # without teaching the extractor about an unstable event-list envelope.
    raw_frozen_events = getattr(run, "events", None) or []
    try:
        frozen_events = normalize_events(raw_frozen_events)
    except NormalizationError:
        frozen_events = raw_frozen_events
    sandbox_ids = _extract_sandbox_instance_ids(
        getattr(run, "state_final", {}) or {},
        release_response,
        evidence.get("diagnostic_stop"),
        *frozen_events,
    )
    if sandbox_ids and container_names is not None:
        cleanup = _scoped_cleanup_slice(
            sandbox_ids,
            container_names,
            volume_names,
            baseline_dangling,
            after_dangling,
            released=released,
            allow_global_fallback=allow_global_fallback,
        )
        if cleanup is None:
            timeline.append(
                "REL-5 scoped cleanup evidence incomplete: an applicable volume "
                "probe was unavailable; omitted cleanup adjudication"
            )
        else:
            evidence["cleanup"] = cleanup
        return
    if allow_global_fallback and baseline_containers is not None and container_names is not None:
        cleanup = _global_cleanup_slice(
            baseline_containers,
            container_names,
            baseline_dangling,
            after_dangling,
            released=released,
        )
        if cleanup is None:
            timeline.append(
                "REL-5 global cleanup evidence incomplete: dangling-volume probe "
                "was unavailable; omitted cleanup adjudication"
            )
        else:
            evidence["cleanup"] = cleanup
        return
    if not allow_global_fallback:
        timeline.append(
            "REL-5 cleanup attribution unavailable: parallel run exposed no sandbox "
            "instance id; refused contaminated global container delta"
        )


async def _collect_terminal_cleanup_evidence(
    client: ProductClient,
    cid: str,
    run: Any,
    *,
    baseline_containers: int | None,
    relay_log: str | None,
    timeline: list[str],
    baseline_dangling_volumes: set[str] | None = None,
    grace_s: float = 8.0,
    allow_global_cleanup_fallback: bool = True,
    bindings: CleanupBindings | None = None,
) -> dict[str, Any]:
    """Adjudicate lifecycle, post-terminal provider calls, release, and orphan cleanup."""
    probes = bindings or CleanupBindings(
        _live_disco_container_names,
        _disco_volume_names,
        _dangling_volume_names,
    )
    evidence: dict[str, Any] = dict(getattr(run, "product_evidence", None) or {})
    _add_lifecycle_evidence(run, evidence)
    calls_after = await _provider_calls_after_terminal(run, cid, relay_log, timeline, grace_s)
    release_response, released = await _release_for_cleanup(client, cid, run, timeline)
    if released and getattr(client, "last_conversation_id", None) == cid:
        client.last_conversation_id = None
    await asyncio.sleep(4.0)
    if calls_after is not None:
        evidence["sidecar"] = {
            "stopped_at_terminal": released,
            "provider_calls_after_terminal": calls_after,
        }
    _add_cleanup_evidence(
        evidence,
        run,
        release_response,
        probes,
        baseline_containers=baseline_containers,
        baseline_dangling=baseline_dangling_volumes,
        released=released,
        allow_global_fallback=allow_global_cleanup_fallback,
        timeline=timeline,
    )
    with contextlib.suppress(Exception):
        run.product_evidence = evidence
    return evidence
