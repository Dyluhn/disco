"""Definition-of-Done, dictated-content, and execution-nudge gates."""

# ruff: noqa: F403,F405 -- mixin split intentionally shares the common import surface
from __future__ import annotations

import asyncio

from .common import *
from .common import (
    _DICTATED_CONTENT_REFUSAL_CAP,
    _DOD_REFUSAL_CAP,
    _EXECUTION_NUDGE,
    _EXECUTION_NUDGE_CAP,
    _LOG,
    _appkit_scope_active,
    _deliverable_event_paths,
    _DoDWorkspaceUnavailable,
    _FinishGateProto,
    _is_web_deliverable,
    _latest_plan_revision,
    _plan_file_exists_paths,
    _safe_deliverable_file_path,
)

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
_DICTATED_CONTENT_BUNDLE_SKIP_DIRS = frozenset(
    {
        ".disco",
        ".git",
        ".pmx",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
        "vendor",
    }
)
_DICTATED_CONTENT_BUNDLE_MAX_DIRS = 64
_DICTATED_CONTENT_BUNDLE_MAX_FILES = 512
_DICTATED_CONTENT_BUNDLE_MAX_DEPTH = 8
_DICTATED_CONTENT_BUNDLE_MAX_ENTRIES_PER_DIR = 256
_DICTATED_CONTENT_BUNDLE_MAX_ENTRIES = 1024
_DICTATED_CONTENT_BUNDLE_WALL_CLOCK_S = 60.0
_DICTATED_CONTENT_MAX_FILE_BYTES = 32 * 1024 * 1024
_DICTATED_CONTENT_MAX_TOTAL_BYTES = 64 * 1024 * 1024


class _DictatedContentInspectionIncomplete(RuntimeError):
    """The gate could not inspect the declared app scope completely and safely."""


def _dictated_content_text_candidate(path: str, data: bytes) -> bool:
    """Whether bytes may safely participate in a user-visible text floor.

    Known binary extensions are always excluded. Unknown extensions must still
    be valid NUL-free UTF-8, which protects binary artifacts without preventing
    extensionless or uncommon text deliverables from carrying dictated copy.
    Empty non-binary files remain candidates so missing content fails loudly.
    """

    if posixpath.splitext(path)[1].lower() in _DICTATED_CONTENT_BINARY_SUFFIXES:
        return False
    if not data:
        return True
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return False
    return True


