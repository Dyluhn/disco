"""Host-owned AppKit-to-Freeform revision transition."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from disco.core import (
    APPKIT_EJECTION_LOST_GUARANTEES,
    APPKIT_EJECTION_SOURCE_TRIGGER,
    APPKIT_EJECTION_TARGET_TRIGGER,
    ActionEvent,
    AppKitEjectionEvent,
    ConversationStatus,
    Event,
    StatusEvent,
    ToolCall,
    WorkspaceVersionEvent,
)
from disco.core.build_platform import APPKIT_PROFILE_ID, FREEFORM_PROFILE_ID
from disco.core.llm import ToolSpec
from disco.core.store import EventStore
from disco.tools.appkit_scope import (
    APPKIT_EJECTION_PATH,
    APPKIT_EJECTION_SOURCE_LABEL,
    APPKIT_EJECTION_TARGET_LABEL,
    REQUEST_CUSTOM_BUILD,
    AppKitEjectionReceipt,
)
from disco.tools.builtin.request_custom_build import RequestCustomBuildArgs
from disco.tools.projects import ProjectStore, StorageStatus, VersionRecord
from disco.tools.sandbox.base import SandboxInstance

from .build_platform_shadow import BuildPlatformRouteRecord
from .workspace_service import WorkspaceCoordinator


class AppKitEventStore(EventStore, Protocol):
    """Event authority with the durable AppKit-mode identity lookup."""

    def conversation_appkit_mode_sync(self, conversation_id: str) -> bool | None: ...


class AppKitRevisionCapture(Protocol):
    """Capture immutable workspace revisions under the caller-owned fence."""

    async def capture_workspace(
        self,
        conversation_id: str,
        *,
        trigger: str,
        version_label: str,
    ) -> WorkspaceVersionEvent | None: ...


class AppKitProjectStores(Protocol):
    """Resolve the currently configured project store at operation time."""

    def current_project_store(self) -> ProjectStore: ...


class AppKitSandboxes(Protocol):
    """Resolve the live AppKit sandbox without exposing executor ownership."""

    def sandbox_for(self, conversation_id: str) -> SandboxInstance | None: ...


class AppKitEjectionLedger:
    """Live process cache reconstructed from durable ejection events."""

    def __init__(self) -> None:
        self._ejected: set[str] = set()

    def is_appkit_ejected(self, conversation_id: str) -> bool:
        return conversation_id in self._ejected

    def record(self, conversation_id: str, *, ejected: bool) -> None:
        if ejected:
            self._ejected.add(conversation_id)
        else:
            self._ejected.discard(conversation_id)


class AppKitTransitionPort(Protocol):
    """Prepare and durably publish the Build admission transition."""

    async def prepare_appkit_ejection_locked(
        self,
        conversation_id: str,
        *,
        tool_specs: Iterable[ToolSpec],
    ) -> BuildPlatformRouteRecord | None: ...

    async def transition_appkit_ejection_locked(
        self,
        conversation_id: str,
        ejection: AppKitEjectionEvent,
        *,
        prepared_record: BuildPlatformRouteRecord | None,
    ) -> AppKitEjectionEvent: ...


@dataclass(frozen=True, slots=True)
class _EjectionResources:
    store: ProjectStore
    session: SandboxInstance
    prepared_record: BuildPlatformRouteRecord | None


@dataclass(frozen=True, slots=True)
class _SourceRevision:
    record: VersionRecord
    manifest: bytes


class AppKitEjectionService:
    """Cut the governed source and explicit Freeform target under one fence."""

    def __init__(
        self,
        *,
        event_store: AppKitEventStore,
        workspace: WorkspaceCoordinator,
        revision_capture: AppKitRevisionCapture,
        project_stores: AppKitProjectStores,
        sandboxes: AppKitSandboxes,
        transitions: AppKitTransitionPort,
    ) -> None:
        self._event_store = event_store
        self._workspace = workspace
        self._revision_capture = revision_capture
        self._project_stores = project_stores
        self._sandboxes = sandboxes
        self._transitions = transitions

    @staticmethod
    def _matching_action(events: list[Event], call: ToolCall) -> ActionEvent | None:
        return next(
            (
                event
                for event in reversed(events)
                if isinstance(event, ActionEvent)
                and event.tool_call.tool_name == REQUEST_CUSTOM_BUILD
                and event.tool_call.call_id == call.call_id
            ),
            None,
        )

    @staticmethod
    def _confirmation_wait(
        events: list[Event],
        action: ActionEvent,
        action_seq: int,
    ) -> StatusEvent | None:
        return next(
            (
                event
                for event in events
                if isinstance(event, StatusEvent)
                and event.status is ConversationStatus.WAITING_FOR_CONFIRMATION
                and event.detail == action.id
                and type(event.seq) is int
                and event.seq > action_seq
            ),
            None,
        )

    @staticmethod
    def _confirmation_resume(
        events: list[Event],
        action: ActionEvent,
        waiting_seq: int,
    ) -> StatusEvent | None:
        return next(
            (
                event
                for event in events
                if isinstance(event, StatusEvent)
                and event.status is ConversationStatus.RUNNING
                and type(event.seq) is int
                and event.seq > waiting_seq
                and event.agent_view_id == action.agent_view_id
            ),
            None,
        )

    @classmethod
    def _confirmed_action(cls, events: Iterable[Event], call: ToolCall) -> ActionEvent:
        materialized = list(events)
        action = cls._matching_action(materialized, call)
        if action is None or type(action.seq) is not int or action.agent_view_id is None:
            raise RuntimeError("the persisted AppKit ejection action is unavailable")
        waiting = cls._confirmation_wait(materialized, action, action.seq)
        if waiting is None or type(waiting.seq) is not int:
            raise RuntimeError("AppKit ejection requires the recorded human confirmation")
        resumed = cls._confirmation_resume(materialized, action, waiting.seq)
        if resumed is None:
            raise RuntimeError("AppKit ejection requires the recorded human confirmation")
        return action

    @staticmethod
    def _manifest(
        args: RequestCustomBuildArgs,
        *,
        source_version_seq: int,
        source_tree_digest: str,
    ) -> bytes:
        document = {
            "schema_version": 1,
            "transition": "appkit_to_freeform",
            "source_profile_id": APPKIT_PROFILE_ID.canonical,
            "target_profile_id": FREEFORM_PROFILE_ID.canonical,
            "source_version_seq": source_version_seq,
            "source_tree_digest": source_tree_digest,
            "reason": args.reason,
            "needed_capabilities": sorted(set(args.needed_capabilities)),
            "lost_guarantees": list(APPKIT_EJECTION_LOST_GUARANTEES),
            "appkit_verified": False,
        }
        return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()

    @staticmethod
    def _parse_recovery_manifest(raw: bytes, args: RequestCustomBuildArgs) -> tuple[int, str]:
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("the existing AppKit ejection manifest is invalid") from exc
        expected = {
            "schema_version": 1,
            "transition": "appkit_to_freeform",
            "source_profile_id": APPKIT_PROFILE_ID.canonical,
            "target_profile_id": FREEFORM_PROFILE_ID.canonical,
            "reason": args.reason,
            "needed_capabilities": sorted(set(args.needed_capabilities)),
            "lost_guarantees": list(APPKIT_EJECTION_LOST_GUARANTEES),
            "appkit_verified": False,
        }
        if not isinstance(document, dict) or any(
            document.get(key) != value for key, value in expected.items()
        ):
            raise RuntimeError(
                "an incomplete prior ejection used different terms; retry the original request"
            )
        seq = document.get("source_version_seq")
        digest = document.get("source_tree_digest")
        if (
            type(seq) is not int
            or seq < 1
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise RuntimeError("the existing AppKit ejection source identity is invalid")
        return seq, digest

    def _require_workspace_authority(self, conversation_id: str) -> None:
        lock = self._workspace.lock(conversation_id)
        if not lock.locked() or not self._workspace._fence_owned_by_current_task(
            conversation_id
        ):
            raise RuntimeError("AppKit ejection requires the workspace process fence")
        if self._event_store.conversation_appkit_mode_sync(conversation_id) is not True:
            raise RuntimeError("the conversation is not governed by AppKit")

    async def _resources(
        self,
        conversation_id: str,
        *,
        tool_specs: Iterable[ToolSpec],
    ) -> _EjectionResources:
        store = self._project_stores.current_project_store()
        if store.status() is not StorageStatus.OK:
            raise RuntimeError("project storage is unavailable for the ejection revision")
        session = self._sandboxes.sandbox_for(conversation_id)
        if session is None:
            raise RuntimeError("the AppKit workspace is unavailable for revision capture")
        prepared = await self._transitions.prepare_appkit_ejection_locked(
            conversation_id,
            tool_specs=tool_specs,
        )
        return _EjectionResources(store=store, session=session, prepared_record=prepared)

    async def _capture_source_revision(
        self,
        conversation_id: str,
        args: RequestCustomBuildArgs,
        resources: _EjectionResources,
    ) -> _SourceRevision:
        marker = await self._revision_capture.capture_workspace(
            conversation_id,
            trigger=APPKIT_EJECTION_SOURCE_TRIGGER,
            version_label=APPKIT_EJECTION_SOURCE_LABEL,
        )
        if not isinstance(marker, WorkspaceVersionEvent):
            raise RuntimeError("the governed source revision could not be captured")
        record = resources.store.set_version_pinned(conversation_id, marker.version_seq, True)
        await self._workspace.record_mutation_locked(
            conversation_id,
            "agent.appkit-ejection",
            paths=(APPKIT_EJECTION_PATH,),
        )
        manifest = self._manifest(
            args,
            source_version_seq=record.seq,
            source_tree_digest=record.tree_digest,
        )
        await resources.session.write_file(APPKIT_EJECTION_PATH, manifest)
        return _SourceRevision(record=record, manifest=manifest)

    async def _recover_source_revision(
        self,
        conversation_id: str,
        args: RequestCustomBuildArgs,
        resources: _EjectionResources,
        manifest: bytes,
    ) -> _SourceRevision:
        source_seq, source_digest = self._parse_recovery_manifest(manifest, args)
        record = resources.store.set_version_pinned(conversation_id, source_seq, True)
        if record.tree_digest != source_digest:
            raise RuntimeError("the retained governed revision no longer matches its digest")
        marker = await self._event_store.append(
            conversation_id,
            WorkspaceVersionEvent(
                version_seq=record.seq,
                tree_digest=record.tree_digest,
                trigger=APPKIT_EJECTION_SOURCE_TRIGGER,
            ),
        )
        if not isinstance(marker, WorkspaceVersionEvent):
            raise RuntimeError("the recovered governed revision marker is invalid")
        return _SourceRevision(record=record, manifest=manifest)

    async def _source_revision(
        self,
        conversation_id: str,
        args: RequestCustomBuildArgs,
        resources: _EjectionResources,
    ) -> _SourceRevision:
        try:
            manifest = await resources.session.read_file(APPKIT_EJECTION_PATH)
        except FileNotFoundError:
            return await self._capture_source_revision(conversation_id, args, resources)
        return await self._recover_source_revision(
            conversation_id,
            args,
            resources,
            manifest,
        )

    async def _target_record(
        self,
        conversation_id: str,
        resources: _EjectionResources,
        source: _SourceRevision,
    ) -> VersionRecord:
        marker = await self._revision_capture.capture_workspace(
            conversation_id,
            trigger=APPKIT_EJECTION_TARGET_TRIGGER,
            version_label=APPKIT_EJECTION_TARGET_LABEL,
        )
        if not isinstance(marker, WorkspaceVersionEvent):
            raise RuntimeError("the Freeform ejection revision could not be captured")
        target = resources.store.verify_version(conversation_id, marker.version_seq)
        if target.seq == source.record.seq or target.tree_digest == source.record.tree_digest:
            raise RuntimeError("the ejection did not create a distinct workspace revision")
        with resources.store.open_verified_version(conversation_id, target.seq) as verified:
            if verified.read_bytes(APPKIT_EJECTION_PATH) != source.manifest:
                raise RuntimeError("the ejection manifest is absent from the immutable revision")
        return target

    async def eject(
        self,
        conversation_id: str,
        call: ToolCall,
        *,
        tool_specs: Iterable[ToolSpec],
    ) -> AppKitEjectionReceipt:
        """Create, verify, expose, and audit one confirmed revision transition."""

        if call.tool_name != REQUEST_CUSTOM_BUILD:
            raise RuntimeError("unexpected AppKit ejection tool")
        self._require_workspace_authority(conversation_id)
        events = await self._event_store.get_events(conversation_id)
        if any(isinstance(event, AppKitEjectionEvent) for event in events):
            raise RuntimeError("this AppKit conversation has already been ejected")
        action = self._confirmed_action(events, call)
        args = RequestCustomBuildArgs.model_validate(call.arguments)
        resources = await self._resources(conversation_id, tool_specs=tool_specs)
        source = await self._source_revision(conversation_id, args, resources)
        target = await self._target_record(conversation_id, resources, source)
        receipt = AppKitEjectionReceipt(
            source_version_seq=source.record.seq,
            source_tree_digest=source.record.tree_digest,
            ejected_version_seq=target.seq,
            ejected_tree_digest=target.tree_digest,
            preview_version_seq=target.seq,
        )
        event = AppKitEjectionEvent(
            agent_view_id=action.agent_view_id,
            action_id=action.id,
            tool_call_id=call.call_id,
            source_version_seq=receipt.source_version_seq,
            source_tree_digest=receipt.source_tree_digest,
            ejected_version_seq=receipt.ejected_version_seq,
            ejected_tree_digest=receipt.ejected_tree_digest,
            lost_guarantees=receipt.lost_guarantees,
        )
        await self._transitions.transition_appkit_ejection_locked(
            conversation_id,
            event,
            prepared_record=resources.prepared_record,
        )
        return receipt


__all__ = ["AppKitEjectionLedger", "AppKitEjectionService"]
