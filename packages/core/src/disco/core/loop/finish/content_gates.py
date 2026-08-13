"""Definition-of-Done, dictated-content, and execution-nudge gates.

`_ContentGateMixin` is the composed facade `FinishGate` binds to (see
`finish/__init__.py`); it must keep exposing exactly the same methods with
exactly the same behavior. The heavy, largely self-contained computations it
used to inline — deliverable byte reading, bounded app-bundle traversal, DoD
evaluator construction, plan-verifier failure fingerprinting, and the
model-facing notice text — now live in `content_gate_parts/` as pure
functions with explicit arguments (loop/sandbox/data in, result out). Each
mixin method that used to hold that logic is now a thin delegator so the
class stays a real orchestration surface rather than a place where policy
gets duplicated.
"""

from __future__ import annotations

import asyncio
import posixpath
from typing import TYPE_CHECKING, Any, Protocol, cast

from ...dod import FileExistsPredicate
from ...dod_evaluator import DoDEvaluator
from ...events import DeliverableEvent, Event, PlanEvent, StatusEvent
from ...llm import OperatingMode
from ..boundaries import AgentStep
from ..control import Disp
from ..plan_conditions import DictatedContentCondition, dictated_content_conditions_from_events
from .common import (
    _DICTATED_CONTENT_REFUSAL_CAP,
    _DOD_REFUSAL_CAP,
    _EXECUTION_NUDGE_CAP,
    _LOG,
    _appkit_scope_active,
    _deliverable_event_paths,
    _DoDWorkspaceUnavailable,
    _execution_nudge,
    _FinishGateComponent,
    _is_web_deliverable,
    _latest_app_deliverable_event,
    _latest_deliverable_event,
    _latest_plan_revision,
    _plan_file_exists_paths,
    _safe_deliverable_file_path,
    signals,
)
from .content_gate_parts.app_bundle import (
    _DICTATED_CONTENT_BUNDLE_MAX_DEPTH,
    _DICTATED_CONTENT_BUNDLE_MAX_DIRS,
    _DICTATED_CONTENT_BUNDLE_MAX_ENTRIES,
    _DICTATED_CONTENT_BUNDLE_MAX_ENTRIES_PER_DIR,
    _DICTATED_CONTENT_BUNDLE_MAX_FILES,
    _DICTATED_CONTENT_BUNDLE_SKIP_DIRS,
    bounded_app_bundle_paths,
    resolve_sandboxed_app_entry,
)
from .content_gate_parts.deliverable_bytes import read_deliverable_bytes
from .content_gate_parts.dictated_content_scan import (
    first_dictated_content_miss,
    is_text_candidate,
)
from .content_gate_parts.dod_evaluator_build import build_sandbox_dod_evaluator
from .content_gate_parts.errors import _DictatedContentInspectionIncomplete
from .content_gate_parts.notices import (
    emit_dangling_plan_evidence_notice,
    emit_dictated_content_cap_release_notice,
    emit_dictated_content_inspection_incomplete_notice,
    emit_dictated_content_refusal_notice,
    emit_dod_workspace_unavailable_notice,
    emit_execution_nudge,
    emit_external_dod_cap_pause_notice,
    emit_external_dod_unmet_notice,
    land_execution_nudge_exhausted,
)
from .content_gate_parts.plan_verifier_failure import (
    _failed_predicate_fingerprints,
)
from .verify_gate_parts.host_claims import governed_structured_browser_target

# These bundle-inspection limits and the plan-verifier fingerprint helper now
# live in `content_gate_parts` (their real owner boundary — see the modules
# above) and are no longer read by this module's own code. They are kept
# importable here under their original names since content_gates.py used to
# define them directly at module scope.
_REEXPORTED_CONTENT_GATE_PART_NAMES = (
    _DICTATED_CONTENT_BUNDLE_MAX_DEPTH,
    _DICTATED_CONTENT_BUNDLE_MAX_DIRS,
    _DICTATED_CONTENT_BUNDLE_MAX_ENTRIES,
    _DICTATED_CONTENT_BUNDLE_MAX_ENTRIES_PER_DIR,
    _DICTATED_CONTENT_BUNDLE_MAX_FILES,
    _DICTATED_CONTENT_BUNDLE_SKIP_DIRS,
    _failed_predicate_fingerprints,
)