class _ContentGateMixin(_FinishGateProto):
    def _contract_required_deliverable_paths(self) -> list[str]:
        """Best-effort bridge from a contract finalizer alias to required files.

        The loop only stores the finalizer alias, not the whole contract. When
        present, match it against the registry and reuse the contract's required
        files as primary deliverable candidates. No alias/no match is inert.
        """

        alias = getattr(self._loop, "_finish_alias", None)
        if not alias:
            return []
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

    async def _dictated_content_deliverable_paths(self, events: list[Event]) -> list[str]:
        """Primary deliverable files for dictated-content checking.

        Sources are files named by Plan/DoD/contract declarations, explicit handoff
        events, the existing web convention, and a strictly bounded resolved traversal
        of an explicitly handed-off app root. Arbitrary workspace trees remain out of
        scope.
        """

        paths: list[str] = []
        latest_app = next(
            (
                event
                for event in reversed(events)
                if isinstance(event, DeliverableEvent) and event.artifact_kind == "app"
            ),
            None,
        )
        if latest_app is not None:
            selected_entry = _safe_deliverable_file_path(latest_app.path, app_root=True)
            if selected_entry is None:
                raise _DictatedContentInspectionIncomplete(
                    "the selected app handoff path is unsafe or invalid"
                )
            paths.append(selected_entry)
            # Once an app is explicitly selected, its resolved bundle is authoritative.
            # Stale handoffs, scratch artifacts, and unrelated plan files cannot satisfy
            # content requirements for the current selected app.
            paths.extend(await self._dictated_content_app_bundle_paths(events))
        else:
            paths.extend(_plan_file_exists_paths(events))
            paths.extend(_deliverable_event_paths(events))
            spec = await self._loop.store.get_dod_spec(self._loop.conversation_id)
            if spec is not None:
                for pred in spec.predicates:
                    if isinstance(pred, FileExistsPredicate):
                        p = _safe_deliverable_file_path(pred.path)
                        if p is not None:
                            paths.append(p)
            paths.extend(self._contract_required_deliverable_paths())
            if _is_web_deliverable(events):
                paths.append("index.html")

        out: list[str] = []
        seen: set[str] = set()
        for path in paths:
            norm = posixpath.normpath(path)
            if norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
        return out

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
        """Boundedly enumerate text candidates belonging to handed-off app roots.

        A multi-file app's explicit handoff names its entry file, not every external
        CSS/JS/SVG/service-worker sibling. Treat that entry's directory as the bundle
        root while excluding dependency, VCS, runtime-state, hidden, and known-binary
        paths. The hard limits keep a large project from turning a finish check into an
        unbounded workspace crawl.
        """

        roots: list[str] = []
        latest_app = next(
            (
                event
                for event in reversed(events)
                if isinstance(event, DeliverableEvent) and event.artifact_kind == "app"
            ),
            None,
        )
        if latest_app is not None:
            entry = _safe_deliverable_file_path(latest_app.path, app_root=True)
            if entry is not None:
                roots.append(posixpath.dirname(entry) or ".")
        if not roots:
            return []

        sbx = getattr(self._loop.executor, "sandbox", None)
        required = ("list_dir_bounded", "resolve_relpath")
        if sbx is None or any(not hasattr(sbx, method) for method in required):
            raise _DictatedContentInspectionIncomplete(
                "the sandbox lacks bounded app-bundle inspection APIs"
            )

        for root in roots:
            try:
                resolved_root = posixpath.normpath(str(await sbx.resolve_relpath(root)))
            except Exception as exc:  # noqa: BLE001 — becomes visible unverifiable evidence
                raise _DictatedContentInspectionIncomplete(
                    "the selected app root could not be resolved safely"
                ) from exc
            if resolved_root != posixpath.normpath(root):
                raise _DictatedContentInspectionIncomplete(
                    "the selected app root resolves through an alias"
                )

        files: list[str] = []
        visited_dirs: set[str] = set()
        stack = [(root, 0) for root in reversed(roots)]
        entries_seen = 0
        while stack:
            directory, depth = stack.pop()
            if directory in visited_dirs:
                continue
            if len(visited_dirs) >= _DICTATED_CONTENT_BUNDLE_MAX_DIRS:
                raise _DictatedContentInspectionIncomplete(
                    "the app bundle exceeds the directory inspection limit"
                )
            visited_dirs.add(directory)
            try:
                entries, truncated = await sbx.list_dir_bounded(
                    directory,
                    _DICTATED_CONTENT_BUNDLE_MAX_ENTRIES_PER_DIR,
                )
            except Exception as exc:  # noqa: BLE001 — becomes visible unverifiable evidence
                raise _DictatedContentInspectionIncomplete(
                    "an app-bundle directory could not be listed safely"
                ) from exc
            if truncated:
                raise _DictatedContentInspectionIncomplete(
                    "an app-bundle directory exceeds the entry inspection limit"
                )
            entries_seen += len(entries)
            if entries_seen > _DICTATED_CONTENT_BUNDLE_MAX_ENTRIES:
                raise _DictatedContentInspectionIncomplete(
                    "the app bundle exceeds the total entry inspection limit"
                )
            for raw_name, entry_kind in entries:
                name = str(raw_name)
                if (
                    not name
                    or name in {".", ".."}
                    or name.startswith(".")
                    or name in _DICTATED_CONTENT_BUNDLE_SKIP_DIRS
                    or "/" in name
                    or "\x00" in name
                    or any(ord(character) < 32 or ord(character) == 127 for character in name)
                ):
                    continue
                child = posixpath.normpath(posixpath.join(directory, name))
                safe = _safe_deliverable_file_path(child)
                if safe is None:
                    continue
                if entry_kind == "other":
                    continue
                if entry_kind == "file":
                    if posixpath.splitext(safe)[1].lower() in _DICTATED_CONTENT_BINARY_SUFFIXES:
                        continue
                    if safe not in files:
                        files.append(safe)
                    if len(files) > _DICTATED_CONTENT_BUNDLE_MAX_FILES:
                        raise _DictatedContentInspectionIncomplete(
                            "the app bundle exceeds the file inspection limit"
                        )
                elif entry_kind != "directory":
                    raise _DictatedContentInspectionIncomplete(
                        "an app-bundle entry has an invalid type classification"
                    )
                elif depth >= _DICTATED_CONTENT_BUNDLE_MAX_DEPTH:
                    raise _DictatedContentInspectionIncomplete(
                        "the app bundle exceeds the depth inspection limit"
                    )
                else:
                    stack.append((safe, depth + 1))
        return files

    async def _read_deliverable_bytes(self, path: str) -> bytes | None:
        """Read one deliverable file, returning None only when no host/sandbox
        read surface is available. Missing/empty files return b"" so the content
        condition fails loudly against the named file."""

        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is not None and hasattr(sbx, "read_file"):
            try:
                data = await sbx.read_file(path)
            except FileNotFoundError:
                return b""
            except Exception as exc:  # noqa: BLE001 — becomes visible unverifiable evidence
                raise _DictatedContentInspectionIncomplete(
                    "a declared deliverable could not be read safely"
                ) from exc
            if isinstance(data, bytes):
                return data
            return str(data).encode("utf-8", "surrogatepass")

        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        if not workspace:
            return None
        from pathlib import Path

        try:
            root = Path(workspace).resolve()
            candidate = (root / path).resolve()
            root_s = str(root)
            cand_s = str(candidate)
            if not (cand_s == root_s or cand_s.startswith(root_s.rstrip("/") + "/")):
                return b""
            if not candidate.is_file():
                return b""
            if candidate.stat().st_size > _DICTATED_CONTENT_MAX_FILE_BYTES:
                raise _DictatedContentInspectionIncomplete(
                    "a text deliverable exceeds the per-file inspection budget"
                )
            return candidate.read_bytes()
        except OSError as exc:
            raise _DictatedContentInspectionIncomplete(
                "a declared deliverable could not be read from the workspace"
            ) from exc

    async def _first_dictated_content_miss(
        self,
        conditions: list[DictatedContentCondition],
        paths: list[str],
    ) -> tuple[DictatedContentCondition, list[str]] | None:
        unresolved = list(conditions)
        checked: list[str] = []
        total_bytes = 0
        for path in paths:
            if posixpath.splitext(path)[1].lower() in _DICTATED_CONTENT_BINARY_SUFFIXES:
                continue
            data = await self._read_deliverable_bytes(path)
            if data is None:
                continue
            if len(data) > _DICTATED_CONTENT_MAX_FILE_BYTES:
                raise _DictatedContentInspectionIncomplete(
                    "a text deliverable exceeds the per-file inspection budget"
                )
            total_bytes += len(data)
            if total_bytes > _DICTATED_CONTENT_MAX_TOTAL_BYTES:
                raise _DictatedContentInspectionIncomplete(
                    "the app bundle exceeds the total byte inspection budget"
                )
            if not _dictated_content_text_candidate(path, data):
                continue
            checked.append(path)
            unresolved = [
                condition
                for condition in unresolved
                if condition.literal.encode("utf-8", "surrogatepass") not in data
            ]
            if not unresolved:
                return None
        if not checked:
            raise _DictatedContentInspectionIncomplete(
                "no readable text deliverable surface was available"
            )
        return (unresolved[0], checked) if unresolved else None

    async def dictated_content_gate_passed(self, events: list[Event]) -> bool:
        """REL-RC-O finish gate: quoted user literals must be present verbatim.

        Conditions are reconstructed from USER messages bound to PlanEvent
        revisions. The current revision inherits all prior revisions, so a later
        phase cannot silently drop content quoted earlier in the conversation.
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
        inspection_error: _DictatedContentInspectionIncomplete | None = None
        miss: tuple[DictatedContentCondition, list[str]] | None = None
        try:
            async with asyncio.timeout(_DICTATED_CONTENT_BUNDLE_WALL_CLOCK_S):
                paths = await self._dictated_content_deliverable_paths(events)
                if not paths:
                    return True
                miss = await self._first_dictated_content_miss(conditions, paths)
        except TimeoutError:
            inspection_error = _DictatedContentInspectionIncomplete(
                "the complete app-content inspection exceeded its wall-clock limit"
            )
        except _DictatedContentInspectionIncomplete as exc:
            inspection_error = exc
        if inspection_error is not None:
            _LOG.warning("dictated-content inspection incomplete: %s", inspection_error)
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail="dictated_content_inspection_incomplete",
                )
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\nThe dictated-content finish check could not "
                            "inspect the selected app completely: "
                            f"{inspection_error}. The task is NOT "
                            "complete and has been paused fail-closed; repair the workspace "
                            "or sandbox evidence surface before finishing.\n</system-reminder>"
                        ),
                    ),
                )
            )
            self._loop._pause_requested.set()
            return False
        if miss is None:
            self._loop._dictated_content_refusals = 0
            return True

        cond, checked_paths = miss
        file_word = "file" if len(checked_paths) == 1 else "files"
        files = ", ".join(f"`{p}`" for p in checked_paths)
        literal = cond.literal

        if self._loop._dictated_content_refusals >= _DICTATED_CONTENT_REFUSAL_CAP:
            if self._loop._dictated_content_refusals == _DICTATED_CONTENT_REFUSAL_CAP:
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail="dictated_content_release",
                    )
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "⚠ Finished despite missing dictated content after "
                                f"{self._loop._dictated_content_refusals} refusals: "
                                f"literal {literal!r} from plan revision {cond.revision} "
                                f"still was not found in deliverable {file_word} {files}. "
                                "Releasing the finish gate to avoid an unbounded loop; "
                                "the deliverable may fail content review."
                            ),
                        ),
                    )
                )
                self._loop._dictated_content_refusals += 1
            return True

        self._loop._dictated_content_refusals += 1
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "You called finish, but a quoted user literal is missing "
                        f"from the deliverable {file_word} {files}: {literal!r}.\n\n"
                        f"This literal was dictated in the user instruction for plan "
                        f"revision {cond.revision} and is carried forward into the "
                        "current revision. The match is case-sensitive and exact. "
                        "It needs to appear in one appropriate text deliverable, not "
                        "in every listed file. Never add text to a binary asset. Update "
                        "a suitable text deliverable so it contains that exact text, then "
                        "finish again.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return False

    async def finish_dod_gate_passed(self) -> bool:
        """C1c DoD gate. Returns True iff the finish should be allowed.

        Algorithm (in order):
          1. Read the DoD spec from the store. None → no spec → True
             (LEGACY BYTE-IDENTICAL PATH — no events emitted, no state
             changed, control flow identical to pre-C1c).
          2. Build a DoDEvaluator. Factory-injected if the loop was
             constructed with one; otherwise build a default over the
             executor's sandbox evidence APIs. Missing evidence pauses
             fail-closed rather than turning the requirement into a pass.
          3. Run the verdict against the spec.
          4. Verdict passed → True (the finish lands).
          5. Verdict failed → emit a MessageEvent carrying the SPECIFIC
             unmet predicates (visible to the agent AND the audit), bump
             the refusal streak, return False so the caller `continue`s.

        Visible: the refusal is a `<system-reminder>` MessageEvent with the
        spec fingerprint, the number of unmet predicates, and a per-
        predicate line naming kind + reason. Never silent: a refused finish
        ALWAYS leaves a trace event. Same shape as verify-on-finish's
        refusal, distinct content (the predicates are external, not the
        agent's own command)."""
        spec = await self._loop.store.get_dod_spec(self._loop.conversation_id)
        if spec is None:
            # LEGACY: no DoD spec for this conversation → the gate is a
            # no-op. Today's finish path is reproduced EXACTLY — no events,
            # no state change, no log query beyond a single SELECT. The
            # read is observable as a side-effect-free DB query; it does
            # NOT change the events, status transitions, or final state.
            return True
        # Spec exists → run the evaluator. The factory seam is the test
        # injection point (fakes for command_runner / http_probe); the
        # default builds a real DoDEvaluator over the executor's workspace.
        try:
            evaluator = await self.build_dod_evaluator()
        except _DoDWorkspaceUnavailable as exc:
            # S-W5 D4: absence of the evidence surface cannot turn a committed
            # acceptance bar into a pass. Pause visibly; an operator can repair
            # the backend or begin a new conversation, but the model cannot earn
            # FINISHED by making verification unavailable.
            _LOG.warning(
                "DoD spec set for %s but no workspace_root available; pausing fail-closed: %s",
                self._loop.conversation_id,
                exc,
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\nThe external Definition-of-Done "
                            "could not be evaluated because its sandbox evidence surface "
                            "is unavailable. The task is NOT complete and has been paused "
                            "fail-closed; repair the sandbox or start a new conversation.\n"
                            "</system-reminder>"
                        ),
                    ),
                )
            )
            self._loop._pause_requested.set()
            return False
        verdict = await evaluator.evaluate(spec, conversation_id=self._loop.conversation_id)
        if verdict.passed:
            self._loop._dod_refusals = 0  # clean pass → reset the streak (mirror verify)
            return True
        # Bound the model retry loop without weakening the acceptance bar. Once
        # the refusal budget is spent, PAUSE for the user; never auto-release to
        # FINISHED. Unverifiable is still unmet—breaking the checker is not a pass.
        if self._loop._dod_refusals >= _DOD_REFUSAL_CAP:
            _LOG.warning(
                "DoD for %s still unmet after %d refusals (cap %d) — pausing "
                "fail-closed instead of auto-releasing.",
                self._loop.conversation_id,
                self._loop._dod_refusals,
                _DOD_REFUSAL_CAP,
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\nThe external Definition-of-Done is "
                            "still unmet after the bounded retry budget. The task is NOT "
                            "complete. This run is pausing for explicit user review rather "
                            "than silently marking incomplete work done.\n</system-reminder>"
                        ),
                    ),
                    meta={"blocking": "dod_unmet"},
                )
            )
            self._loop._pause_requested.set()
            return False
        # Refuse + keep working. The agent sees the SPECIFIC unmet
        # predicates (named by `kind` + the frozen-predicate `repr`); the
        # audit sees the spec fingerprint + the per-predicate results.
        self._loop._dod_refusals += 1  # bounded by _DOD_REFUSAL_CAP (see above)
        unmet_lines: list[str] = []
        for result in verdict.results:
            if result.passed:
                continue
            # The frozen predicate's repr names kind + fields. Pair with
            # the verdict's reason (the human explanation).
            label = "could not verify" if result.unverifiable else "reason"
            unmet_lines.append(f"  - {result.predicate!r}\n      {label}: {result.reason}")
        unmet_block = "\n".join(unmet_lines) if unmet_lines else "  - (no per-predicate results)"
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "You called finish, but the external Definition-of-Done "
                        f"evaluator found {len(verdict.unmet)} unmet acceptance "
                        f"predicate(s) (spec fingerprint {verdict.spec_fingerprint}):\n\n"
                        f"{unmet_block}\n\n"
                        "The task is NOT complete. These predicates were captured at "
                        "task start and live outside the agent's tool surface — you "
                        "cannot edit them, you can only satisfy them. Fix what they "
                        "surface (the predicates name the gap), then finish again.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return False

    async def build_dod_evaluator(self) -> DoDEvaluator:
        """Construct the DoDEvaluator. Two paths:

          * `_dod_evaluator_factory` is set (test seam): call it, ignore args.
          * Otherwise: use the sandbox's own file/command/HTTP surfaces. A host
            workspace path is optional; container backends are graded inside
            their namespace rather than silently skipping the gate.

        The factory is the dependency-injection point — tests close over
        a tmp_path + fake command_runner / http_probe and return a fully
        configured `DoDEvaluator`. Production callers leave the factory
        None and the engine does the workspace resolution here.
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
        sbx = getattr(self._loop.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        sandbox_evidence = sbx is not None and all(
            hasattr(sbx, method) for method in ("file_exists", "exec_shell", "resolve_relpath")
        )
        if not workspace and not sandbox_evidence:
            raise _DoDWorkspaceUnavailable(
                f"no sandbox evidence API on executor {type(self._loop.executor).__name__}"
            )
        from pathlib import Path

        file_checker = None
        if (
            sbx is not None
            and hasattr(sbx, "file_exists")
            and hasattr(sbx, "exec_shell")
            and hasattr(sbx, "resolve_relpath")
        ):
            import shlex

            from ...dod_evaluator import DoDPredicateResult

            async def _in_sandbox_file_checker(
                predicate: FileExistsPredicate,
            ) -> DoDPredicateResult:
                try:
                    exists = await sbx.file_exists(predicate.path)
                except Exception as exc:  # noqa: BLE001 — fail closed with evidence
                    return DoDPredicateResult(
                        predicate=predicate,
                        passed=False,
                        reason=f"sandbox file check failed: {type(exc).__name__}: {exc}",
                        unverifiable=True,
                        details={"kind": "file_exists", "path": predicate.path},
                    )
                if not exists:
                    return DoDPredicateResult(
                        predicate=predicate,
                        passed=False,
                        reason=f"file does not exist as a regular workspace file: {predicate.path}",
                        details={"kind": "file_exists", "path": predicate.path},
                    )
                try:
                    relpath = await sbx.resolve_relpath(predicate.path)
                    res = await sbx.exec_shell(
                        shlex.join(["sh", "-c", 'test -s "$1"', "disco", relpath]),
                        timeout_s=5,
                    )
                except Exception as exc:  # noqa: BLE001 — fail closed with evidence
                    return DoDPredicateResult(
                        predicate=predicate,
                        passed=False,
                        reason=f"sandbox non-empty check failed: {type(exc).__name__}: {exc}",
                        unverifiable=True,
                        details={"kind": "file_exists", "path": predicate.path},
                    )
                passed = int(res.exit_code) == 0
                return DoDPredicateResult(
                    predicate=predicate,
                    passed=passed,
                    reason=(
                        f"regular non-empty workspace file exists: {predicate.path}"
                        if passed
                        else f"workspace file is empty: {predicate.path}"
                    ),
                    details={
                        "kind": "file_exists",
                        "path": predicate.path,
                        "exit_code": int(res.exit_code),
                    },
                )

            file_checker = _in_sandbox_file_checker

        # An `http_ok` serve-check must probe from INSIDE the sandbox: a build's dev
        # server binds the SANDBOX's localhost, not the host's. The default host-side
        # urlopen would hit the agent-server (404 on `/`), false-failing every sandboxed
        # serve and spinning the model into re-serve/re-verify loops. Route the probe
        # through `exec_shell` + curl when the backend supports it; otherwise fall back
        # to the default host probe (e.g. the process backend shares the host network).
        http_probe = None
        if sbx is not None and hasattr(sbx, "exec_shell"):
            import shlex

            async def _in_sandbox_http_probe(url: str, expected_status: int) -> HttpProbeResult:
                cmd = "curl -s -o /dev/null -w '%{http_code}' --max-time 10 " + shlex.quote(url)
                try:
                    res = await sbx.exec_shell(cmd, timeout_s=15)
                except Exception as exc:  # noqa: BLE001 — a probe failure is "not probed", never a crash
                    return HttpProbeResult(
                        status_code=None,
                        error_message=f"sandbox http probe failed: {exc}",
                    )
                out = (res.stdout or "").strip()
                if not out.isdigit() or out == "000":
                    return HttpProbeResult(
                        status_code=None,
                        error_message=(
                            f"sandbox curl produced no HTTP status "
                            f"(got {out!r}, exit {res.exit_code})"
                        ),
                    )
                return HttpProbeResult(status_code=int(out))

            http_probe = _in_sandbox_http_probe

        # W3 C-2/C-4: a DoD `command` predicate is model-authored (it is copied
        # from the plan's `done_condition`). It must NEVER run as a host-side
        # `subprocess(shell=True)` — the DoDEvaluator's default runner does exactly
        # that. Mirror the http probe: route the command through the box via
        # `exec_shell`, so on a container backend it executes in the sandbox and
        # on the process backend it at least stays behind the hard-deny floor
        # (the deeper guarantee is OS isolation on the container backends). Without
        # this, `command: "curl … | sh"` in a plan step ran on the host at `finish`.
        command_runner = None
        if sbx is not None and hasattr(sbx, "exec_shell"):
            from ...dod_evaluator import CommandResult
            from ...dod_util import _hard_deny_reason

            async def _in_sandbox_command_runner(command: str) -> CommandResult:
                deny = _hard_deny_reason(command)
                if deny is not None:
                    return CommandResult(
                        exit_code=None,
                        error_message=f"hard-denied: {deny}",
                        denied=True,
                        deny_reason=deny,
                    )
                try:
                    res = await sbx.exec_shell(command, timeout_s=30)
                except Exception as exc:  # noqa: BLE001 — a check failure is "not run", never a crash
                    return CommandResult(
                        exit_code=None,
                        error_message=f"sandbox exec error: {type(exc).__name__}: {exc}",
                    )
                if getattr(res, "timed_out", False):
                    return CommandResult(
                        exit_code=None,
                        stdout=res.stdout or "",
                        stderr=res.stderr or "",
                        error_message="timeout after 30s in sandbox",
                    )
                return CommandResult(
                    exit_code=int(res.exit_code),
                    stdout=res.stdout or "",
                    stderr=res.stderr or "",
                )

            command_runner = _in_sandbox_command_runner

        return DoDEvaluator(
            Path(workspace) if workspace else Path("."),
            command_runner=command_runner,
            http_probe=http_probe,
            file_checker=file_checker,
        )

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
                await self._loop._land_blocked(
                    reason="approve_plan_no_execution",
                    guidance=(
                        "Plan approved but no execution action was taken after "
                        f"{self._loop._execution_nudges} execution reminders. "
                        "The plan was not executed."
                    ),
                    legacy_status=ConversationStatus.STUCK,
                    legacy_detail="approve_plan_no_execution",
                )
                return Disp.HALT

            self._loop._execution_nudges += 1  # telemetry + cap counter
            # Surface the model's reasoning before nudging (don't
            # discard it) — mirror the plan-nudge sibling.
            if step.thought.strip():
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(role="assistant", content=step.thought),
                    )
                )
            else:
                # An empty finish-step persists nothing, so it's
                # invisible to every event-derived detector; the
                # instance counter has to carry it (the (g) no-op
                # path does the same).
                self._loop._invisible_steps += 1
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=_EXECUTION_NUDGE),
                )
            )
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
