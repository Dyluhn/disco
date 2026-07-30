"""Cohesive workspace finalization, capture, version/seal publication, and recovery.

Extracted from `lifecycle.py` (K6d decomposition). Owns the durable
finalization-journal helpers, ``commit_finished_workspace``, journal recovery,
workspace capture/version/seal publication, strict snapshot-completeness, and
synthetic deliverable logic.  ``LifecycleManager`` retains thin one-line
delegates so the existing runtime/test seams are undisturbed.

Receives its actual collaborators directly, without a runtime handle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from disco.core import (
    DEFAULT_OWNER_ID,
    ActionEvent,
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    DeliverableEvent,
    EventSource,
    ObservationEvent,
    StatusEvent,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
    agent_view_consistent_events,
    current_appkit_ejection,
    current_workspace_agent_view_id,
    derive_final_workspace_fence,
    event_matches_current_workspace_view,
    workspace_terminal_matches_current_run,
)
from disco.core.context.artifact_projection import manifest_shadow_enabled
from disco.core.loop import SealabilityProbeResult
from disco.tools import APPKIT_MUTATORS
from disco.tools.projects import (
    ProjectStore,
    StorageStatus,
    VersionRecord,
    WorkspaceTreeFacts,
)

from .workspace_commit import WorkspaceRunSuperseded, pending_workspace_run_intent
from .workspace_finalization import (
    FINALIZATION_JOURNAL,
    checkpoint_event,
    clear_finalization_journal,
    journal_facts,
    seal_event,
    write_finalization_journal,
)

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .artifact_manifest_shadow import ArtifactManifestShadow
    from .persistence_notifier import PersistenceNotifier
    from .project_runtime_service import ProjectRuntimeService
    from .run_registry import RunRegistry, RunResourceRegistry
    from .workspace_fence import WorkspaceFenceService

_LOG = logging.getLogger(__name__)


def _cut_recovery_version(
    store: ProjectStore,
    conversation_id: str,
    trigger: str,
    label: str,
) -> VersionRecord | None:
    version = (
        store.cut_version(conversation_id, label=label, trigger=trigger)
        if label
        else store.cut_version(conversation_id, trigger=trigger)
    )
    if version is not None:
        return version
    versions = store.list_versions(conversation_id)
    return versions[0] if versions else None


# The two deliberate non-deliverable exclusions the strict seal accepts.
# Dependency caches and runtime secret paths are outside the workspace-seal
# scope by design; every other skip makes final-byte attribution incomplete.
_ALLOWED_SKIP_SUFFIXES = (
    "dependency/cache path excluded",
    "runtime secret path excluded",
)

# REL-27 — the typed disclosure kind for a strict seal refused on CONTENT.
# The soak harness adjudicates on this product-owned value (never a private
# re-derivation of the rule), so it is a contract constant: changing it is a
# cross-repo classifier change.
SEAL_INCOMPLETE_CONTENT_KIND = "seal_incomplete_content"


# Deterministic CONTENT judgments of the snapshot walkers (both the Podman
# guest script and the portable fallback): stable properties of the workspace
# the model can act on. Transient capture failures (read/lstat/list failures,
# changed-while-archiving races) are deliberately NOT here — refusing a finish
# or branding a product failure on a transient would launder infra into
# product exactly the way F-27's classifier laundered product into infra.
_CONTENT_SKIP_SUFFIXES = (
    "symlink excluded",
    "non-regular entry excluded",
    "hardlinked entry excluded",
    "-byte cap",  # "<n> bytes exceeds the <m>-byte cap" (both walkers)
)


def strict_blocking_skips(skipped: list[str]) -> list[str]:
    """The skip entries the strict final seal refuses (all non-allowed skips).

    Single source of truth for "what blocks a seal" — the commit-time strict
    seal judges with exactly this rule.
    """

    return [item for item in skipped if not item.endswith(_ALLOWED_SKIP_SUFFIXES)]


def content_blocking_skips(skipped: list[str]) -> list[str]:
    """The blocking subset that is a DETERMINISTIC content judgment (REL-27).

    Shared verbatim by the finish-time sealability gate (refuse only what the
    model can actually fix) and the typed ``seal_incomplete_content``
    disclosure (attribute to the product only what was deterministically
    unsealable), so gate, seal, and disclosure can never disagree.
    """

    return [
        item for item in strict_blocking_skips(skipped) if item.endswith(_CONTENT_SKIP_SUFFIXES)
    ]


class FinalSealIncompleteContent(RuntimeError):
    """The strict final seal refused, and at least one refusal is a
    deterministic CONTENT judgment (symlink, hardlinked/non-regular entry,
    oversized file) — as distinct from storage/session unavailability or a
    transient capture failure. Carries both the full blocking list (the honest
    message) and the content subset (the typed disclosure) for F-27."""

    def __init__(self, blocking: list[str], content: list[str]) -> None:
        super().__init__("final workspace snapshot was incomplete: " + "; ".join(blocking[:8]))
        self.blocking = list(blocking)
        self.content_blocking = list(content)


def _seal_refusal_meta(exc: BaseException) -> dict[str, Any] | None:
    """REL-27 — the typed half of a content-refused strict-seal disclosure.

    The soak harness (and any auditor) distinguishes "the product refused to
    attribute unsealable content" from mere snapshot lag by this meta, never by
    parsing the English prose."""

    if not isinstance(exc, FinalSealIncompleteContent):
        return None
    return {
        "persistence_failure": {
            "kind": SEAL_INCOMPLETE_CONTENT_KIND,
            "blocking": exc.content_blocking[:32],
        }
    }


async def probe_finish_sealability(
    run_resources: RunResourceRegistry,
    conversation_id: str,
    *,
    snapshot_fn: Callable[..., Any] | None,
) -> SealabilityProbeResult:
    """REL-27 — dry-run the REAL final-seal snapshot; report strict refusals.

    Runs the exact per-backend snapshot machinery (`snapshot_fn` — the same
    callable the FINISHED commit passes) against a throwaway destination under
    a private temp directory, then judges the skip list with the SAME
    `content_blocking_skips` rule the typed seal disclosure uses. Zero
    content-model drift by construction; side-effect-free on durable storage.

    Refuses ONLY on deterministic content judgments the model can act on;
    transient capture failures stay advisory here (the commit-time seal still
    refuses them honestly, and the harness keeps classifying them as
    rerunnable evidence gaps, not product). No live sandbox / no snapshot
    function → sealable ("nothing to judge"): the commit-time seal and the
    skipped-capture disclosure remain the publication authority. Transport
    failures propagate — the core gate treats a probe error as advisory.
    """

    executor = run_resources.executor(conversation_id)
    session = getattr(executor, "_sandbox", None) if executor is not None else None
    if session is None or snapshot_fn is None:
        return SealabilityProbeResult(sealable=True, detail="no live workspace to probe")
    tmp = Path(tempfile.mkdtemp(prefix=".disco-seal-probe-"))
    try:
        result = await snapshot_fn(session, tmp / "tree")
        blocking = content_blocking_skips(list(result.skipped))
        return SealabilityProbeResult(sealable=not blocking, blocking=tuple(blocking[:64]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _facts_match_record(facts: WorkspaceTreeFacts, record: VersionRecord) -> bool:
    return (
        facts.file_count == record.file_count
        and facts.total_bytes == record.total_bytes
        and facts.tree_digest == record.tree_digest
    )


async def _require_seal_publication_stable(
    store: SqliteEventStore,
    conversation_id: str,
    project_store: ProjectStore,
    seal_fence: tuple[int, int | None],
    version: VersionRecord,
    *,
    host_mirror: bool,
) -> None:
    """Re-prove the durable head and governed live bytes during publication."""

    events = await store.get_events(conversation_id)
    if pending_workspace_run_intent(events) is not None:
        raise RuntimeError("workspace has unprocessed agent run intent")
    if derive_final_workspace_fence(events) != seal_fence:
        raise RuntimeError("workspace event head changed during finalization")
    if host_mirror and not _facts_match_record(
        project_store.inspect_workspace(conversation_id), version
    ):
        raise RuntimeError("host mirror changed during finalization")


def _skipped_capture(conversation_id: str, trigger: str) -> None:
    """Report a workspace capture that never ran, and return the skip value.

    A bare ``return None`` is indistinguishable from "captured nothing because
    there was nothing to capture", and the difference is a conversation's entire
    durable workspace — no tree, no versions, no manifest.

    Certified-lane evidence 2026-07-27 (`p4_ff_react_steer` seed 621005,
    INVALID_RUN / MISSING_REQUIRED_EVIDENCE): 68 actions over 1104s, then the run
    terminated with no ``<projects_root>/<cid>/`` at all. Its browser screenshots
    were referenced as durable evidence and had nowhere to live, and nothing in
    the log said a capture had been skipped.

    Best-effort on this path is deliberate — the strict ``seal_fence`` path
    raises instead — but silence is not. Lives at module level so reporting the
    skip costs :class:`WorkspacePersistence` no lines against its size budget.
    """

    _LOG.warning(
        "workspace capture SKIPPED for %s (trigger=%s): no capture session could be "
        "resolved; nothing was persisted for this conversation",
        conversation_id,
        trigger,
    )
    return None


class WorkspacePersistence:
    """Own workspace durability while callers supply the snapshot operation."""

    def __init__(
        self,
        store: SqliteEventStore,
        run_resources: RunResourceRegistry,
        workspace: WorkspaceFenceService,
        projects: ProjectRuntimeService,
        artifact_manifest_shadow: ArtifactManifestShadow,
        persistence_notifier: PersistenceNotifier,
        run_registry: RunRegistry,
    ) -> None:
        self._store = store
        self._run_resources = run_resources
        self._workspace = workspace
        self._projects = projects
        self._artifact_manifest_shadow = artifact_manifest_shadow
        self._persistence_notifier = persistence_notifier
        self._run_registry = run_registry

    async def _publish_strict_version(
        self,
        conversation_id: str,
        store: ProjectStore,
        seal_fence: tuple[int, int | None],
        facts: WorkspaceTreeFacts,
        journal: dict[str, Any] | None,
        *,
        host_mirror: bool,
    ) -> WorkspaceVersionEvent:
        """Cut, checkpoint, seal, and revalidate one strict immutable version."""

        version = store.cut_verified_version(conversation_id, trigger="finish", pin=True)
        if version is None:
            raise RuntimeError("verified version cut returned no version")
        if host_mirror and not _facts_match_record(facts, version):
            raise RuntimeError("host mirror changed during finalization")
        await _require_seal_publication_stable(
            self._store,
            conversation_id,
            store,
            seal_fence,
            version,
            host_mirror=host_mirror,
        )
        if journal is not None:
            journal.update(
                {
                    "phase": "version",
                    "version_seq": version.seq,
                    "file_count": version.file_count,
                    "total_bytes": version.total_bytes,
                    "tree_digest": version.tree_digest,
                }
            )
            write_finalization_journal(store, conversation_id, journal)
        checkpoint = await self._store.append(
            conversation_id,
            checkpoint_event(seal_fence, version),
        )
        if not isinstance(checkpoint, WorkspaceVersionEvent):
            raise RuntimeError("event store returned wrong finalization checkpoint type")
        await _require_seal_publication_stable(
            self._store,
            conversation_id,
            store,
            seal_fence,
            version,
            host_mirror=host_mirror,
        )
        if journal is not None:
            journal.update({"phase": "checkpoint", "checkpoint_event_id": checkpoint.id})
            write_finalization_journal(store, conversation_id, journal)
        stored = await self._store.append(
            conversation_id,
            seal_event(conversation_id, seal_fence, version),
        )
        if not isinstance(stored, WorkspaceVersionEvent):
            raise RuntimeError("event store returned wrong final seal event type")
        try:
            await _require_seal_publication_stable(
                self._store,
                conversation_id,
                store,
                seal_fence,
                version,
                host_mirror=host_mirror,
            )
        except Exception:
            await self._store.append(
                conversation_id,
                WorkspaceMutationEvent(operation="host.finalization-invalidated"),
            )
            raise
        if journal is not None:
            journal.update({"phase": "sealed", "seal_event_id": stored.id})
            write_finalization_journal(store, conversation_id, journal)
        return stored

    # -- commit_finished_workspace (bulk) ----------------------------------------

    async def _do_commit_finished_workspace(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool = False,
        snapshot_fn: Callable[..., Any],
    ) -> StatusEvent:
        """Persist FINISHED and its immutable workspace commit as one barrier.

        SQLite and the project filesystem cannot share a transaction. The
        journal makes every cross-store phase explicit, while the runtime-owned
        lock excludes tool and host mutations from terminal append through seal
        publication. A capture failure leaves the status durable but unsealed;
        strict consumers reject it instead of falling back to mutable bytes.
        """

        if terminal_event.status is not ConversationStatus.FINISHED:
            raise ValueError("terminal workspace commit requires FINISHED")
        lock = self._workspace.lock(conversation_id)
        async with lock:
            async with self._workspace.interprocess_mutation_fence(conversation_id):
                return await self._commit_finished_workspace_locked(
                    conversation_id,
                    terminal_event,
                    require_inactive_finished_head=require_inactive_finished_head,
                    host_mirror=False,
                    snapshot_fn=snapshot_fn,
                )

    async def _do_commit_finished_host_mirror_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
    ) -> StatusEvent:
        """Publish host-mirror bytes while the caller retains the workspace lock."""

        lock = self._workspace.lock(conversation_id)
        if not lock.locked():
            raise RuntimeError("host-mirror finalization requires the conversation lock")
        if terminal_event.status is not ConversationStatus.FINISHED:
            raise ValueError("terminal workspace commit requires FINISHED")
        async with self._workspace.interprocess_mutation_fence(conversation_id):
            return await self._commit_finished_workspace_locked(
                conversation_id,
                terminal_event,
                require_inactive_finished_head=True,
                host_mirror=True,
                snapshot_fn=None,
            )

    async def _commit_finished_workspace_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool,
        host_mirror: bool,
        snapshot_fn: Callable[..., Any] | None,
    ) -> StatusEvent:
        """Shared journaled terminal pipeline; caller owns the workspace lock."""

        if require_inactive_finished_head:
            if self._workspace.has_run_claim(conversation_id):
                raise RuntimeError("cannot seal a host revision while an agent run is active")
            current = await self._store.get_state(conversation_id)
            if current.execution_status is not ConversationStatus.FINISHED:
                raise RuntimeError(
                    "host revision can be sealed only from an inactive FINISHED head"
                )
        events_before_terminal = await self._store.get_events(conversation_id)
        same_id = [event for event in events_before_terminal if event.id == terminal_event.id]
        if same_id:
            # Event ids are the idempotency key for the terminal barrier.  A
            # retry of the exact persisted FINISHED must be a read-only return:
            # running the shadow fold again would create an effect *after* the
            # existing terminal and invalidate its otherwise valid seal.
            if len(same_id) != 1 or not isinstance(same_id[0], StatusEvent):
                raise RuntimeError("terminal event id collides with another persisted event")
            existing_terminal = same_id[0]
            if (
                existing_terminal.status is not ConversationStatus.FINISHED
                or existing_terminal.model_dump(exclude={"seq"})
                != terminal_event.model_dump(exclude={"seq"})
            ):
                raise RuntimeError("terminal event id was reused with a different payload")
            try:
                fence = derive_final_workspace_fence(events_before_terminal)
            except ValueError as exc:
                raise WorkspaceRunSuperseded(
                    "persisted terminal is no longer the current workspace fence"
                ) from exc
            if existing_terminal.seq != fence[0]:
                raise WorkspaceRunSuperseded(
                    "persisted terminal is no longer the current workspace fence"
                )
            # If the first attempt remained unsealed, keep that state honest.
            # Recovery may resume a non-writing capture/checkpoint phase, but a
            # terminal retry must never backfill workspace bytes after FINISHED.
            return existing_terminal
        terminal_matches = workspace_terminal_matches_current_run(
            events_before_terminal, terminal_event
        )
        if require_inactive_finished_head and (
            terminal_event.host_mutation_id is None or not terminal_matches
        ):
            raise RuntimeError("host revision terminal lacks matching mutation authority")
        if not require_inactive_finished_head and not terminal_matches:
            raise WorkspaceRunSuperseded("cannot seal output from a stale agent view generation")
        if pending_workspace_run_intent(events_before_terminal) is not None and not (
            not require_inactive_finished_head and terminal_matches
        ):
            raise RuntimeError("cannot seal while an agent run intent is unprocessed")

        store = self._projects.current_project_store()
        if store is None or store.status() is not StorageStatus.OK:
            unavailable_terminal = await self._store.append(conversation_id, terminal_event)
            if not isinstance(unavailable_terminal, StatusEvent):
                raise RuntimeError("event store returned wrong terminal event type")
            return unavailable_terminal

        journal: dict[str, Any] = {
            "schema_version": 1,
            "conversation_id": conversation_id,
            "terminal_event_id": terminal_event.id,
            "phase": "prepared",
        }
        try:
            write_finalization_journal(store, conversation_id, journal)
        except Exception as exc:
            _LOG.error(
                "could not prepare finalization journal for %s; FINISHED will be unsealed",
                conversation_id,
                exc_info=True,
            )
            unjournaled_terminal = await self._store.append(conversation_id, terminal_event)
            if not isinstance(unjournaled_terminal, StatusEvent):
                raise RuntimeError("event store returned wrong terminal event type") from exc
            return unjournaled_terminal

        async def finalize() -> tuple[StatusEvent, WorkspaceVersionEvent | None]:
            # The whole terminal pipeline is one drained task. Cancellation can
            # otherwise strand an avoidable partial finalization after FINISHED.
            #
            # PIN the sandbox now, while the run that produced these bytes is still
            # the live one. The seal used to re-resolve it from `runtime._executors`
            # AFTER appending FINISHED, and anything that dropped the executor in
            # that window (notably the sealed-run exception rollback) left the build
            # unsealed: "no sandbox (executor=None) — workspace NOT persisted", then
            # "FINISHED remains unsealed". No durable version exists after that, so
            # a later restore 503s and the user's finished build is silently unsaved
            # (pilot seed 406546, p4_ff_import_rollback). Holding the reference does
            # not keep a dead box alive — capture still fails closed if the session
            # is gone — it removes the WINDOW.
            pinned_executor = self._run_resources.executor(conversation_id)
            pinned_session = (
                getattr(pinned_executor, "_sandbox", None) if pinned_executor is not None else None
            )
            events = await self._store.get_events(conversation_id)
            # The shadow manifest is a real workspace write. Record its effect
            # before touching bytes and complete it before FINISHED so the
            # terminal fence and immutable version describe the same cut.
            # Host-mirror commits must never recapture or mutate sandbox bytes.
            if not host_mirror and manifest_shadow_enabled():
                await self._store.append(
                    conversation_id,
                    WorkspaceMutationEvent(
                        operation="agent.artifact-manifest-fold",
                        paths=(".disco/context/artifact_manifest.json",),
                        agent_view_id=terminal_event.agent_view_id,
                    ),
                )
                events = await self._store.get_events(conversation_id)
                await self._artifact_manifest_shadow.fold(
                    conversation_id,
                    events=events,
                )
            stored = await self._store.append(conversation_id, terminal_event)
            if not isinstance(stored, StatusEvent) or stored.seq is None:
                raise RuntimeError("event store returned an unsequenced terminal event")
            try:
                events = await self._store.get_events(conversation_id)
                fence = derive_final_workspace_fence(events)
                if fence[0] != stored.seq:
                    raise RuntimeError("persisted FINISHED is not the canonical terminal fence")
                journal.update(
                    {
                        "phase": "terminal",
                        "terminal_seq": fence[0],
                        "latest_effect_seq": fence[1],
                    }
                )
                write_finalization_journal(store, conversation_id, journal)
                journal["phase"] = "capturing"
                write_finalization_journal(store, conversation_id, journal)
                sealed = await self._do_capture_workspace(
                    conversation_id,
                    trigger="finish",
                    seal_fence=fence,
                    journal=journal,
                    snapshot_fn=snapshot_fn,
                    host_mirror=host_mirror,
                    pinned_session=pinned_session,
                )
                return stored, sealed
            except Exception:
                _LOG.error(
                    "final workspace seal failed for %s; FINISHED remains unsealed",
                    conversation_id,
                    exc_info=True,
                )
                return stored, None

        # Cancellation changes what the caller observes, never when the barrier
        # releases: drain through a sealed-or-honestly-unsealed outcome.
        finalizer = asyncio.create_task(finalize())
        cancellation: asyncio.CancelledError | None = None
        stored: StatusEvent
        sealed: WorkspaceVersionEvent | None = None
        while not finalizer.done():
            try:
                await asyncio.shield(finalizer)
            except asyncio.CancelledError as exc:
                if finalizer.cancelled():
                    raise
                cancellation = cancellation or exc
        try:
            stored, sealed = finalizer.result()
        except Exception:
            if cancellation is not None:
                _LOG.error(
                    "terminal workspace pipeline failed while draining cancellation for %s",
                    conversation_id,
                    exc_info=True,
                )
                raise cancellation from None
            raise
        if sealed is not None:
            clear_finalization_journal(store, conversation_id)
        if cancellation is not None:
            raise cancellation
        return stored

    def _has_active_work(self, conversation_id: str) -> bool:
        """True when a suspend would KILL in-flight work.

        Replicated from LifecycleManager so the bulk commit/capture methods can
        use it without a cross-object call.
        """
        task = self._run_registry.task(conversation_id)
        return task is not None and not task.done()

    # -- journal recovery ------------------------------------------------------

    async def _do_recover_finalization_journals(self) -> int:
        """Recover only a version bound to the terminal by SQLite evidence.

        Filesystem-only phases remain honestly unsealed.  The recoverable
        ``checkpoint``/``sealed`` phases must name the exact canonical terminal
        fence, immutable version facts, and SQLite checkpoint event written by
        the interrupted finalizer.
        """

        store = self._projects.current_project_store()
        if store is None or store.status() is not StorageStatus.OK or store.root is None:
            return 0
        recovered = 0
        try:
            root = store.root.resolve()
            projects = sorted(
                path
                for path in root.iterdir()
                if not path.is_symlink() and path.is_dir() and path.resolve().is_relative_to(root)
            )
        except OSError:
            _LOG.warning("finalization journal scan failed", exc_info=True)
            return 0
        for project_dir in projects:
            path = project_dir / FINALIZATION_JOURNAL
            if path.is_symlink() or not path.is_file():
                continue
            conversation_id = project_dir.name
            lock = self._workspace.lock(conversation_id)
            async with lock, self._workspace.interprocess_mutation_fence(conversation_id):
                try:
                    raw = json.loads(path.read_text())
                    if not isinstance(raw, dict):
                        raise ValueError("journal root is not an object")
                    journal = raw
                    if (
                        journal.get("schema_version") != 1
                        or journal.get("conversation_id") != conversation_id
                        or not isinstance(journal.get("terminal_event_id"), str)
                    ):
                        raise ValueError("journal identity is invalid")
                    events = await self._store.get_events(conversation_id)
                    if pending_workspace_run_intent(events) is not None:
                        raise ValueError("journal head has an unprocessed agent run intent")
                    terminal = next(
                        (
                            event
                            for event in events
                            if isinstance(event, StatusEvent)
                            and event.id == journal["terminal_event_id"]
                            and event.status is ConversationStatus.FINISHED
                        ),
                        None,
                    )
                    if terminal is None:
                        if journal.get("phase") == "prepared":
                            clear_finalization_journal(store, conversation_id)
                        continue
                    fence = derive_final_workspace_fence(events)
                    if terminal.seq != fence[0]:
                        raise ValueError("journal terminal is no longer the canonical fence")

                    existing = next(
                        (
                            event
                            for event in reversed(events)
                            if isinstance(event, WorkspaceVersionEvent)
                            and event.final_seal is not None
                            and event.final_seal.terminal_seq == fence[0]
                        ),
                        None,
                    )
                    if existing is not None and existing.final_seal is not None:
                        record = store.verify_version(conversation_id, existing.version_seq)
                        seal = existing.final_seal
                        if (
                            seal.latest_effect_seq != fence[1]
                            or seal.file_count != record.file_count
                            or seal.total_bytes != record.total_bytes
                            or seal.tree_digest != record.tree_digest
                        ):
                            raise ValueError("persisted seal disagrees with immutable version")
                        clear_finalization_journal(store, conversation_id)
                        recovered += 1
                        continue

                    phase = journal.get("phase")
                    if phase in {"prepared", "terminal", "capturing", "snapshot", "version"}:
                        # Filesystem-only journal state is not trusted to mint a
                        # seal. Recovery advances only after the exact version is
                        # bound to this terminal by a durable SQLite checkpoint.
                        continue
                    if (
                        journal.get("terminal_seq") != fence[0]
                        or journal.get("latest_effect_seq") != fence[1]
                    ):
                        raise ValueError("finalization journal fence is invalid")
                    facts = journal_facts(journal)
                    if phase in {"checkpoint", "sealed"}:
                        version_seq = journal.get("version_seq")
                        if type(version_seq) is not int or version_seq < 1:
                            raise ValueError("journal version sequence is invalid")
                        version = store.verify_version(conversation_id, version_seq)
                        if not _facts_match_record(facts, version):
                            raise ValueError("immutable version disagrees with journal")
                        if not _facts_match_record(
                            store.inspect_workspace(conversation_id), version
                        ):
                            raise ValueError("live workspace disagrees with recovered version")
                        checkpoint = next(
                            (
                                event
                                for event in events
                                if isinstance(event, WorkspaceVersionEvent)
                                and event.final_seal is None
                                and event.version_seq == version.seq
                                and event.tree_digest == version.tree_digest
                                and event.trigger == f"finalizing:{fence[0]}"
                            ),
                            None,
                        )
                        if checkpoint is None:
                            raise ValueError("finalization version lacks its SQLite checkpoint")
                        checkpoint_id = journal.get("checkpoint_event_id")
                        if not isinstance(checkpoint_id, str) or checkpoint_id != checkpoint.id:
                            raise ValueError("journal checkpoint identity is invalid")
                    else:
                        raise ValueError(f"unknown finalization journal phase: {phase!r}")

                    stored = await self._store.append(
                        conversation_id,
                        seal_event(conversation_id, fence, version),
                    )
                    if not isinstance(stored, WorkspaceVersionEvent):
                        raise RuntimeError("event store returned wrong recovered seal type")
                    clear_finalization_journal(store, conversation_id)
                    recovered += 1
                except Exception:
                    _LOG.error(
                        "finalization journal recovery failed for %s; workspace remains unsealed",
                        conversation_id,
                        exc_info=True,
                    )
        return recovered

    # -- capture_workspace (bulk) ----------------------------------------------

    async def _conversation_manifest_metadata(
        self,
        conversation_id: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Best-effort title, creation time, and owner for the project manifest."""

        try:
            summaries = await self._store.list_conversation_summaries(
                owner_id=DEFAULT_OWNER_ID, limit=500, cursor=None
            )
            row = next((s for s in summaries if s.conversation_id == conversation_id), None)
            if row is not None:
                return row.title, row.created_at, row.owner_id
        except Exception:  # noqa: BLE001 — metadata is best-effort
            pass
        return None, None, None

    async def _do_capture_workspace(
        self,
        conversation_id: str,
        *,
        trigger: str,
        version_label: str = "",
        seal_fence: tuple[int, int | None] | None = None,
        journal: dict[str, Any] | None = None,
        snapshot_fn: Callable[..., Any] | None,
        host_mirror: bool = False,
        pinned_session: Any | None = None,
    ) -> WorkspaceVersionEvent | None:
        """Mirror live bytes and optionally publish a strict immutable seal.

        ``seal_fence`` selects the strict path. Any incomplete capture, missing
        storage/session, version disagreement, or event-store rejection then
        raises and leaves FINISHED honestly unsealed. Ordinary PAUSED/STUCK/etc.
        snapshots preserve their historical best-effort behavior.
        """

        if host_mirror:
            store = self._projects.current_project_store()
            if store is None or store.status() is not StorageStatus.OK:
                raise RuntimeError("project store unavailable for host-mirror finalization")
            session = None
        else:
            resolved = await self._resolve_capture_session(
                conversation_id,
                seal_fence=seal_fence,
                pinned_session=pinned_session,
            )
            if resolved is None:
                return _skipped_capture(conversation_id, trigger)
            session, store = resolved
        title, created_at, owner_id = await self._conversation_manifest_metadata(conversation_id)
        started = time.monotonic()
        source = "host-mirror" if host_mirror else "sandbox"
        _LOG.info(
            "workspace capture started for %s (trigger=%s source=%s)",
            conversation_id,
            trigger,
            source,
        )
        try:
            inspect_workspace = getattr(store, "inspect_workspace", None)
            if host_mirror:
                if seal_fence is None:
                    raise RuntimeError("host-mirror capture requires a final seal fence")
                if not callable(inspect_workspace):
                    raise RuntimeError("project store cannot prove host-mirror facts")
                facts = cast(WorkspaceTreeFacts, inspect_workspace(conversation_id))
            else:
                if snapshot_fn is None:
                    raise RuntimeError("sandbox capture function is unavailable")
                result = await snapshot_fn(session, store.path_for(conversation_id))
                if seal_fence is not None:
                    blocking = strict_blocking_skips(list(result.skipped))
                    if blocking:
                        content = content_blocking_skips(list(result.skipped))
                        if content:
                            raise FinalSealIncompleteContent(blocking, content)
                        raise RuntimeError(
                            "final workspace snapshot was incomplete: " + "; ".join(blocking[:8])
                        )
                if callable(inspect_workspace):
                    facts = cast(WorkspaceTreeFacts, inspect_workspace(conversation_id))
                elif seal_fence is not None:
                    raise RuntimeError("project store cannot prove final workspace facts")
                else:
                    facts = WorkspaceTreeFacts(
                        file_count=result.file_count,
                        total_bytes=result.total_bytes,
                        tree_digest="0" * 64,
                    )
            _LOG.info(
                "workspace capture completed for %s: %d files, %d bytes in %.3fs (source=%s)",
                conversation_id,
                facts.file_count,
                facts.total_bytes,
                time.monotonic() - started,
                source,
            )
            if journal is not None:
                journal.update(
                    {
                        "phase": "snapshot",
                        "file_count": facts.file_count,
                        "total_bytes": facts.total_bytes,
                        "tree_digest": facts.tree_digest,
                    }
                )
            store.write_manifest(
                conversation_id,
                title=title,
                owner_id=owner_id,
                created_at=created_at,
                file_count=facts.file_count,
                total_bytes=facts.total_bytes,
            )
            await self._do_maybe_synthesize_app_deliverable(
                conversation_id, store.path_for(conversation_id)
            )
            if journal is not None:
                write_finalization_journal(store, conversation_id, journal)
            try:
                if seal_fence is not None:
                    return await self._publish_strict_version(
                        conversation_id,
                        store,
                        seal_fence,
                        facts,
                        journal,
                        host_mirror=host_mirror,
                    )

                version = _cut_recovery_version(store, conversation_id, trigger, version_label)
                if version is not None:
                    stored = await self._store.append(
                        conversation_id,
                        WorkspaceVersionEvent(
                            version_seq=version.seq,
                            tree_digest=version.tree_digest,
                            trigger=trigger,
                        ),
                    )
                    return stored if isinstance(stored, WorkspaceVersionEvent) else None
            except Exception:  # noqa: BLE001
                _LOG.warning(
                    "version cut failed for %s after snapshot",
                    conversation_id,
                    exc_info=True,
                )
                if seal_fence is not None:
                    raise
        except asyncio.CancelledError:
            _LOG.warning(
                "snapshot canceled for %s after %.3fs (trigger=%s)",
                conversation_id,
                time.monotonic() - started,
                trigger,
            )
            raise
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            _LOG.warning(
                "snapshot failed for %s after %.3fs (trigger=%s): %s",
                conversation_id,
                time.monotonic() - started,
                trigger,
                exc,
            )
            await self._persistence_notifier.emit(
                conversation_id,
                f"snapshot failed: {exc}",
                meta=_seal_refusal_meta(exc),
            )
            if seal_fence is not None:
                raise
            return None

    async def _resolve_capture_session(
        self,
        conversation_id: str,
        *,
        seal_fence: tuple[int, int | None] | None = None,
        pinned_session: Any | None = None,
    ) -> tuple[Any, ProjectStore] | None:
        """Validate store status and resolve the live sandbox session for capture.

        Returns (session, store) or None to indicate a non-sealing skip.
        Raises RuntimeError when seal_fence is set and prerequisites fail.
        """
        store = self._projects.current_project_store()
        if store is None:
            _LOG.warning(
                "snapshot %s: no project store — workspace NOT persisted",
                conversation_id,
            )
            if seal_fence is not None:
                raise RuntimeError("no project store available for final workspace seal")
            return None
        status = store.status()
        if status != StorageStatus.OK:
            _LOG.warning(
                "snapshot %s: store status=%s — NOT saved",
                conversation_id,
                status.value,
            )
            await self._persistence_notifier.emit(
                conversation_id,
                f"project storage is {status.value}; this build was NOT saved.",
            )
            if seal_fence is not None:
                raise RuntimeError(f"project storage is {status.value}")
            return None
        # A caller that pinned the sandbox at the terminal wins over a fresh
        # lookup: by seal time the executor may already have been dropped, and
        # re-resolving it there is what left FINISHED builds unsealed.
        #
        # But a pin must never be WORSE than the lookup it replaces. If the box
        # rotated between the terminal and the seal, the pinned reference is a dead
        # generation, so fall back to whatever is live now. The pin closes the
        # dropped-executor window; it does not bind the seal to a corpse.
        executor = self._run_resources.executor(conversation_id)
        live_session = getattr(executor, "_sandbox", None) if executor is not None else None
        session = pinned_session
        if session is not None and live_session is not None and live_session is not session:
            _LOG.warning(
                "snapshot %s: pinned sandbox is a previous generation — sealing the live one",
                conversation_id,
            )
            session = live_session
        if session is None:
            session = live_session
        if session is None:
            _LOG.warning(
                "snapshot SKIPPED for %s: no sandbox (executor=%s) — workspace NOT persisted",
                conversation_id,
                type(executor).__name__ if executor is not None else None,
            )
            if seal_fence is not None:
                raise RuntimeError("no live sandbox available for final workspace seal")
            return None
        return session, store

    # -- synthetic deliverable -------------------------------------------------

    async def _do_maybe_synthesize_app_deliverable(
        self, conversation_id: str, snapshot_dir: Path
    ) -> None:
        """Fix 2 (B-H.1): append a synthetic app DeliverableEvent for a shell-served
        build (index.html on disk, no serve tool-call). Idempotent + best-effort —
        a missing index.html, an existing app-deliverable, or any read/append failure
        is a silent no-op (the snapshot itself already succeeded)."""
        try:
            existing = await self._store.get_events(conversation_id)
            latest_admission = next(
                (
                    event
                    for event in reversed(existing)
                    if isinstance(event, BuildPlatformAdmissionEvent)
                ),
                None,
            )
            governed_contract = (
                latest_admission.verification_contract if latest_admission is not None else None
            )
            if any(
                isinstance(event, DeliverableEvent)
                and event.artifact_kind == "app"
                and event_matches_current_workspace_view(existing, event)
                and (
                    governed_contract is None
                    or (
                        event.target_id == governed_contract.target_id
                        and event.delivery_contract == governed_contract.delivery
                        and event.verification_contract_digest == governed_contract.digest
                    )
                )
                for event in existing
            ):
                return  # idempotency read goes through the durable store
            if governed_contract is not None:
                # Governed completion establishes its exact target handoff before
                # the terminal.  Post-terminal synthesis must never invent or
                # "upgrade" missing run authority after the admission has released.
                return
            path = self._trusted_verified_app_entry(existing, snapshot_dir)
            if path is None:
                if any(isinstance(event, DeliverableEvent) for event in existing):
                    # A non-app handoff cannot identify the runnable application.
                    # Preserve it without guessing a competing root. Strict AppKit
                    # is the exception above: its paired host verifier supplies the
                    # exact built entry even when the run also attached files.
                    return
                index = self._find_snapshot_index(snapshot_dir)
                if index is None:
                    return
                rel_dir = index.parent.relative_to(snapshot_dir).as_posix()
                path = rel_dir if rel_dir and rel_dir != "." else "."
            await self._store.append(
                conversation_id,
                DeliverableEvent(
                    source=EventSource.AGENT,
                    agent_view_id=current_workspace_agent_view_id(existing),
                    title="Web app",
                    path=path,
                    artifact_kind="app",
                    deployment_url="",
                ),
            )
        except Exception:  # noqa: BLE001 — synthetic card is a convenience, never crash snapshot
            _LOG.debug("synthetic app-deliverable skipped for %s", conversation_id, exc_info=True)

    @staticmethod
    def _trusted_verified_app_entry(events: list[Any], snapshot_dir: Path) -> str | None:
        """Latest paired host verifier's exact built entry, if present in the snapshot."""

        projected = agent_view_consistent_events(events)
        if current_appkit_ejection(projected) is not None:
            return None
        actions = {event.id: event for event in projected if isinstance(event, ActionEvent)}
        # Terminal finalization records this exact internal-manifest fold after
        # AppKit verification. It does not change the verified application entry;
        # every other workspace mutation continues to make the receipt stale.
        last_mutation_seq = max(
            (
                event.seq or -1
                for event in projected
                if (
                    isinstance(event, WorkspaceMutationEvent)
                    and event.operation != "agent.artifact-manifest-fold"
                )
                or (
                    isinstance(event, ObservationEvent)
                    and event.tool_result.success is True
                    and (action := actions.get(event.action_id or "")) is not None
                    and action.tool_call.tool_name in APPKIT_MUTATORS
                )
            ),
            default=-1,
        )
        candidates: list[tuple[int, str]] = []
        for event in projected:
            if not isinstance(event, ObservationEvent):
                continue
            result = event.tool_result
            action = actions.get(event.action_id or "")
            structured = result.structured
            if (
                action is None
                or result.tool_name != "verify_appkit_app"
                or action.tool_call.tool_name != result.tool_name
                or result.call_id != action.tool_call.call_id
                or result.success is not True
                or (event.seq or -1) <= last_mutation_seq
                or not isinstance(structured, dict)
                or structured.get("passed") is not True
            ):
                continue
            entry = structured.get("canonical_entry_path")
            if not isinstance(entry, str) or not entry or "\\" in entry or "\x00" in entry:
                continue
            rel = Path(entry)
            if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
                continue
            target = snapshot_dir.joinpath(*rel.parts)
            if target.is_symlink() or not target.is_file():
                continue
            candidates.append((event.seq or -1, rel.as_posix()))
        return max(candidates)[1] if candidates else None

    @staticmethod
    def _find_snapshot_index(snapshot_dir: Path) -> Path | None:
        """First index.html in the snapshot (root preferred, else shallowest subdir),
        skipping internal dirs — mirrors SandboxSession._detect_serve_dir's skip set."""
        root = snapshot_dir / "index.html"
        if root.is_file():
            return root
        candidates = [
            p
            for p in snapshot_dir.rglob("index.html")
            if p.is_file()
            and ".pmx" not in p.parts
            and ".disco" not in p.parts
            and "node_modules" not in p.parts
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda p: len(p.parts))
