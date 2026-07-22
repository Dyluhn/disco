"""Host-owned AppKit-to-Freeform revision transition."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

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
from disco.tools.appkit_scope import (
    APPKIT_EJECTION_PATH,
    APPKIT_EJECTION_SOURCE_LABEL,
    APPKIT_EJECTION_TARGET_LABEL,
    REQUEST_CUSTOM_BUILD,
    AppKitEjectionReceipt,
)
from disco.tools.builtin.request_custom_build import RequestCustomBuildArgs
from disco.tools.projects import StorageStatus


class AppKitEjectionService:
    """Cut the governed source and explicit Freeform target under one fence."""

    def __init__(self, runtime: Any) -> None:
        self._rt = runtime

    @staticmethod
    def _confirmed_action(events: Iterable[Event], call: ToolCall) -> ActionEvent:
        materialized = list(events)
        action = next(
            (
                event
                for event in reversed(materialized)
                if isinstance(event, ActionEvent)
                and event.tool_call.tool_name == REQUEST_CUSTOM_BUILD
                and event.tool_call.call_id == call.call_id
            ),
            None,
        )
        if action is None or type(action.seq) is not int or action.agent_view_id is None:
            raise RuntimeError("the persisted AppKit ejection action is unavailable")
        waiting = next(
            (
                event
                for event in materialized
                if isinstance(event, StatusEvent)
                and event.status is ConversationStatus.WAITING_FOR_CONFIRMATION
                and event.detail == action.id
                and type(event.seq) is int
                and event.seq > action.seq
            ),
            None,
        )
        resumed = next(
            (
                event
                for event in materialized
                if waiting is not None
                and isinstance(event, StatusEvent)
                and event.status is ConversationStatus.RUNNING
                and type(event.seq) is int
                and event.seq > (waiting.seq or -1)
                and event.agent_view_id == action.agent_view_id
            ),
            None,
        )
        if waiting is None or resumed is None:
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

    async def eject(
        self,
        conversation_id: str,
        call: ToolCall,
        *,
        tool_specs: Iterable[Any],
    ) -> AppKitEjectionReceipt:
        """Create, verify, expose, and audit one confirmed revision transition."""

        if call.tool_name != REQUEST_CUSTOM_BUILD:
            raise RuntimeError("unexpected AppKit ejection tool")
        lock = self._rt.workspace_lock(conversation_id)
        if not lock.locked() or not self._rt._workspace.fence_owned_by_current_task(
            conversation_id
        ):
            raise RuntimeError("AppKit ejection requires the workspace process fence")
        if self._rt._store.conversation_appkit_mode_sync(conversation_id) is not True:
            raise RuntimeError("the conversation is not governed by AppKit")
        events = await self._rt._store.get_events(conversation_id)
        if any(isinstance(event, AppKitEjectionEvent) for event in events):
            raise RuntimeError("this AppKit conversation has already been ejected")
        action = self._confirmed_action(events, call)
        args = RequestCustomBuildArgs.model_validate(call.arguments)

        store = self._rt._project_store_now()
        if store is None or store.status() is not StorageStatus.OK:
            raise RuntimeError("project storage is unavailable for the ejection revision")
        executor = self._rt._executors.get(conversation_id)
        session = getattr(executor, "sandbox", None)
        if session is None:
            raise RuntimeError("the AppKit workspace is unavailable for revision capture")
        prepared_record = await self._rt._build_platform.prepare_appkit_ejection_locked(
            conversation_id,
            tool_specs=tool_specs,
        )

        try:
            existing_manifest = await session.read_file(APPKIT_EJECTION_PATH)
        except FileNotFoundError:
            existing_manifest = None

        if existing_manifest is None:
            source_marker = await self._rt._lifecycle._capture_workspace(
                conversation_id,
                trigger=APPKIT_EJECTION_SOURCE_TRIGGER,
                version_label=APPKIT_EJECTION_SOURCE_LABEL,
            )
            if not isinstance(source_marker, WorkspaceVersionEvent):
                raise RuntimeError("the governed source revision could not be captured")
            source_record = store.set_version_pinned(
                conversation_id,
                source_marker.version_seq,
                True,
            )
            await self._rt._workspace.record_mutation_locked(
                conversation_id,
                "agent.appkit-ejection",
                paths=(APPKIT_EJECTION_PATH,),
            )
            manifest = self._manifest(
                args,
                source_version_seq=source_record.seq,
                source_tree_digest=source_record.tree_digest,
            )
            await session.write_file(APPKIT_EJECTION_PATH, manifest)
        else:
            source_seq, source_digest = self._parse_recovery_manifest(existing_manifest, args)
            source_record = store.set_version_pinned(conversation_id, source_seq, True)
            if source_record.tree_digest != source_digest:
                raise RuntimeError("the retained governed revision no longer matches its digest")
            source_marker = await self._rt._store.append(
                conversation_id,
                WorkspaceVersionEvent(
                    version_seq=source_record.seq,
                    tree_digest=source_record.tree_digest,
                    trigger=APPKIT_EJECTION_SOURCE_TRIGGER,
                ),
            )
            if not isinstance(source_marker, WorkspaceVersionEvent):
                raise RuntimeError("the recovered governed revision marker is invalid")
            manifest = existing_manifest

        target_marker = await self._rt._lifecycle._capture_workspace(
            conversation_id,
            trigger=APPKIT_EJECTION_TARGET_TRIGGER,
            version_label=APPKIT_EJECTION_TARGET_LABEL,
        )
        if not isinstance(target_marker, WorkspaceVersionEvent):
            raise RuntimeError("the Freeform ejection revision could not be captured")
        target_record = store.verify_version(conversation_id, target_marker.version_seq)
        if (
            target_record.seq == source_record.seq
            or target_record.tree_digest == source_record.tree_digest
        ):
            raise RuntimeError("the ejection did not create a distinct workspace revision")
        with store.open_verified_version(conversation_id, target_record.seq) as verified:
            if verified.read_bytes(APPKIT_EJECTION_PATH) != manifest:
                raise RuntimeError("the ejection manifest is absent from the immutable revision")

        receipt = AppKitEjectionReceipt(
            source_version_seq=source_record.seq,
            source_tree_digest=source_record.tree_digest,
            ejected_version_seq=target_record.seq,
            ejected_tree_digest=target_record.tree_digest,
            preview_version_seq=target_record.seq,
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
        await self._rt._build_platform.transition_appkit_ejection_locked(
            conversation_id,
            event,
            prepared_record=prepared_record,
        )
        return receipt


__all__ = ["AppKitEjectionService"]