if TYPE_CHECKING:
    from ..ports import ConversationModePort, FinishVerificationPort, LoopEventPort

    class _LoopFacet(ConversationModePort, FinishVerificationPort, LoopEventPort, Protocol):
        """The loop capability this module uses: conversation mode, finish verification, the event
        log.
        """


_DICTATED_CONTENT_BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".avi",
        ".doc",
        ".docx",
        ".eot",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".odt",
        ".ogg",
        ".otf",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".sqlite",
        ".sqlite3",
        ".tar",
        ".ttf",
        ".wasm",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".xls",
        ".xlsx",
        ".zip",
    }
)
_DICTATED_CONTENT_BUNDLE_WALL_CLOCK_S = 60.0
_DICTATED_CONTENT_MAX_FILE_BYTES = 32 * 1024 * 1024
_DICTATED_CONTENT_MAX_TOTAL_BYTES = 64 * 1024 * 1024


class _DictatedContentShortCircuitPass(Exception):
    """Internal signal: the gate passes NOW without touching the refusal counter.

    Distinct from "checked everything, nothing missing" (which DOES reset the
    refusal counter) — this fires only for the "not yet applicable" shortcuts
    (no selected app entry yet, no deliverable paths yet) that `dictated_content_gate_passed`
    used to `return True` from directly, mid-computation.
    """


def _dictated_content_inspection_cause(exc: BaseException) -> dict[str, str | int]:
    """Return a bounded, non-secret cause taxonomy for durable evidence."""

    cause = exc.__cause__
    if cause is None:
        return {}
    if isinstance(cause, FileNotFoundError):
        category = "file_not_found"
    elif isinstance(cause, PermissionError):
        category = "permission_denied"
    elif isinstance(cause, NotADirectoryError):
        category = "not_a_directory"
    elif isinstance(cause, IsADirectoryError):
        category = "is_a_directory"
    elif isinstance(cause, TimeoutError):
        category = "timeout"
    elif isinstance(cause, OSError):
        category = "os_error"
    else:
        category = "unclassified"
    detail: dict[str, str | int] = {"category": category}
    safe_errno_types = (
        OSError,
        FileNotFoundError,
        PermissionError,
        NotADirectoryError,
        IsADirectoryError,
        TimeoutError,
    )
    errno_value = (
        cast(OSError, cause).errno if type(cause) in safe_errno_types else None  # noqa: E721
    )
    if type(errno_value) is int and 1 <= errno_value <= 4095:  # noqa: E721
        detail["errno"] = errno_value
    return detail


def _dictated_content_text_candidate(path: str, data: bytes) -> bool:
    """Whether bytes may safely participate in a user-visible text floor.

    Delegates to `is_text_candidate` (the single owner of this rule); kept
    under its original name/signature since it is part of this module's
    existing surface.
    """

    return is_text_candidate(path, data, binary_suffixes=_DICTATED_CONTENT_BINARY_SUFFIXES)


def _selected_app_deliverable_present(events: list[Event]) -> bool:
    return any(
        isinstance(event, DeliverableEvent) and event.artifact_kind == "app" for event in events
    )


def _dictated_content_target_pending(events: list[Event]) -> bool:
    """Whether target selection must run before content can be scoped.

    Preview activity and a provisional web admission prove that an app exists,
    but they do not identify its entry file.  Only a current handoff does.  Let
    the target-shape gate request that handoff before inspecting content instead
    of guessing ``index.html`` and steering the agent toward the wrong file.
    """

    if governed_structured_browser_target(events):
        return _latest_app_deliverable_event(events) is None
    return _is_web_deliverable(events) and _latest_deliverable_event(events) is None


def _finish_alias(loop: _LoopFacet) -> str | None:
    return getattr(loop, "_finish_alias", None)


def _contract_required_paths_for_alias(alias: str) -> list[str]:
    """Required files of every registered contract whose finalizer matches `alias`."""

    try:
        from ...contract.registry import BuildContractRegistry

        reg = BuildContractRegistry.default()
        out: list[str] = []
        for kind in reg.kinds():
            c = reg.get(kind)
            if c is None or c.verify.finalizer != alias:
                continue
            for p in c.artifact.required_files:
                safe = _safe_deliverable_file_path(p)
                if safe is not None:
                    out.append(safe)
        return out
    except Exception:  # noqa: BLE001 — finish gate degrades to other path sources
        return []


