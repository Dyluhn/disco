"""Typed boundaries for Build Soak transport, orchestration, and retention."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .adapters.disco_api import CollectedRun, InfraProbeError
    from .efficiency import LiveEfficiencyProgress


class ScenarioDriver(Protocol):
    """Drive one scenario and return collected facts."""

    async def drive(
        self,
        client: ProductClient,
        scenario: dict[str, Any],
        *,
        model: str | None,
        autonomous: bool,
        timeout_s: float,
        hard_cap_s: float,
        seed: int | None,
    ) -> CollectedRun: ...


class ConversationClient(Protocol):
    """Conversation controls consumed by the scenario driver."""

    last_conversation_id: str | None
    scenario_evidence: dict[str, Any]

    async def pre_create_probe(self, model: str | None) -> None: ...

    async def create_build_conversation(
        self,
        prompt: str,
        *,
        model: str | None = None,
        autonomous: bool = False,
        appkit: bool = False,
        surface: str = "build",
        import_fixture: dict[str, Any] | None = None,
        verification_requirements: dict[str, Any] | None = None,
    ) -> str: ...

    async def approve_plan(self, conversation_id: str) -> None: ...

    async def confirm(self, conversation_id: str) -> None: ...

    async def resolve_decision(
        self,
        conversation_id: str,
        *,
        preferred_option_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    async def resume(self, conversation_id: str) -> dict[str, Any]: ...

    async def pause(self, conversation_id: str) -> None: ...

    async def restore_workspace_version(
        self,
        conversation_id: str,
        *,
        selector: str = "oldest",
    ) -> dict[str, Any]: ...

    async def download_project(self, conversation_id: str) -> tuple[int, bytes]: ...

    async def kill(self, conversation_id: str) -> dict[str, Any]: ...

    async def send_followup(
        self,
        conversation_id: str,
        text: str,
        *,
        kind: str = "message",
    ) -> None: ...

    async def get_state(self, conversation_id: str) -> dict[str, Any]: ...


class ProgressClient(Protocol):
    """Progress-aware waits and serialized follow-up controls."""

    async def poll_until_terminal_or_gate(
        self,
        conversation_id: str,
        *,
        inactivity_s: float,
        hard_cap_s: float,
        min_terminal_seq: int | None = None,
    ) -> str: ...

    async def poll_until_terminal(
        self,
        conversation_id: str,
        *,
        inactivity_s: float,
        hard_cap_s: float,
        min_terminal_seq: int | None = None,
    ) -> str: ...

    async def wait_until_status_leaves(
        self,
        conversation_id: str,
        status: str,
        *,
        timeout_s: float,
    ) -> str: ...

    async def capture_followup_baseline(
        self,
        conversation_id: str,
    ) -> dict[str, Any]: ...

    async def wait_for_followup_pickup(
        self,
        conversation_id: str,
        baseline: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> str: ...

    async def wait_for_first_file_write(
        self,
        conversation_id: str,
        *,
        timeout_s: float,
    ) -> int | None: ...

    def latest_user_message_seq(self, conversation_id: str) -> int: ...

    async def wait_for_new_user_message_seq(
        self,
        conversation_id: str,
        *,
        after_seq: int,
        timeout_s: float,
    ) -> int | None: ...

    def _latest_terminal_seq(self, conversation_id: str) -> int: ...


class ObservabilityClient(Protocol):
    """Live observability operations used during one run."""

    @property
    def live_thrash_monitor(self) -> dict[str, Any]: ...

    def enable_live_thrash_monitor(self, scenario: dict[str, Any]) -> None: ...

    def enable_efficiency_progress(
        self,
        progress: LiveEfficiencyProgress,
        *,
        ledger_path: str | None = None,
    ) -> None: ...

    def observe_live_thrash_snapshot(
        self,
        events: list[dict[str, Any]],
        inspect_trace: dict[str, Any] | None,
        *,
        terminal_status: str = "",
    ) -> bool: ...

    async def finish_inspect_collection(
        self,
        conversation_id: str,
    ) -> dict[str, Any] | None: ...

    async def collect_inspect_trace(
        self,
        conversation_id: str,
    ) -> dict[str, Any] | None: ...

    def note_inspect_stop_unconfirmed(self, conversation_id: str) -> None: ...

    def note_expected_stack_restart(self, conversation_id: str) -> None: ...


class EvidenceSourceClient(Protocol):
    """Product facts collected before dossier retention."""

    def collect_events(self, conversation_id: str) -> list[dict[str, Any]]: ...

    async def collect_workspace(
        self,
        conversation_id: str,
        file_paths: list[str],
    ) -> dict[str, Any]: ...

    async def workspace_snapshot_digest(
        self,
        conversation_id: str,
        file_paths: list[str],
    ) -> str | None: ...

    def collect_browser_evidence(
        self,
        conversation_id: str,
        events: list[dict[str, Any]],
        workspace_manifest: dict[str, Any],
        *,
        require_verified_host_screenshot: bool = False,
        horizon_seq: int | None = None,
    ) -> dict[str, bytes]: ...

    async def freeze_progressing_workspace(
        self,
        conversation_id: str,
        declared: list[str],
        *,
        deadline_s: float = 90.0,
    ) -> dict[str, Any]: ...

    def collect_paused_workspace(
        self,
        conversation_id: str,
        declared: list[str],
    ) -> dict[str, Any]: ...

    async def collect_preview(self, conversation_id: str) -> dict[str, Any]: ...


class ProductClient(
    ConversationClient,
    ProgressClient,
    ObservabilityClient,
    EvidenceSourceClient,
    Protocol,
):
    """Composite product boundary; intentionally adds no operations."""


class EvidenceSink(Protocol):
    """Retain facts without selecting a verdict."""

    def assemble_dossier(
        self,
        out_root: str | Path,
        run_id: str,
        scenario: dict[str, Any],
        run: CollectedRun,
        *,
        model: str | None,
        autonomous: bool,
        commit: str = "",
        repo_revision: str = "",
        repo_dirty: bool = False,
        seed: int | None = None,
        mode: str = "api",
        kernel: str = "disco",
        started_at: str | None = None,
        provider_ledger: list[dict[str, Any]] | None = None,
    ) -> Path: ...

    def record_infra_failure(
        self,
        out_root: str | Path,
        run_id: str,
        scenario: dict[str, Any],
        exc: InfraProbeError,
    ) -> dict[str, Any]: ...

    def record_invalid_run(
        self,
        out_root: str | Path,
        run_id: str,
        scenario: dict[str, Any],
        reason: str,
        *,
        code: str = "RUN_INTERRUPTED",
        first_broken_link: str = "drive -> evidence_collection",
        facts: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        timeline_markdown: str | None = None,
    ) -> dict[str, Any]: ...

    def record_finish_unsealable(
        self,
        out_root: str | Path,
        run_id: str,
        scenario: dict[str, Any],
        reason: str,
        *,
        facts: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        timeline_markdown: str | None = None,
    ) -> dict[str, Any]: ...

    def write_batch_summary(
        self,
        path: Path,
        summary: dict[str, Any],
    ) -> Path: ...