async def _dictated_content_fallback_paths(loop: _LoopFacet, events: list[Event]) -> list[str]:
    """Deliverable-path sources used when no app has been explicitly selected yet."""

    paths: list[str] = []
    paths.extend(_plan_file_exists_paths(events))
    paths.extend(_deliverable_event_paths(events))
    spec = await loop.store.get_external_dod_spec(loop.conversation_id)
    if spec is not None:
        for pred in spec.predicates:
            if isinstance(pred, FileExistsPredicate):
                p = _safe_deliverable_file_path(pred.path)
                if p is not None:
                    paths.append(p)
    alias = _finish_alias(loop)
    if alias:
        paths.extend(_contract_required_paths_for_alias(alias))
    if _is_web_deliverable(events):
        paths.append("index.html")
    return paths


def _dedupe_normalized_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        norm = posixpath.normpath(path)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


class _ContentGateService(_FinishGateComponent):
    async def _dictated_content_selected_app_entry(self, events: list[Event]) -> str | None:
        latest_app = next(
            (
                event
                for event in reversed(events)
                if isinstance(event, DeliverableEvent) and event.artifact_kind == "app"
            ),
            None,
        )
        if latest_app is None:
            return None
        selected_entry = _safe_deliverable_file_path(latest_app.path)
        legacy_index = _safe_deliverable_file_path(latest_app.path, app_root=True)
        if selected_entry is None:
            selected_entry = legacy_index
        if selected_entry is None:
            raise _DictatedContentInspectionIncomplete(
                "the selected app handoff path is unsafe or invalid"
            )
        sbx = getattr(self._loop.executor, "sandbox", None)
        return await resolve_sandboxed_app_entry(sbx, selected_entry, legacy_index)

    def _contract_required_deliverable_paths(self) -> list[str]:
        """Best-effort bridge from a contract finalizer alias to required files."""

        alias = _finish_alias(self._loop)
        if not alias:
            return []
        return _contract_required_paths_for_alias(alias)

    async def _dictated_content_deliverable_paths(self, events: list[Event]) -> list[str]:
        """Primary deliverable files for dictated-content checking."""

        paths: list[str] = []
        selected_entry = await self._dictated_content_selected_app_entry(events)
        if selected_entry is not None:
            paths.append(selected_entry)
            # Once an app is explicitly selected, its resolved bundle remains the
            # authoritative app surface.  The current approved plan may also own
            # explicit deliverables outside that bundle (for example a deliberately
            # stale root scaffold beside ``release/index.html``).  Those files are
            # part of the same user-approved execution contract, unlike stale
            # handoffs or arbitrary workspace scratch, so retain them as bounded
            # sibling content evidence.
            paths.extend(await self._dictated_content_app_bundle_paths(events))
            paths.extend(_plan_file_exists_paths(events))
        else:
            paths.extend(await _dictated_content_fallback_paths(self._loop, events))
        return _dedupe_normalized_paths(paths)

    async def _dictated_content_app_bundle_paths(self, events: list[Event]) -> list[str]:
        try:
            async with asyncio.timeout(_DICTATED_CONTENT_BUNDLE_WALL_CLOCK_S):
                return await self._dictated_content_app_bundle_paths_within_deadline(events)
        except TimeoutError as exc:
            raise _DictatedContentInspectionIncomplete(
                "the app-bundle inspection exceeded its wall-clock limit"
            ) from exc

    async def _dictated_content_app_bundle_paths_within_deadline(
        self,
        events: list[Event],
    ) -> list[str]:
        """Resolve the handed-off app root, then delegate the bounded walk.

        The entry's directory is the bundle root; `bounded_app_bundle_paths`
        owns the actual traversal and every hard limit.
        """

        roots: list[str] = []
        entry = await self._dictated_content_selected_app_entry(events)
        if entry is not None:
            roots.append(posixpath.dirname(entry) or ".")
        if not roots:
            return []
        sbx = getattr(self._loop.executor, "sandbox", None)
        return await bounded_app_bundle_paths(
            sbx,
            roots,
            binary_suffixes=_DICTATED_CONTENT_BINARY_SUFFIXES,
            safe_path=_safe_deliverable_file_path,
        )

    async def _read_deliverable_bytes(self, path: str, *, strict: bool) -> bytes | None:
        """Read one deliverable file via the sandbox, else the host workspace path."""

        sbx = getattr(self._loop.executor, "sandbox", None)
        return await read_deliverable_bytes(
            sbx,
            path,
            strict=strict,
            max_file_bytes=_DICTATED_CONTENT_MAX_FILE_BYTES,
            log=_LOG,
        )

    async def _first_dictated_content_miss(
        self,
        conditions: list[DictatedContentCondition],
        paths: list[str],
        *,
        strict: bool,
    ) -> tuple[DictatedContentCondition, list[str]] | None:
        return await first_dictated_content_miss(
            conditions,
            paths,
            strict=strict,
            read_bytes=lambda path: self._read_deliverable_bytes(path, strict=strict),
            max_file_bytes=_DICTATED_CONTENT_MAX_FILE_BYTES,
            max_total_bytes=_DICTATED_CONTENT_MAX_TOTAL_BYTES,
            binary_suffixes=_DICTATED_CONTENT_BINARY_SUFFIXES,
            log=_LOG,
        )

    async def _dictated_content_compute_miss(
        self,
        events: list[Event],
        conditions: list[DictatedContentCondition],
        *,
        selected_app: bool,
    ) -> tuple[DictatedContentCondition, list[str]] | None:
        """Compute the first unmet condition, or raise the short-circuit-pass signal.

        Explicitly artifact-scoped conditions are checked against that exact
        artifact first. Remaining unscoped conditions use the selected entry as
        an existential fast path, widening to the full bundle only when needed.
        """

        targeted: dict[str, list[DictatedContentCondition]] = {}
        unscoped: list[DictatedContentCondition] = []
        for condition in conditions:
            if condition.document_artifact is None:
                unscoped.append(condition)
                continue
            target = _safe_deliverable_file_path(condition.document_artifact)
            if target is None:
                raise _DictatedContentInspectionIncomplete(
                    "a dictated-content artifact target is unsafe or invalid"
                )
            targeted.setdefault(target, []).append(condition)

        for target, target_conditions in targeted.items():
            miss = await self._first_dictated_content_miss(
                target_conditions,
                [target],
                strict=selected_app,
            )
            if miss is not None:
                return miss

        if not unscoped:
            return None

        if _dictated_content_target_pending(events):
            raise _DictatedContentShortCircuitPass

        if not selected_app:
            paths = await self._dictated_content_deliverable_paths(events)
            if not paths:
                raise _DictatedContentShortCircuitPass
            return await self._first_dictated_content_miss(unscoped, paths, strict=False)

        async with asyncio.timeout(_DICTATED_CONTENT_BUNDLE_WALL_CLOCK_S):
            selected_entry = await self._dictated_content_selected_app_entry(events)
            if selected_entry is None:
                raise _DictatedContentShortCircuitPass
            try:
                entry_miss = await self._first_dictated_content_miss(
                    unscoped,
                    [selected_entry],
                    strict=True,
                )
            except _DictatedContentInspectionIncomplete:
                entry_miss = (unscoped[0], [])
            if entry_miss is None:
                return None
            paths = await self._dictated_content_deliverable_paths(events)
            if not paths:
                raise _DictatedContentShortCircuitPass
            return await self._first_dictated_content_miss(unscoped, paths, strict=True)

    async def _dictated_content_report_inspection_incomplete(
        self, inspection_error: _DictatedContentInspectionIncomplete
    ) -> None:
        inspection_cause = _dictated_content_inspection_cause(inspection_error)
        _LOG.warning(
            "dictated-content inspection incomplete: %s; cause=%s",
            inspection_error,
            inspection_cause or "unavailable",
        )
        await emit_dictated_content_inspection_incomplete_notice(
            self._loop, inspection_error, inspection_cause
        )
        self._loop._pause_requested.set()

    async def _dictated_content_handle_miss(
        self,
        cond: DictatedContentCondition,
        checked_paths: list[str],
    ) -> bool:
        """Cap-bounded refusal: release loudly at the cap, else refuse and retry."""

        file_word = "file" if len(checked_paths) == 1 else "files"
        files = ", ".join(f"`{p}`" for p in checked_paths)
        if self._loop._dictated_content_refusals >= _DICTATED_CONTENT_REFUSAL_CAP:
            if self._loop._dictated_content_refusals == _DICTATED_CONTENT_REFUSAL_CAP:
                await emit_dictated_content_cap_release_notice(self._loop, cond, file_word, files)
                self._loop._dictated_content_refusals += 1
            return True
        self._loop._dictated_content_refusals += 1
        await emit_dictated_content_refusal_notice(self._loop, cond, file_word, files)
        return False

    async def dictated_content_gate_passed(self, events: list[Event]) -> bool:
        """REL-RC-O finish gate: quoted user literals must be present verbatim.

        Conditions are reconstructed from USER messages bound to PlanEvent
        revisions; the current revision inherits all prior ones.
        """

        if not self._loop._planning_tools or self._loop.mode == OperatingMode.PLANNING:
            return True
        if _appkit_scope_active(self._loop):
            self._loop._dictated_content_refusals = 0
            return True
        current_revision = _latest_plan_revision(events)
        if current_revision is None:
            return True
        conditions = [
            c
            for c in dictated_content_conditions_from_events(events)
            if c.revision <= current_revision
        ]
        if not conditions:
            self._loop._dictated_content_refusals = 0
            return True
        selected_app = _selected_app_deliverable_present(events)
        miss: tuple[DictatedContentCondition, list[str]] | None = None
        try:
            miss = await self._dictated_content_compute_miss(
                events, conditions, selected_app=selected_app
            )
        except _DictatedContentShortCircuitPass:
            return True
        except TimeoutError:
            inspection_error = _DictatedContentInspectionIncomplete(
                "the complete app-content inspection exceeded its wall-clock limit"
            )
        except _DictatedContentInspectionIncomplete as exc:
            inspection_error = exc
        else:
            inspection_error = None
        if inspection_error is not None:
            await self._dictated_content_report_inspection_incomplete(inspection_error)
            return False
        if miss is None:
            self._loop._dictated_content_refusals = 0
            return True
        cond, checked_paths = miss
        return await self._dictated_content_handle_miss(cond, checked_paths)

    async def _reject_dangling_plan_evidence(
        self, events: list[Event], plan: PlanEvent | None
    ) -> bool:
        """True (after pausing fail-closed) when approval exists without a plan."""

        approval_exists = any(
            isinstance(event, StatusEvent) and event.detail == "plan_approved" for event in events
        )
        if not (approval_exists and plan is None):
            return False
        _LOG.error(
            "Approved plan evidence for %s is dangling or fingerprint-mismatched; pausing.",
            self._loop.conversation_id,
        )
        await emit_dangling_plan_evidence_notice(self._loop)
        self._loop._pause_requested.set()
        return True

    async def _dod_evaluator_or_pause(self) -> DoDEvaluator | None:
        """Build the DoD evaluator; on workspace-unavailable, pause and return None."""

        try:
            return await self.build_dod_evaluator()
        except _DoDWorkspaceUnavailable as exc:
            _LOG.warning(
                "Acceptance verification for %s has no workspace evidence; pausing: %s",
                self._loop.conversation_id,
                exc,
            )
            await emit_dod_workspace_unavailable_notice(self._loop)
            self._loop._pause_requested.set()
            return None

    async def finish_dod_gate_passed(self) -> bool:
        """Evaluate immutable external requirements only.

        Plan ``done_condition`` values are agent-authored progress diagnostics. They
        remain available to the step-level advisory evaluator, but are not acceptance
        authority and cannot refuse finish. The external DoD is host-owned and remains
        fail-closed.
        """
        events = await self._loop._events()
        external_spec = await self._loop.store.get_external_dod_spec(self._loop.conversation_id)
        plan = signals.latest_approved_plan(events)
        if await self._reject_dangling_plan_evidence(events, plan):
            return False
        if external_spec is None:
            return True
        evaluator = await self._dod_evaluator_or_pause()
        if evaluator is None:
            return False

        external_verdict = await evaluator.evaluate(
            external_spec,
            conversation_id=self._loop.conversation_id,
        )
        if not external_verdict.passed:
            return await self._record_external_dod_failure(external_verdict)
        self._loop._dod_refusals = 0
        return True

    async def _record_external_dod_failure(self, verdict: Any) -> bool:
        """Fail closed on immutable external authority with a bounded pause."""
        if self._loop._dod_refusals >= _DOD_REFUSAL_CAP:
            _LOG.warning(
                "External DoD for %s still unmet after %d refusals (cap %d) — pausing "
                "fail-closed instead of auto-releasing.",
                self._loop.conversation_id,
                self._loop._dod_refusals,
                _DOD_REFUSAL_CAP,
            )
            await emit_external_dod_cap_pause_notice(self._loop)
            self._loop._pause_requested.set()
            return False
        self._loop._dod_refusals += 1
        await emit_external_dod_unmet_notice(self._loop, verdict)
        return False

    async def build_dod_evaluator(self) -> DoDEvaluator:
        """Construct the DoDEvaluator.

        Two paths: a test-seam factory (`_dod_evaluator_factory`, the
        dependency-injection point — tests close over a fake command_runner /
        http_probe there), or production, where `build_sandbox_dod_evaluator`
        resolves the sandbox's own file/command/HTTP surfaces from the executor.
        """
        if self._loop._dod_evaluator_factory is not None:
            # The factory is an async-callable in the common case (tests
            # want to close over a `tmp_path` + fakes without performing
            # any I/O at construction time), but a sync callable is also
            # accepted — production callers may want to keep the
            # construction cheap. Awaiting a non-awaitable raises
            # TypeError, which the gate's `_DoDWorkspaceUnavailable`-
            # style `try/except` doesn't catch; the explicit
            # `inspect.iscoroutine` check keeps both shapes working.
            import inspect

            result = self._loop._dod_evaluator_factory()
            if inspect.iscoroutine(result):
                result = await result
            # inspect.iscoroutine is a TypeGuard (narrows only the positive
            # branch), so the awaited-away Coroutine lingers in the static type;
            # at runtime `result` is always the resolved DoDEvaluator here.
            return cast(DoDEvaluator, result)
        return await build_sandbox_dod_evaluator(self._loop.executor)

    async def gate_execution_nudge(self, step: AgentStep, events: list[Event]) -> Disp:
        # PLAN-MODE EXECUTION GATE — a forcing function, NOT a prompt. If
        # the loop is in execution mode (planning_tools configured) and the
        # agent declares "done" without any productive action since plan
        # approval, refuse the finish: append an IMPLICIT system-reminder
        # and re-enter the loop. W5: capped at _EXECUTION_NUDGE_CAP (3) for
        # parity with the other finish-path gates — after cap the gate
        # RELEASES with a visible warning rather than running forever.
        if (
            self._loop._planning_tools  # plan-first lifecycle is configured
            and self._loop.mode != OperatingMode.PLANNING  # we're executing
            and not signals.productive_action_since_approval(events)
            and not signals.finish_intent_replan_after_prior_productive_work(events)
            and not signals.verifier_repair_execution_active(events)
        ):
            # W5 cap: after _EXECUTION_NUDGE_CAP nudges without productive
            # action, TERMINALIZE the run as a FAILURE — NOT a false FINISHED.
            # Spec §11.2: after approval, execution must produce ≥1 action OR
            # a terminal explicit failure. A plan that is approved but never
            # executed is the latter, so land STUCK (bounded no-progress /
            # model-stall — same family as the other no-progress terminals;
            # ERROR is reserved for thrown exceptions). The cap is the
            # terminal exit for this path: HALT so the engine exits the loop
            # and `get_state()` surfaces STUCK/approve_plan_no_execution.
            if self._loop._execution_nudges >= _EXECUTION_NUDGE_CAP:
                _LOG.warning(
                    "execution-nudge cap (%d) reached for %s — plan approved but "
                    "never executed; terminalizing STUCK (approve_plan_no_execution).",
                    _EXECUTION_NUDGE_CAP,
                    self._loop.conversation_id,
                )
                await land_execution_nudge_exhausted(self._loop, self._loop._execution_nudges)
                return Disp.HALT

            self._loop._execution_nudges += 1  # telemetry + cap counter
            # Surface the model's reasoning before nudging (don't discard
            # it — mirror the plan-nudge sibling); an empty finish-step
            # persists nothing, so it's invisible to every event-derived
            # detector and the instance counter has to carry it (the (g)
            # no-op path does the same).
            nudge = _execution_nudge(self._loop._execution_nudges, _EXECUTION_NUDGE_CAP)
            if not await emit_execution_nudge(self._loop, step.thought, nudge):
                self._loop._invisible_steps += 1
            # The execution-nudge cap (_EXECUTION_NUDGE_CAP) is the
            # backstop for this path: after N nudges the gate emits
            # FINISHED and halts above. The old noop valve call has
            # been removed — it fired at _ACTIONLESS_BREAK_CAP (3),
            # same count as the cap, and would PAUSED the run before
            # the cap's FINISHED release could land (W5 fix).
            return Disp.CONTINUE
        # Productive action found — reset the nudge streak so a fresh
        # plan-approval cycle gets a full _EXECUTION_NUDGE_CAP budget.
        self._loop._execution_nudges = 0
        return Disp.FALLTHROUGH
