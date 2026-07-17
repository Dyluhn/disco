"""Browser, host, export, and render-verification gates for FinishGate."""

# ruff: noqa: F403,F405 -- mixin split intentionally shares the common import surface
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from ...verify_medium import (
    VerifierMediumHint,
    detect_html_medium,
    html_manifest_hrefs,
    html_script_srcs,
)
from .common import *
from .common import (
    _FINISH_VERIFY_CAP,
    _LOG,
    _PREVIEW_PORTS,
    _VERIFY_MARKER_PREFIX,
    _appkit_scope_active,
    _artifact_record_kind,
    _bounded_verifier_check_results,
    _browser_content_meaningful,
    _browser_unavailable_observed,
    _browser_verified,
    _deliverable_event_paths,
    _FinishGateProto,
    _is_web_deliverable,
    _last_productive_seq,
    _latest_app_deliverable_event,
    _latest_browser_error,
    _latest_browser_screenshot,
    _latest_browser_structured,
    _latest_deliverable_event,
    _latest_host_verifier_verdict,
    _latest_verify_verdict,
    _missing_steps_all_verify,
    _nonbrowser_static_validation_passed,
    _plan_file_exists_paths,
    _preview_key,
    _prior_verify_marker_fp,
    _real_web_failure_evidence,
    _safe_deliverable_file_path,
    _screenshot_from_verdict,
    _tc_components_installed,
    _vision_mode,
)

_STARTUP_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|token|secret)\b"
    r"(?:\s*[:=]\s*|\s+)(?:bearer\s+)?[^\s;]+"
)
_MODEL_VERIFIER_META_KEYS = (
    "model_verifier_status",
    "model_verifier_applied",
    "model_verifier_cause",
)
_HOST_VERDICT_SCREENSHOT_PATH_MAX_CHARS = 512
_SAFE_MODEL_VERIFIER_CAUSE_RE = re.compile(
    r"(?:"
    r"model verifier unavailable \("
    r"(?:JSONDecodeError: [A-Za-z0-9 _.-]{1,100} at line \d+ column \d+|"
    r"ValidationError: \d+ field error\(s\) \[[A-Za-z0-9_,.-]{1,160}\]|"
    r"LLM[A-Za-z]+Error(?:: provider [A-Za-z0-9_.-]{1,80} returned HTTP \d{3}"
    r"(?: type=[A-Za-z0-9_.-]{1,80})?|: (?:request timed out|connection error))?)"
    r"\)|"
    r"model verifier deadline exceeded|"
    r"judge exception: [A-Za-z_][A-Za-z0-9_]{0,100}|"
    r"deterministic host failure floor retained"
    r")"
)


def _bounded_model_verifier_cause(value: object) -> str:
    clean = "".join(char for char in str(value or "") if char in "\n\t" or ord(char) >= 32)
    clean = _STARTUP_DIAGNOSTIC_SECRET_RE.sub("<redacted>", clean)
    clean = clean[:256]
    if _SAFE_MODEL_VERIFIER_CAUSE_RE.fullmatch(clean):
        return clean
    return "model verifier unavailable (unclassified structural failure)"


def _bounded_host_verdict_screenshot_path(value: object) -> str | None:
    """Retain a usable, bounded path from the host verifier's raw verdict.

    The value is audit provenance, not a verification signal.  Do not truncate
    an overlong path into a different path, and do not persist control-bearing
    values.  Downstream evidence capture owns workspace jailing and namespace
    validation.
    """

    if not isinstance(value, str):
        return None
    path = value.strip()
    if (
        not path
        or len(path) > _HOST_VERDICT_SCREENSHOT_PATH_MAX_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        return None
    return path


def _model_verifier_fallback(base: dict[str, Any], *, status: str, cause: object) -> dict[str, Any]:
    out = dict(base)
    out.update(
        {
            "model_verifier": True,
            "model_verifier_status": status,
            "model_verifier_applied": False,
            "model_verifier_cause": _bounded_model_verifier_cause(cause),
        }
    )
    return out


class _WorkflowPathParams(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_workflow_output_path(template: str, params: dict[str, object]) -> str:
    values = _WorkflowPathParams(
        {key: "" if value is None else value for key, value in params.items()}
    )
    try:
        return template.format_map(values)
    except (IndexError, KeyError, ValueError):
        return template


class _FinishVerifyMixin(_FinishGateProto):
    async def finish_verify_passed(self, command: str) -> tuple[bool, bool]:
        """Run the agent's stated acceptance check before allowing `finish`
        (verify-on-finish post-condition gate). The agent attaches a shell
        command to finish whose exit 0 means the deliverable is good; we run it,
        VISIBLE in the trace, and on failure REFUSE the finish so the agent fixes
        the real problem instead of declaring a broken build complete.

        The verify command is NOT privileged: it passes the same hard-deny gate
        AND the same confirmation policy as any action. A command that would
        normally require confirmation is refused here (we don't silently run a
        gated command as a 'verification') — the agent is told to run it as an
        ordinary, gated action first. Ordinary test/build/lint checks assess as
        MEDIUM and run unimpeded. Returns (passed, malformed): `passed` is True
        iff the check ran and passed; `malformed` is True iff the verify command
        itself is broken (command-not-found / SyntaxError) rather than the task."""
        if _appkit_scope_active(self._loop):
            # Strict AppKit has no raw shell surface. Its authoritative finish
            # verification is the structured `verify_appkit_app` gate that runs
            # later in the shared finish path, so the model-authored shell probe is
            # a redundant impossible check here.
            return True, False

        workflow_run = getattr(self._loop, "_workflow_run", None)
        workflow_tools = tuple(getattr(getattr(workflow_run, "definition", None), "tools", ()))
        if workflow_run is not None and "shell" not in workflow_tools:
            # Defense in depth for stale clients and model hallucinations. The
            # workflow finish schema omits `verify` in this shape, but arguments
            # are still untrusted: never let the virtual finish tool smuggle a
            # raw shell action outside the approved workflow scope.
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            "The supplied finish verification was ignored because this "
                            "sealed workflow does not grant the shell tool. Host-owned "
                            "workflow output checks remain authoritative.\n"
                            "</system-reminder>"
                        ),
                    ),
                    meta={"workflow_finish_verify_ignored": True},
                )
            )
            return True, False

        call = ToolCall(tool_name="shell", arguments={"command": command})
        # meta marker: this shell action is the GATE'S probe, not the agent's
        # work. Phase-B re-run #6 (2026-06-10): an unmarked probe counted as a
        # real action in _actions_since_last_resume, so a refused first-move
        # finish UNLOCKED the withheld meta tools and the model remember-spammed
        # straight into the valve. The probe must never flip fresh-session.
        action = ActionEvent(
            thought=f"Verifying completion: {command}",
            tool_call=call,
            meta={"verify_probe": True},
        )

        deny = signals.hard_deny_reason(action)
        if deny is not None:
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        f"The verify command attached to finish is hard-denied ({deny}); it "
                        "will not run. Provide a safe verify command, or finish without one.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        risk = self._loop.analyzer.assess(action)
        if self._loop.policy.should_confirm(risk):
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        "The verify command attached to finish needs confirmation to run "
                        "and won't be executed silently as a verification. Run that check "
                        "as a normal action first (it will go through the confirm gate), "
                        "then finish.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        await self._loop._emit(action)
        await self._loop._execute_and_observe(action)
        # Find the observation correlated to THIS verify action (robust against a
        # trailing sandbox-restart notice that _execute_and_observe may append).
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        passed = isinstance(obs, ObservationEvent) and obs.tool_result.success
        # malformed = the verify COMMAND ITSELF is broken (not the deliverable):
        # command-not-found (127) or an interpreter SyntaxError. A non-zero exit from
        # an unrunnable check is NOT evidence the task failed — the carrier was bad.
        # The caller auto-strips a malformed verify rather than counting it as a
        # failed acceptance. Detected from the result text.
        malformed = False
        # Malformed = the verify CARRIER is broken, which only makes sense if the
        # shell actually RAN the command and reported it (an ObservationEvent). An
        # AgentErrorEvent means the executor raised BEFORE any observation (sandbox
        # down, transport error) — that's an environmental failure, NOT a malformed
        # verify, and must stay a real failure so it isn't auto-stripped into a false
        # "done". (Earlier this read AgentErrorEvent.error text and a stray "command
        # not found" substring there would wrongly strip an environmental failure.)
        if not passed and isinstance(obs, ObservationEvent):
            st = obs.tool_result.structured or {}
            ec = st.get("exit_code")
            exit_code = ec if isinstance(ec, int) and not isinstance(ec, bool) else None
            low = f"{obs.tool_result.content or ''} {obs.tool_result.error or ''}".lower()
            # The shell couldn't find/parse the command: exit 127 (command-not-found,
            # authoritative from the structured result — locale-independent, can't be
            # faked by output text) or an interpreter SyntaxError. Use the real exit
            # code, NOT a regex over output: "exit 1, 127 tests failed" is a REAL
            # failure, not a malformed carrier, and must NOT become a false success.
            malformed = (
                exit_code == 127
                or "syntaxerror" in low
                # content fallbacks only when the structured exit code is unavailable
                or (exit_code is None and "command not found" in low)
                or (exit_code is None and ": not found" in low)
            )
        return passed, malformed

    async def _drive_finish_browser_probe(self, target_url: str | None = None) -> bool:
        """ACTIVE finish-verify: instead of TRUSTING the agent to have browsed the
        deliverable, the gate DRIVES a `browser navigate <preview>` itself and
        judges the result on ground truth. The preview platform assigns a RANDOM
        port, so the gate navigates to the RESOLVED preview (`target_url`, from
        `_detect_preview_url` — backend-aware, never the agent-server's :8000 on the
        shared-host backend); only when no preview is detectable does it fall back
        to the legacy `http://127.0.0.1:8000/` (the isolated-backend app port). This
        closes the
        verification-overclaim hole: an agent that declares done without ever
        looking can no longer land a JS-broken/blank page as FINISHED — the gate
        looks for it.

        Returns True iff the probe RAN and produced a usable browser observation
        (ObservationEvent.tool_result.success). The caller then re-judges via
        `_browser_verified` (console errors) + `_browser_content_meaningful`
        (blank render). Returns False (no-op) when the browser backend is
        unavailable (the process backend ships no `browser` tool) or the probe is
        hard-denied — so the gate degrades to its prior passive nudge/release
        behavior on browserless backends rather than hanging or false-refusing.

        The probe ActionEvent is tagged `meta={"verify_probe": True}` so signals.py
        keeps it OUT of agent-work accounting (actions_since_last_resume /
        productive_actions_since_approval) — the same marker finish_verify_passed
        uses. Running the gate's own probe is never evidence the AGENT did work."""
        # Browserless backend? (process backend exposes no `browser` tool) → no-op,
        # let the gate fall back to today's passive behavior.
        try:
            tool_names = {getattr(t, "name", None) for t in self._loop.executor.available_tools()}
        except Exception:  # noqa: BLE001 — any introspection failure → degrade safely
            tool_names = set()
        if "browser" not in tool_names:
            return False

        nav_url = target_url or "http://127.0.0.1:8000/"
        call = ToolCall(
            tool_name="browser",
            arguments={"action": "navigate", "url": nav_url},
        )
        action = ActionEvent(
            thought=f"Verifying the app renders: navigating to {nav_url}",
            tool_call=call,
            meta={"verify_probe": True},
        )
        # A hard-denied probe (should never happen for a browser navigate, but the
        # contract surface is shared) is a no-op → degrade to passive.
        if signals.hard_deny_reason(action) is not None:
            return False

        await self._loop._emit(action)
        await self._loop._execute_and_observe(action)
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        return isinstance(obs, ObservationEvent) and obs.tool_result.success

    def _available_tool_names(self) -> set[str | None]:
        try:
            return {getattr(t, "name", None) for t in self._loop.executor.available_tools()}
        except Exception:  # noqa: BLE001 — introspection failure → degrade safely
            return set()

    def _verify_tool_available(self) -> bool:
        """W-45 — is a structured app verifier in the execution set?"""
        return self._active_verify_tool() is not None

    def _active_verify_tool(self) -> str | None:
        """Return the structured verifier active for this executor.

        Strict AppKit mode advertises `verify_appkit_app` from the AppKit-only
        allowlist. Prefer it; otherwise fall back to normal Build's `verify_web_app`.
        """
        tool_names = self._available_tool_names()
        if "verify_appkit_app" in tool_names:
            return "verify_appkit_app"
        if "verify_web_app" in tool_names:
            return "verify_web_app"
        return None


class _HostVerifyGateMixin(_FinishGateProto):
    async def _host_artifact_file_exists(self, path: str) -> bool | None:
        sbx = getattr(self._loop.executor, "sandbox", None)
        file_exists: Any = getattr(sbx, "file_exists", None)
        if file_exists is None:
            return None
        try:
            return bool(await file_exists(path))
        except Exception:  # noqa: BLE001 — path resolution is advisory; skip only on known absence
            return None

    async def _artifact_manifest_records(
        self, events: list[Event], *, consumer: str
    ) -> tuple[Any, ...] | None:
        """Read REL-2a artifact manifest records for promoted readers.

        Empty manifests are NOT agreement: the REL-1e flip exposed how easy it is
        to bank a shadow claim when no shadow data was actually recorded. This
        helper only returns records when the manifest has at least one path, and
        always logs the event-projection comparison while the reader soaks.
        """

        if not artifact_manifest_reader_enabled():
            return None
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            _LOG.info(
                "artifact-manifest reader skipped for %s:%s: sandbox unavailable",
                self._loop.conversation_id,
                consumer,
            )
            return None
        try:
            from ...context import ArtifactMemoryStore

            records = tuple(await ArtifactMemoryStore(sbx).read_artifacts())
        except Exception:  # noqa: BLE001 — manifest reader must fail open to legacy readers
            _LOG.warning(
                "artifact-manifest reader failed for %s:%s",
                self._loop.conversation_id,
                consumer,
                exc_info=True,
            )
            return None

        manifest_paths = artifact_paths_from_manifest_records(records)
        projected = artifact_paths_from_events(events)
        if not manifest_paths:
            _LOG.info(
                "artifact-manifest reader skipped for %s:%s: no manifest data (projected=%d)",
                self._loop.conversation_id,
                consumer,
                len(projected),
            )
            return None

        missing, extra = manifest_path_divergence(projected, manifest_paths)
        _LOG.info(
            "artifact-manifest reader compare for %s:%s: projected=%d manifest=%d "
            "missing=%s extra=%s",
            self._loop.conversation_id,
            consumer,
            len(projected),
            len(manifest_paths),
            sorted(missing),
            sorted(extra),
        )
        return records

    async def _host_verify_artifact_path(self, raw_path: str) -> str | None:
        path = _safe_deliverable_file_path(raw_path)
        legacy_index = _safe_deliverable_file_path(raw_path, app_root=True)
        if path is None:
            path = legacy_index
        if path is None:
            return None
        exists = await self._host_artifact_file_exists(path)
        if exists is not False:
            return path
        if legacy_index is not None and legacy_index != path:
            if await self._host_artifact_file_exists(legacy_index) is not False:
                return legacy_index
        return None

    async def _host_verify_manifest_deliverable(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        include_unverifiable: bool,
    ) -> HostVerificationDeliverable | None:
        records = await self._artifact_manifest_records(events, consumer="host_verify")
        if records is None:
            return None

        app_records = [r for r in records if _artifact_record_kind(r) == "app"]
        for record in reversed(app_records):
            raw_path = getattr(record, "path", None)
            if not isinstance(raw_path, str):
                continue
            path = await self._host_verify_artifact_path(raw_path)
            if path is None:
                continue
            return HostVerificationDeliverable(
                conversation_id=self._loop.conversation_id,
                artifact_path=path,
                artifact_kind="app",
                deployment_url="",
                requested_verification=step.requested_verification,
            )

        if not include_unverifiable:
            return None

        file_records = [r for r in records if _artifact_record_kind(r) != "app"]
        shown_records = [r for r in file_records if bool(getattr(r, "shown", False))]
        for record in reversed(shown_records or file_records):
            raw_path = getattr(record, "path", None)
            if not isinstance(raw_path, str):
                continue
            path = _safe_deliverable_file_path(raw_path)
            if path is None:
                continue
            return HostVerificationDeliverable(
                conversation_id=self._loop.conversation_id,
                artifact_path=path,
                artifact_kind=_artifact_record_kind(record),
                deployment_url="",
                requested_verification=step.requested_verification,
            )
        return None

    def _host_verify_authoritative(self) -> bool:
        return bool(getattr(self._loop, "_host_verify_authoritative", False))

    async def _host_verify_deliverable(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        include_unverifiable: bool = False,
    ) -> HostVerificationDeliverable | None:
        """REL-1c — reconstruct the web-like deliverable for host shadow verify.

        The host verifier is advisory in this PR, so missing/ambiguous delivery
        evidence simply skips the shadow path. A first-class app handoff wins;
        otherwise the existing web-finish convention (index.html/server_status) is
        treated as index.html. App-root handoffs such as "." or "dist" are
        resolved to their primary index.html-style file, never verified as the
        directory path itself.
        """

        manifest_deliverable = await self._host_verify_manifest_deliverable(
            step, events, include_unverifiable=include_unverifiable
        )
        if manifest_deliverable is not None:
            return manifest_deliverable

        app_event = _latest_app_deliverable_event(events)
        any_event = _latest_deliverable_event(events) if include_unverifiable else app_event
        if any_event is None and not _is_web_deliverable(events):
            return None
        if any_event is not None and any_event.artifact_kind != "app":
            path = _safe_deliverable_file_path(any_event.path)
        else:
            path = await self._host_verify_artifact_path(
                any_event.path if any_event is not None else "index.html"
            )
        if path is None:
            return None
        deployment_url = any_event.deployment_url if any_event is not None else ""
        artifact_kind = any_event.artifact_kind if any_event is not None else "app"
        return HostVerificationDeliverable(
            conversation_id=self._loop.conversation_id,
            artifact_path=path,
            artifact_kind=artifact_kind,
            deployment_url=deployment_url or "",
            requested_verification=step.requested_verification,
        )

    @staticmethod
    def _verdict_label(verdict: dict | None) -> str | None:
        if not verdict:
            return None
        label = verdict.get("verdict")
        if label is not None:
            return str(label)
        if "passed" in verdict:
            return "pass" if verdict.get("passed") is True else "fail"
        return None

    @staticmethod
    def _verdict_failures(verdict: dict) -> list[dict[str, object]]:
        failures: list[dict[str, object]] = []
        for item in verdict.get("failures") or []:
            if isinstance(item, dict):
                failures.append(dict(item))
        for err in verdict.get("console_errors") or []:
            if isinstance(err, dict):
                failures.append({"kind": "console_error", **err})
        for err in verdict.get("network_failures") or []:
            if isinstance(err, dict):
                failures.append({"kind": "network_failure", **err})
        if verdict.get("passed") is not True and not failures:
            summary = str(verdict.get("summary") or verdict.get("detail") or "").strip()
            if summary:
                failures.append({"kind": "summary", "message": summary})
        return failures

    @staticmethod
    def _host_unavailable_verdict(
        deliverable: HostVerificationDeliverable, detail: str
    ) -> dict[str, object]:
        return {
            "passed": False,
            "verdict": "unavailable",
            "url": deliverable.deployment_url,
            "http_status": 0,
            "summary": detail,
            "next_action": "",
            "console_errors": [],
            "network_failures": [],
            "failure_fingerprint": "host_verifier_unavailable",
        }

    @staticmethod
    def _host_unverifiable_verdict(
        deliverable: HostVerificationDeliverable,
    ) -> dict[str, object]:
        return {
            "passed": False,
            "verdict": "unverifiable",
            "url": deliverable.deployment_url,
            "http_status": 0,
            "summary": (
                f"No host validator is available for artifact kind "
                f"{deliverable.artifact_kind!r}; verification was not claimed."
            ),
            "next_action": "",
            "console_errors": [],
            "network_failures": [],
            "failure_fingerprint": f"host_verifier_unverifiable:{deliverable.artifact_kind}",
        }

    @staticmethod
    def _verdict_first_failure(verdict: dict) -> str:
        errs = verdict.get("console_errors") or []
        nets = verdict.get("network_failures") or []
        if errs:
            e0 = errs[0]
            if isinstance(e0, dict):
                where = f" @ {e0.get('source')}" if e0.get("source") else ""
                return f"{e0.get('text', '')}{where}".strip()
        if nets:
            n0 = nets[0]
            if isinstance(n0, dict):
                marker = n0.get("status") or n0.get("failure") or "failed"
                return f"{n0.get('method', 'GET')} {n0.get('url', '')} -> {marker}".strip()
        failures = _HostVerifyGateMixin._verdict_failures(verdict)
        if failures:
            message = failures[0].get("message")
            if message:
                return str(message)
        return ""

    def _verifier_contract_payload(self) -> dict[str, Any]:
        alias = getattr(self._loop, "_finish_alias", None)
        if not alias:
            return {}
        try:
            from ...contract.registry import BuildContractRegistry

            reg = BuildContractRegistry.default()
            for kind in reg.kinds():
                c = reg.get(kind)
                if c is not None and c.verify.finalizer == alias:
                    return c.model_dump(mode="json")
        except Exception:  # noqa: BLE001 — verifier seed degrades to finalizer-only
            pass
        return {"verify": {"finalizer": alias}}

    async def _verifier_deliverable_paths(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
    ) -> list[str]:
        paths: list[str] = [deliverable.artifact_path]
        paths.extend(_deliverable_event_paths(events))
        paths.extend(_plan_file_exists_paths(events))
        paths.extend(self._contract_required_deliverable_paths())
        spec = await self._loop.store.get_external_dod_spec(self._loop.conversation_id)
        if spec is not None:
            for pred in spec.predicates:
                if isinstance(pred, FileExistsPredicate):
                    p = _safe_deliverable_file_path(pred.path)
                    if p is not None:
                        paths.append(p)

        out: list[str] = []
        seen: set[str] = set()
        for p in paths:
            safe = _safe_deliverable_file_path(str(p))
            if safe is None or safe in seen:
                continue
            seen.add(safe)
            out.append(safe)
        return out

    async def _read_workspace_bytes(self, path: str) -> bytes | None:
        sbx = getattr(getattr(self._loop, "executor", None), "sandbox", None)
        if sbx is None:
            return None
        try:
            data = await sbx.read_file(path)
        except Exception:
            return None
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("utf-8")
        return None

    async def _manifest_present_for_html(
        self, html_path: str, html_text: str, deliverable_paths: list[str]
    ) -> bool:
        base_dir = posixpath.dirname(html_path)
        candidates: list[str] = []
        for href in html_manifest_hrefs(html_text):
            clean = href.split("#", 1)[0].split("?", 1)[0].strip()
            if not clean or "://" in clean or clean.startswith("//"):
                continue
            safe = _safe_deliverable_file_path(posixpath.normpath(posixpath.join(base_dir, clean)))
            if safe is not None:
                candidates.append(safe)
        if not candidates:
            safe_default = _safe_deliverable_file_path(
                posixpath.normpath(posixpath.join(base_dir, "manifest.json"))
            )
            if safe_default is not None:
                candidates.append(safe_default)
        candidates.extend(
            p for p in deliverable_paths if posixpath.basename(p).lower() == "manifest.json"
        )
        seen: set[str] = set()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            if await self._read_workspace_bytes(path) is not None:
                return True
        return False

    async def _related_game_texts_for_html(self, html_path: str, html_text: str) -> list[str]:
        base_dir = posixpath.dirname(html_path)
        candidates: list[str] = []
        for src in html_script_srcs(html_text):
            clean = src.split("#", 1)[0].split("?", 1)[0].strip()
            if not clean or "://" in clean or clean.startswith("//"):
                continue
            safe = _safe_deliverable_file_path(posixpath.normpath(posixpath.join(base_dir, clean)))
            if safe is not None:
                candidates.append(safe)
        for name in ("README.md", "NOTES.md"):
            safe = _safe_deliverable_file_path(posixpath.normpath(posixpath.join(base_dir, name)))
            if safe is not None:
                candidates.append(safe)

        texts: list[str] = []
        seen: set[str] = set()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            raw = await self._read_workspace_bytes(path)
            if raw is not None:
                texts.append(raw.decode("utf-8", errors="replace"))
        return texts

    async def _verifier_medium_hint(
        self,
        deliverable_paths: list[str],
    ) -> VerifierMediumHint | None:
        for path in deliverable_paths:
            if not path.lower().endswith((".html", ".htm")):
                continue
            raw = await self._read_workspace_bytes(path)
            if raw is None:
                continue
            text = raw.decode("utf-8", errors="replace")
            manifest_present = await self._manifest_present_for_html(path, text, deliverable_paths)
            hint = detect_html_medium(
                text,
                manifest_present=manifest_present,
                related_texts=await self._related_game_texts_for_html(path, text),
            )
            if hint is not None:
                return hint
        return None

    async def _verifier_context_seed(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        check_verdict: dict[str, Any],
    ) -> VerifierContextSeed:
        deliverable_paths = await self._verifier_deliverable_paths(deliverable, events)
        return VerifierContextSeed(
            contract=self._verifier_contract_payload(),
            deliverable_paths=deliverable_paths,
            check_results=_bounded_verifier_check_results(check_verdict),
            screenshot=_screenshot_from_verdict(check_verdict),
            medium=await self._verifier_medium_hint(deliverable_paths),
        )

    @staticmethod
    def _typed_verifier_verdict_to_host_verdict(
        base: dict[str, Any], typed: TypedVerifierVerdict
    ) -> dict[str, Any]:
        out = dict(base)
        out.update(
            {
                "passed": bool(typed.verified),
                "verdict": typed.verdict,
                "summary": typed.detail or str(base.get("summary") or ""),
                "detail": typed.detail or None,
                "next_action": typed.next_action or str(base.get("next_action") or ""),
                "failure_fingerprint": (
                    typed.failure_fingerprint
                    or str(base.get("failure_fingerprint") or "model_verifier")
                ),
                "failures": list(typed.failures),
                "model_verifier": True,
                "model_verifier_status": typed.verdict,
                "model_verifier_applied": True,
            }
        )
        return out

    async def _model_judged_verdict(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        host_verdict: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the optional model verifier behind a bounded context boundary.

        The seed is built from contract + deliverable paths + deterministic check
        results + screenshot only. The model transcript is never appended to the
        builder event log; only the typed verdict summary returned here can flow
        into VerifierVerdictEvent and wake-on-fail.
        """

        judge = getattr(self._loop, "_verifier_judge", None)
        if judge is None:
            return host_verdict
        host_label = self._verdict_label(host_verdict)
        if host_label in {"unavailable", "unverifiable"}:
            return host_verdict

        seed = await self._verifier_context_seed(deliverable, events, host_verdict)
        try:
            raw_typed = await asyncio.wait_for(
                judge.judge(seed),
                timeout=max(
                    0.001,
                    float(getattr(self._loop, "_verifier_judge_timeout_s", 30.0)),
                ),
            )
            typed = (
                raw_typed
                if isinstance(raw_typed, TypedVerifierVerdict)
                else TypedVerifierVerdict.model_validate(raw_typed)
            )
        except TimeoutError:
            _LOG.warning(
                "model verifier timed out for %s:%s",
                self._loop.conversation_id,
                deliverable.artifact_path,
            )
            return _model_verifier_fallback(
                host_verdict,
                status="timeout",
                cause="model verifier deadline exceeded",
            )
        except Exception as exc:  # noqa: BLE001 — verifier judge must not crash finish flow
            _LOG.warning(
                "model verifier failed for %s:%s",
                self._loop.conversation_id,
                deliverable.artifact_path,
                exc_info=True,
            )
            return _model_verifier_fallback(
                host_verdict,
                status="error",
                cause=f"judge exception: {type(exc).__name__}",
            )

        judged = self._typed_verifier_verdict_to_host_verdict(host_verdict, typed)
        if (
            typed.verdict == "unavailable"
            and typed.failure_fingerprint == "model_verifier_unavailable"
        ):
            return _model_verifier_fallback(
                host_verdict,
                status="unavailable",
                cause=typed.detail or typed.failure_fingerprint,
            )
        # Deterministic host failures are a hard floor. A reading/vision judge can
        # add detail to a failure, but it cannot green-light a broken runtime.
        if host_verdict.get("passed") is not True and (
            typed.verified or typed.verdict in {"unavailable", "unverifiable"}
        ):
            return _model_verifier_fallback(
                host_verdict,
                status=typed.verdict,
                cause="deterministic host failure floor retained",
            )
        return judged

    async def _host_verify_failure_disposition(
        self, deliverable: HostVerificationDeliverable, verdict: dict
    ) -> Disp:
        label = self._verdict_label(verdict) or "fail"
        summary = str(
            verdict.get("summary") or verdict.get("detail") or "host verifier did not pass"
        )
        next_action = str(verdict.get("next_action") or "")
        first_failure = self._verdict_first_failure(verdict)
        await self._record_verifier_failure_to_context(
            message=(first_failure or summary), rel_path=None
        )
        if self._loop._browser_verify_refusals < 3:
            self._loop._browser_verify_refusals += 1
            payload = (
                "<system-reminder>\n"
                f"Host verification did not pass for {deliverable.artifact_kind} "
                f"artifact {deliverable.artifact_path!r} ({label}). {summary}\n"
                + (f"first failure: {first_failure}\n" if first_failure else "")
                + (
                    f"next step: {next_action}\n"
                    if next_action
                    else "Fix the issue surfaced by the host verifier, then finish again.\n"
                )
                + "The task is NOT complete until the host verifier passes.\n"
                "</system-reminder>"
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=payload),
                )
            )
            return Disp.CONTINUE

        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            )
        )
        warn = (
            "⚠ Finished WITHOUT a passing host verifier verdict (3 attempts) — "
            f"the deliverable is UNVERIFIED and may be INCOMPLETE. {summary}"
            + (f" Outstanding: {next_action}" if next_action else "")
            + " Note this clearly in your summary."
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=warn),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    async def gate_host_verify(self, step: AgentStep, events: list[Event]) -> Disp:
        """REL-1c/1e — run the host verifier and emit audit telemetry.

        Authoritative (the default since 2026-07-03): a real host ``fail`` on an
        app deliverable refuses the finish (bounded by the 3-refusal release
        valve); ``unavailable`` (verifier infrastructure could not run) degrades
        to FALLTHROUGH so the inline browser gate stays the enforcement path on
        browserless installs; non-app/no-validator handoffs record an honest
        ``unverifiable`` verdict without claiming verification. With
        DISCO_HOST_VERIFY_AUTHORITATIVE explicitly off (REL-1c shadow posture)
        this gate is FALLTHROUGH-only telemetry, preserving the inline
        self-verify/browser gate as sole enforcement.
        """

        authoritative = self._host_verify_authoritative()
        host_verifier = getattr(self._loop, "_host_verifier", None)
        if host_verifier is None and not authoritative:
            return Disp.FALLTHROUGH

        deliverable = await self._host_verify_deliverable(
            step, events, include_unverifiable=authoritative
        )
        if deliverable is None:
            return Disp.FALLTHROUGH

        await self._loop._emit(
            VerifierStartedEvent(
                artifact_path=deliverable.artifact_path,
                artifact_kind=deliverable.artifact_kind,
                requested_by_event_id=None,
                meta={"requested_verification": deliverable.requested_verification},
            )
        )

        if deliverable.artifact_kind != "app":
            host_screenshot_path = None
            if not authoritative:
                return Disp.FALLTHROUGH
            host_verdict = self._host_unverifiable_verdict(deliverable)
        elif host_verifier is None:
            return Disp.FALLTHROUGH
        else:
            host_screenshot_path = None
            try:
                raw_host_verdict = await asyncio.wait_for(
                    host_verifier.verify(deliverable),
                    timeout=max(
                        0.001,
                        float(getattr(self._loop, "_host_verify_timeout_s", 30.0)),
                    ),
                )
                if not isinstance(raw_host_verdict, dict):
                    host_verdict = self._host_unavailable_verdict(
                        deliverable,
                        "verification could not run: host verifier did not return "
                        "a usable verdict.",
                    )
                else:
                    host_verdict = raw_host_verdict
                    host_screenshot_path = _bounded_host_verdict_screenshot_path(
                        raw_host_verdict.get("screenshot_path")
                    )
            except TimeoutError:
                host_verdict = self._host_unavailable_verdict(
                    deliverable,
                    "verification could not run: host verifier timed out.",
                )
            except Exception as exc:  # noqa: BLE001 — shadow verifier never crashes finish flow
                _LOG.warning(
                    "REL-1c host verifier failed for %s:%s",
                    self._loop.conversation_id,
                    deliverable.artifact_path,
                    exc_info=True,
                )
                host_verdict = self._host_unavailable_verdict(
                    deliverable,
                    f"verification could not run: host verifier failed ({exc}).",
                )

        host_verdict = await self._model_judged_verdict(
            deliverable, events, cast(dict[str, Any], host_verdict)
        )
        inline_verdict = _latest_verify_verdict(
            events,
            _last_productive_seq(events),
            target_url=None,
            tool_name=self._active_verify_tool() or "verify_web_app",
        )
        host_label = self._verdict_label(host_verdict)
        inline_label = self._verdict_label(inline_verdict)
        agreement = (
            host_label == inline_label
            if host_label is not None and inline_label is not None
            else None
        )
        detail = str(host_verdict.get("summary") or host_verdict.get("detail") or "")
        startup_diagnostic = str(host_verdict.get("startup_diagnostic") or "").strip()
        if startup_diagnostic:
            startup_diagnostic = "".join(
                char for char in startup_diagnostic if char in "\n\t" or ord(char) >= 32
            )
            startup_diagnostic = _STARTUP_DIAGNOSTIC_SECRET_RE.sub(
                "<redacted>", startup_diagnostic
            )[:1280]
            detail = (
                f"{detail}\nBrowser startup diagnostic: {startup_diagnostic}"
                if detail
                else f"Browser startup diagnostic: {startup_diagnostic}"
            )

        verifier_meta: dict[str, Any] = {
            "requested_verification": deliverable.requested_verification
        }
        for key in _MODEL_VERIFIER_META_KEYS:
            if key in host_verdict:
                verifier_meta[key] = host_verdict[key]

        await self._loop._emit(
            VerifierShadowEvent(
                artifact_path=deliverable.artifact_path,
                artifact_kind=deliverable.artifact_kind,
                inline_verdict=inline_label,
                host_verdict=host_label,
                agreement=agreement,
                detail=detail or None,
                meta=dict(verifier_meta),
            )
        )
        verdict_event = await self._loop._emit(
            VerifierVerdictEvent(
                artifact_path=deliverable.artifact_path,
                artifact_kind=deliverable.artifact_kind,
                verified=host_verdict.get("passed") is True and host_label == "pass",
                verdict=host_label,
                detail=detail or None,
                failures=self._verdict_failures(host_verdict),
                screenshot_path=host_screenshot_path,
                meta=dict(verifier_meta),
            )
        )
        hook = getattr(self._loop, "_host_verifier_verdict_hook", None)
        if hook is not None:
            try:
                await hook(cast(VerifierVerdictEvent, verdict_event))
            except Exception:  # noqa: BLE001 — REL-1d canary bookkeeping is non-authoritative
                _LOG.warning(
                    "REL-1d host verifier canary hook failed for %s:%s",
                    self._loop.conversation_id,
                    deliverable.artifact_path,
                    exc_info=True,
                )
        if authoritative and deliverable.artifact_kind == "app":
            if host_verdict.get("passed") is True and host_label == "pass":
                self._loop._browser_verify_refusals = 0
                return Disp.FALLTHROUGH
            if host_label == "unavailable":
                # Missing host-verifier infrastructure is not proof the app is broken;
                # fall through so the inline browser gate remains the enforcement path.
                return Disp.FALLTHROUGH
            if host_label == "unverifiable":
                # Browser infrastructure was explicitly unavailable. This is not a
                # verified pass, but retrying the identical daemon launch in the inline
                # gate contradicts the terminal browser_unavailable contract. Release
                # once with a durable incomplete marker that UIs/harnesses can classify.
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail="unverified_release",
                    )
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "⚠ Finished WITHOUT browser-render verification — the "
                                "host verifier reported the app UNVERIFIABLE because its "
                                "browser infrastructure could not run. The deliverable is "
                                "UNVERIFIED and may be INCOMPLETE; do not report it as a "
                                "verified pass."
                            ),
                        ),
                    )
                )
                self._loop._browser_verify_refusals = 0
                return Disp.FALLTHROUGH
            return await self._host_verify_failure_disposition(deliverable, host_verdict)
        return Disp.FALLTHROUGH


class _BrowserVerifyGateMixin(_FinishGateProto):
    async def _detect_preview_url(self) -> str | None:
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
        real preview. Never raises (a detection failure must not wedge the gate)."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None or not hasattr(sbx, "exec_shell"):
            return None
        host_shared = backend_shares_host_network(sbx)
        if host_shared:
            # Process backend (shares host net): ownership-aware — never bind the
            # agent-server's 8000. Isolated containers fall to the branch below where
            # 8000 IS the app (the old `workspace_path` heuristic wrongly sent them here).
            try:
                res = await sbx.exec_shell(
                    port_ownership_probe_command(_PREVIEW_PORTS), timeout_s=10
                )
            except Exception:  # noqa: BLE001 — detection failure → no binding (degrade safe)
                return None
            owned = parse_port_ownership(str(getattr(res, "stdout", "") or ""))
            port = resolve_preview_port(
                host_shared=True,
                owned=owned,
                conversation_id=str(getattr(sbx, "conversation_id", "") or ""),
            )
            return f"http://127.0.0.1:{port}/" if port is not None else None

        # Isolated backend: 8000 is the app inside the box — first reachable wins.
        import shlex

        ports = list(_PREVIEW_PORTS)
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

    async def _drive_verify_web_app(
        self,
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
        (which itself uses the same backend-aware resolver)."""
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
        await self._loop._emit(action)
        await self._loop._execute_and_observe(action)
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        return isinstance(obs, ObservationEvent) and obs.tool_result.success

    async def _gate_verify_web_app(
        self,
        events: list[Event],
        tool_name: str = "verify_web_app",
        *,
        step: AgentStep | None = None,
    ) -> Disp:
        """W-45 — the verdict-consuming finish gate (the loop-killer).

        Replaces `_browser_verified()` as the PRIMARY completion check for web
        builds with "the latest structured verifier verdict since the last productive
        edit". If none exists when the agent calls finish, the gate DRIVES
        the verifier itself (ONE call). pass → finish; fail → emit the verdict's
        summary + first concrete error + screenshot + next_action and CONTINUE.

        LOOP BREAKER: the verdict is cached by `_last_productive_seq` — a browser/
        navigate/verify probe is NOT a productive edit, so re-verifying without an
        edit reads the SAME cached verdict (no real re-run). When the SAME
        `failure_fingerprint` recurs without a productive edit that changes the
        served output, the gate marks the run STUCK/no_progress (W-31 detail
        naming) instead of reloading 25-40×. The 3-refusal release no longer
        silently converts repeated failed verification into 'done' — it finishes
        ONLY with an explicit blocked/incomplete summary."""
        since_seq = _last_productive_seq(events)
        # P1-1: bind the accepted verdict to the CURRENT preview target. Detect the
        # live preview (same _PREVIEW_PORTS detection the tool uses) and accept only
        # a verdict whose url matches it; a stale / foreign-port (or url-less) PASS
        # must NOT satisfy the gate — it drives a fresh verify against the real
        # preview instead. target_url=None (preview undetectable) disables binding.
        target_url = await self._detect_preview_url()
        medium = "web"
        if step is not None:
            try:
                deliverable = await self._host_verify_deliverable(step, events)
                if deliverable is not None:
                    host_verifier = cast(_HostVerifyGateMixin, self)
                    paths = await host_verifier._verifier_deliverable_paths(deliverable, events)
                    hint = await host_verifier._verifier_medium_hint(paths)
                    if hint is not None:
                        medium = hint.kind
            except Exception:
                medium = "web"
        verdict = _latest_verify_verdict(events, since_seq, target_url, tool_name)
        if verdict is None:
            # No fresh verdict bound to the current preview — the agent may have
            # overclaimed, or only a stale/foreign-url verdict exists. Drive ONE
            # against the resolved real preview (target_url is backend-aware — never
            # the agent-server's 8000 on the process backend, Bug 7).
            if await self._drive_verify_web_app(target_url, tool_name, medium=medium):
                events = await self._loop._events()
                # The freshly driven verify auto-detected + tested the CURRENT
                # preview, so its verdict IS bound by construction — read it
                # unconditionally (target_url=None) rather than re-binding against a
                # detection that could disagree with the tool's own auto-detect.
                verdict = _latest_verify_verdict(
                    events, since_seq, target_url=None, tool_name=tool_name
                )
        if verdict is None:
            # P1-2: the verifier could not produce a usable verdict (execution
            # error / empty / the driven verify failed). This must NOT fall through
            # to a clean finish — on the build/web surface a FINISH requires a real
            # PASS verdict (W-32: route completion THROUGH the gate). Refuse-and-
            # continue (bounded), then an EXPLICIT unverified release at the cap;
            # never a silent done, never a fall-back to the legacy browser gate.
            return await self._verifier_unavailable_disposition(tool_name)

        if verdict.get("passed") is True:
            self._loop._browser_verify_refusals = 0  # clean pass → reset the streak
            if _vision_mode():
                shot = str(verdict.get("screenshot_path") or "")
                if shot:
                    await self._loop._emit(
                        StatusEvent(
                            status=ConversationStatus.RUNNING,
                            detail=f"vision_artifact:{shot}",
                        )
                    )
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
        honest = await self._maybe_honest_unverifiable_static_finish(verdict)
        if honest is not None:
            return honest

        # FAIL / DEGRADED. Build the concrete next-step payload from the verdict.
        fp = str(verdict.get("failure_fingerprint") or "")
        summary = str(verdict.get("summary") or f"{tool_name} did not pass")
        next_action = str(verdict.get("next_action") or "")
        screenshot = str(verdict.get("screenshot_path") or "")
        errs = verdict.get("console_errors") or []
        nets = verdict.get("network_failures") or []
        first_error = ""
        if errs:
            e0 = errs[0]
            where = f" @ {e0.get('source')}" if e0.get("source") else ""
            first_error = f"{e0.get('text', '')}{where}"
        elif nets:
            n0 = nets[0]
            marker = n0.get("status") or n0.get("failure") or "failed"
            first_error = f"{n0.get('method', 'GET')} {n0.get('url', '')} -> {marker}"

        # CXT-7: record the failure to durable context (.disco/context/verifier_failures.json),
        # best-effort — NEVER alters the gate verdict/flow. CXT-4's assembler surfaces these
        # unresolved failures into the model's ContextPack on later turns/resume.
        await self._record_verifier_failure_to_context(
            message=(first_error or summary), rel_path=(screenshot or None)
        )

        prior_fp = _prior_verify_marker_fp(events, since_seq)
        if prior_fp is not None and prior_fp == fp:
            # LOOP BREAKER: the SAME failure verdict has recurred since the last
            # productive edit and the gate already nudged for it — no new
            # information. Halt STUCK (named) instead of re-loading forever.
            await self._loop._land_blocked(
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

        if self._loop._browser_verify_refusals < 3:
            self._loop._browser_verify_refusals += 1
            payload = (
                f"{tool_name} did not pass ({verdict.get('verdict')}). {summary}\n"
                + (f"first error: {first_error}\n" if first_error else "")
                + (f"screenshot: {screenshot}\n" if screenshot else "")
                + (f"next step: {next_action}" if next_action else "Fix the issue, then finish.")
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=payload),
                )
            )
            # Stamp the fingerprint so a repeat WITHOUT a productive edit trips the
            # loop breaker above on the next finish attempt.
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail=f"{_VERIFY_MARKER_PREFIX}{fp}",
                )
            )
            return Disp.CONTINUE

        # 3-refusal release — but NOT a silent 'done' (P1-3). The release emits a
        # DISTINCT terminal signal (StatusEvent detail="unverified_release") AND a
        # visible INCOMPLETE message BEFORE finalization, so a repeatedly-failing
        # web build can never present as a clean verified FINISHED — the UI/harness
        # keys on the marker, not on the agent's pre-gate summary text. Bounded:
        # the run still releases after the cap so it cannot hang forever.
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            )
        )
        warn = (
            f"⚠ Finished WITHOUT a passing {tool_name} verdict (3 attempts) — the "
            f"deliverable is INCOMPLETE. {summary}"
            + (f" Outstanding: {next_action}" if next_action else "")
            + " Note this clearly in your summary."
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=warn),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    def _browser_verification_unavailable(self) -> bool:
        """True when this backend cannot run browser-based verification (the process
        backend ships no `browser` tool). Mirrors `_drive_finish_browser_probe`'s
        availability check — the precise "the render/console check cannot run here"
        signal that distinguishes an UNVERIFIABLE delivery from a BROKEN app."""
        try:
            tool_names = {getattr(t, "name", None) for t in self._loop.executor.available_tools()}
        except Exception:  # noqa: BLE001 — introspection failure → assume available (cautious)
            return False
        return "browser" not in tool_names

    async def _static_deliverable_present(self) -> bool:
        """True when the static web deliverable (index.html) exists on disk in the
        sandbox workspace — the file-truth half of the honest unverifiable finish (a
        zero-action run wrote nothing, so this is False and it cannot finish)."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None or not hasattr(sbx, "file_exists"):
            return False
        try:
            return bool(await sbx.file_exists("index.html"))
        except Exception:  # noqa: BLE001 — existence probe failure → cannot confirm
            return False

    async def _record_verifier_failure_to_context(
        self, *, message: str, rel_path: str | None
    ) -> None:
        """CXT-7 — persist a verify_web_app failure to .disco/context/verifier_failures.json
        (best-effort; never alters the gate flow). Survives truncation/resume so the
        CXT-4 assembler can surface the unresolved failure to the model later."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            return
        try:
            from ...context import ArtifactMemoryStore, Severity, VerifierFailureRef

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
                self._loop.conversation_id,
                exc_info=True,
            )

    async def _maybe_honest_unverifiable_static_finish(self, verdict: dict) -> Disp | None:
        """Bug 6 — when a web build's verify FAILS ONLY because nothing is serving
        (no console/network errors, no blank-render judgement: the browser never ran)
        AND this backend cannot run a headless browser AND index.html exists on disk,
        return FALLTHROUGH with an explicit honest-unverifiable marker so a delivered
        static build FINISHES instead of pausing/STUCKing. Returns None (let the
        normal refuse/loop-break path run) for every other case — a real fail
        (console/network/blank) or a missing deliverable is NEVER converted to a
        success (W-45 preserved)."""
        http_ok = 200 <= int(verdict.get("http_status") or 0) < 400
        not_serving = (
            str(verdict.get("verdict")) == "fail"
            and not (verdict.get("console_errors") or [])
            and not (verdict.get("network_failures") or [])
            and not http_ok
        )
        if not not_serving:
            return None
        if not self._browser_verification_unavailable():
            return None
        if not await self._static_deliverable_present():
            return None
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverifiable_static_finish",
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "⚠ Finished WITHOUT a live browser verification — the static "
                        "deliverable (index.html) exists but no preview server is "
                        "reachable and this backend cannot run a headless browser, so "
                        "the render could not be checked here. The files are delivered; "
                        "note clearly in your summary that the build is UNVERIFIED."
                    ),
                ),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    async def maybe_honest_unverifiable_static_actionless_finish(self, events: list[Event]) -> bool:
        """Bug 6 — the ACTIONLESS-VALVE twin of `_maybe_honest_unverifiable_static_finish`.

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
        if not await self._static_deliverable_present():
            return False
        if not _nonbrowser_static_validation_passed(events):
            return False
        if not (self._browser_verification_unavailable() or _browser_unavailable_observed(events)):
            return False
        if _real_web_failure_evidence(events):
            return False
        # All guards hold — finish honestly instead of pausing actionless. Same honest
        # marker as the finish-gate path, then a clean terminal FINISHED (NOT PAUSED).
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverifiable_static_finish",
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "⚠ Finished WITHOUT a live browser verification — the static "
                        "deliverable (index.html) exists and a non-browser validation "
                        "passed, but this backend cannot run a headless browser and no "
                        "preview server is reachable, so the only remaining plan step "
                        "(browser verification) could not run here. The files are "
                        "delivered; note clearly in your summary that the render is "
                        "UNVERIFIED."
                    ),
                ),
            )
        )
        await self._loop._emit(StatusEvent(status=ConversationStatus.FINISHED))
        self._loop._browser_verify_refusals = 0
        return True

    async def _verifier_unavailable_disposition(self, tool_name: str = "verify_web_app") -> Disp:
        """P1-2 — disposition when the structured verifier is advertised but produced NO
        usable verdict (verifier execution error / empty / the driven verify
        failed). On the build/web surface a clean FINISH requires a real PASS
        verdict, so this must NOT fall through to finalization (the W-32 regression
        codex found). Refuse-and-continue with a "verification could not run"
        reminder while under the cap; at the cap, release EXPLICITLY as unverified
        (distinct status marker + visible message) rather than a silent clean
        finish. Bounded by the shared `_browser_verify_refusals` cap so a verifier
        that can never run still terminates."""
        if self._loop._browser_verify_refusals < 3:
            self._loop._browser_verify_refusals += 1
            await self._loop._emit(
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
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            )
        )
        await self._loop._emit(
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
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    async def _browser_verify_delegated_to_host(self, step: AgentStep, events: list[Event]) -> bool:
        if not self._host_verify_authoritative():
            return False
        if getattr(self._loop, "_host_verifier", None) is None:
            return False
        deliverable = await self._host_verify_deliverable(step, events)
        if deliverable is None or deliverable.artifact_kind != "app":
            return False
        # Delegate only when the host verifier actually RAN on the current
        # output (recorded pass/fail). An ``unavailable`` verdict (verifier
        # infrastructure could not run) or no verdict at all must keep the
        # inline browser gate as enforcement — otherwise unavailable would slip
        # through BOTH gates and an app could finish with no verification.
        verdict = _latest_host_verifier_verdict(events, _last_productive_seq(events))
        return verdict in ("pass", "fail", "unverifiable")

    async def gate_browser_verify(self, step: AgentStep, events: list[Event]) -> Disp:
        # BROWSER-VERIFY GATE — §BP-05. If web deliverable holds, refuse finish
        # until a clean browser observation (zero console errors) exists
        # since the last state-changing edit.
        verify_tool = self._active_verify_tool()
        # Strict AppKit mode is detected from the EXECUTOR (duck-typed phase
        # attribute), not only the advertised tool set: before a successful
        # app_create the phase allowlist hides verify_appkit_app, and a finish
        # in that window must still be gated (a zero-work appkit FINISH slipped
        # through here, live 2026-07-03).
        is_appkit = verify_tool == "verify_appkit_app" or (
            getattr(self._loop.executor, "appkit_phase", None) is not None
        )
        # WO-TC3: a build that installed trusted components must pass through this
        # gate even without a web-deliverable marker — the component integrity/deps/
        # probe checks ride the verify_web_app verdict. This is kept OUT of the
        # _planning_tools guard on purpose: verification is forced by the mere fact
        # that components were installed, so the security property does not depend
        # on the (currently true) invariant that the install tool only ever lives
        # in a plan-gated scope. add_trusted_component can only fire in the build
        # surface, so no non-build finish is affected in practice.
        tc_installed = _tc_components_installed(events)
        if self._loop.mode != OperatingMode.PLANNING and (
            tc_installed
            or (self._loop._planning_tools and (is_appkit or _is_web_deliverable(events)))
        ):
            # W-45: when the structured `verify_web_app` tool is in the execution
            # set (the build surface), consume its VERDICT as the primary check —
            # the clean actionable signal that kills the reload loop. The legacy
            # raw-observation path below stays for browserless backends / tests
            # without the tool (non-web + assist paths are untouched: this whole
            # block is gated on _is_web_deliverable / strict AppKit mode).
            if await self._browser_verify_delegated_to_host(step, events):
                return Disp.FALLTHROUGH
            if verify_tool is not None:
                return await self._gate_verify_web_app(events, verify_tool, step=step)
            since_seq = _last_productive_seq(events)
            # The preview platform assigns a RANDOM port — there is NO fixed :8000
            # inside the sandbox (a curl there 404s). Resolve the live preview the
            # SAME backend-aware way the verify_web_app gate does and bind every
            # browser-observation check + the driven probe + the nudge text to it.
            # None ⇒ undetectable (sandbox-less / legacy / isolated where :8000 IS
            # the app) ⇒ the readers fall back to the historical :8000 acceptance.
            target_url = await self._detect_preview_url()
            target_key = _preview_key(target_url) if target_url else None
            ok, _ = _browser_verified(events, since_seq, target_key)
            if not ok:
                # ACTIVE verify (verification-overclaim fix): the AGENT has NOT
                # produced a clean preview browser observation since the last edit.
                # Rather than wait/trust it to browse (it may have overclaimed and
                # never looked), DRIVE the browse ourselves (against the resolved
                # preview, not a dead :8000) and judge the probe on ground truth —
                # zero console errors AND a non-blank render (a page can serve 200
                # with a clean console yet mount nothing). Degrades to the prior
                # passive nudge/release on browserless backends (the probe is a
                # no-op there). Bounded by the existing 3-refusal cap below.
                if await self._drive_finish_browser_probe(target_url):
                    events = await self._loop._events()
                    probe = _latest_browser_structured(events, target_key)
                    if probe is not None:
                        probe_console_clean = not any(
                            c.get("level") == "error" for c in probe.get("console", [])
                        )
                        ok = probe_console_clean and _browser_content_meaningful(probe)
            # Messaging reads the FULL history: a post-browse edit
            # invalidates the verification but not what was seen.
            first_error = _latest_browser_error(events, target_key)
            if ok:
                self._loop._browser_verify_refusals = 0  # reset on clean pass
                # W6 vision artifact: when the browser observation includes a
                # screenshot, emit a StatusEvent so the UI / post-run harness can
                # find it (satisfies "finish-gate captures a screenshot" in the
                # no-test UI finish-gate). Only emitted when vision mode is active.
                if _vision_mode():
                    shot = _latest_browser_screenshot(events, target_key)
                    if shot:
                        await self._loop._emit(
                            StatusEvent(
                                status=ConversationStatus.RUNNING,
                                detail=f"vision_artifact:{shot}",
                            )
                        )
            elif self._loop._browser_verify_refusals < 3:
                self._loop._browser_verify_refusals += 1
                # Point the agent at the RESOLVED preview (random platform port), not
                # a dead :8000; fall back to :8000 only when undetectable.
                preview_url = target_url or "http://127.0.0.1:8000/"
                if first_error:
                    # Variant (2): quote the error
                    nudge = (
                        "Before finishing: verify your app the way a user would. "
                        f"Use the browser tool to navigate to {preview_url}, "
                        "read the CONSOLE output, and fix any errors you see. "
                        f"The last load had errors: {first_error}"
                    )
                else:
                    # Variant (1): verbatim from order
                    nudge = (
                        "Before finishing: verify your app the way a user would. "
                        f"Use the browser tool to navigate to {preview_url}, "
                        "read the CONSOLE output, and fix any errors you see. "
                        "Finish only after a clean load."
                    )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=nudge),
                    )
                )
                return Disp.CONTINUE
            else:
                # 3-refusal release valve (3): allow but warn visibly
                warn_msg = (
                    "⚠ finished WITHOUT a clean browser verification — "
                    f"last console errors: {first_error or 'none seen'}"
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=warn_msg),
                    )
                )
        return Disp.FALLTHROUGH


class _ExportRenderGateMixin(_FinishGateProto):
    async def _manifest_export_artifact_path(
        self, events: list[Event], *, require_shown: bool
    ) -> str | None:
        records = await self._artifact_manifest_records(events, consumer="export_render")
        if records is None:
            return None

        shown: list[str] = []
        unshown: list[str] = []
        seen: set[str] = set()
        for record in records:
            if _artifact_record_kind(record) == "app":
                continue
            raw_path = getattr(record, "path", None)
            if not isinstance(raw_path, str):
                continue
            path = _safe_deliverable_file_path(raw_path)
            if path is None or path in seen:
                continue
            if export_render_facts_for_path(events, path) is None:
                continue
            seen.add(path)
            if bool(getattr(record, "shown", False)):
                shown.append(path)
            else:
                unshown.append(path)

        if shown:
            return shown[-1]
        if require_shown:
            return None
        if len(unshown) == 1:
            return unshown[0]
        if len(unshown) > 1:
            _LOG.info(
                "artifact-manifest reader export path ambiguous for %s: %s",
                self._loop.conversation_id,
                unshown,
            )
        return None

    async def gate_export_render(self, step: AgentStep, events: list[Event]) -> Disp:
        """[P10] Refuse FINISHED when a rendered export (deck/document) is BLANK,
        TRUNCATED, or CORRUPT — the "looks done but the file is empty" false
        completeness. The real executor for the inert ``ExportContract.validate``
        stage.

        Reads the render facts the producer stamped from the ACTUAL output bytes
        (``latest_export_render_facts``), NOT the model's declared slide_count. A
        broken export re-enters the loop with a concrete steer; a good one (or none
        produced) falls through. Bounded by ``EXPORT_GATE_MAX_REFUSALS`` so a
        genuinely-broken renderer can't trap the run — it releases with a loud
        UNVERIFIED warning, exactly like the browser-verify valve. Decision/message
        logic is pure (``contract.export_render``); this method only emits."""
        # The latest deliverable AFTER the export decides which facts govern this
        # finish (only the latest counts: a newer files handoff means the broken deck
        # is current and must be gated).
        export_idx = latest_export_render_index(events)
        latest_post_export_deliverable: DeliverableEvent | None = None
        for i in range(len(events) - 1, export_idx, -1):
            ev = events[i]
            if isinstance(ev, DeliverableEvent):
                latest_post_export_deliverable = ev
                break
        # An app deliverable emitted AFTER the broken export means the app is the
        # current handoff and the deck is superseded — fall through (the app gates
        # own that path).
        if (
            latest_post_export_deliverable is not None
            and latest_post_export_deliverable.artifact_kind == "app"
        ):
            return Disp.FALLTHROUGH

        # A FILES handoff naming a specific stamped file is gated on THAT file's facts,
        # so delivering a known-bad export can't clear on a newer sibling's good facts.
        # Otherwise the latest stamped export governs.
        facts: ExportRenderFacts | None = None
        manifest_path = await self._manifest_export_artifact_path(
            events,
            require_shown=(
                latest_post_export_deliverable is not None
                and latest_post_export_deliverable.artifact_kind == "files"
            ),
        )
        if manifest_path is not None:
            facts = export_render_facts_for_path(events, manifest_path)
        if (
            facts is None
            and latest_post_export_deliverable is not None
            and latest_post_export_deliverable.artifact_kind == "files"
        ):
            facts = export_render_facts_for_path(events, latest_post_export_deliverable.path)
        if facts is None:
            facts = latest_export_render_facts(events)
        if facts is None or facts.ok:
            return Disp.FALLTHROUGH  # no export stamped, or it renders fine

        if count_export_gate_refusals(events, since=export_idx) >= EXPORT_GATE_MAX_REFUSALS:
            await self._loop._emit(
                StatusEvent(status=ConversationStatus.RUNNING, detail="unverified_export"),
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=export_gate_release_warning(facts)),
                )
            )
            return Disp.FALLTHROUGH

        await self._record_verifier_failure_to_context(message=facts.detail, rel_path=None)
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=export_gate_refusal_reminder(facts)),
            )
        )
        return Disp.CONTINUE


class _RenderVerifyGateMixin(_FinishGateProto):
    async def _workflow_output_path_exists(self, path: str) -> tuple[bool, str]:
        safe = _safe_deliverable_file_path(path)
        if safe is None:
            return False, path

        sbx = getattr(self._loop.executor, "sandbox", None)
        file_exists = getattr(sbx, "file_exists", None) if sbx is not None else None
        if callable(file_exists):
            file_exists_fn = cast(Callable[[str], Awaitable[object]], file_exists)
            try:
                return bool(await file_exists_fn(safe)), safe
            except Exception as exc:  # noqa: BLE001 — cannot confirm output existence
                _LOG.warning(
                    "workflow output existence check failed for %s:%s: %s",
                    self._loop.conversation_id,
                    safe,
                    exc,
                )
                return False, safe

        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        if not workspace:
            return False, safe

        from pathlib import Path

        try:
            root = Path(workspace).resolve()
            candidate = (root / safe).resolve()
            root_s = str(root)
            cand_s = str(candidate)
            if not (cand_s == root_s or cand_s.startswith(root_s.rstrip("/") + "/")):
                return False, safe
            return candidate.is_file(), safe
        except OSError as exc:
            _LOG.warning(
                "workflow output existence check failed for %s:%s from workspace: %s",
                self._loop.conversation_id,
                safe,
                exc,
            )
            return False, safe

    async def gate_workflow_output_contract(
        self,
        step: AgentStep,
        events: list[Event],  # noqa: ARG002
    ) -> Disp:
        workflow_run = getattr(self._loop, "_workflow_run", None)
        if workflow_run is None:
            return Disp.FALLTHROUGH
        contract = getattr(getattr(workflow_run, "definition", None), "output_contract", None)
        if contract is None:
            return Disp.FALLTHROUGH

        params = getattr(workflow_run, "params", {})
        if not isinstance(params, dict):
            params = {}
        output_path = _render_workflow_output_path(contract.path_template, params)
        exists, checked_path = await self._workflow_output_path_exists(output_path)
        meta = {
            "workflow_run_id": str(getattr(workflow_run, "run_id", "")),
            "workflow_output_path": checked_path,
            "workflow_output_format": contract.format,
        }
        if exists:
            self._loop._workflow_output_contract_refusals = 0
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail="workflow_output_contract_passed",
                    meta={**meta, "verdict": "pass"},
                )
            )
            return Disp.FALLTHROUGH

        # RELEASE VALVE — an uncapped refusal is the sealed done-trap wearing a
        # new mask: a model that never lands the contract path would be refused
        # finish forever. After the cap, release with an HONEST warning instead.
        if self._loop._workflow_output_contract_refusals >= _FINISH_VERIFY_CAP:
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail="workflow_output_contract_release",
                    meta={**meta, "verdict": "release"},
                )
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "⚠ Finished despite the workflow output contract not being "
                            f"satisfied after {self._loop._workflow_output_contract_refusals} "
                            f"refusals. Expected `{checked_path}` ({contract.format}) in the "
                            "workspace. The workflow output is missing; note this clearly in "
                            "the summary."
                        ),
                    ),
                )
            )
            self._loop._workflow_output_contract_refusals = 0
            return Disp.FALLTHROUGH

        self._loop._workflow_output_contract_refusals += 1
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="workflow_output_contract_refused",
                meta={**meta, "verdict": "fail"},
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "finish refused: this workflow completes by writing "
                        f"{checked_path}. Write it (file_write), then call finish. "
                        "If the workflow should not produce output, use `skip` with "
                        "a reason instead of `finish`.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return Disp.CONTINUE

    async def run_finish_verify_gates(self, step: AgentStep, events: list[Event]) -> Disp:
        """The render-verify gate sequence shared by EVERY finish path: host+browser
        app-verify (order depends on the authoritative flag) THEN the P10 export-render
        gate for file deliverables.

        Extracted so ``handle_finish_path`` (affirmative ``finish()``) and
        ``_completed_via_notify_finish`` (the actionless/notify valve) run the SAME
        gates — they had DRIFTED: the notify path historically skipped both
        ``gate_host_verify`` (so shadow agreement was never measurable on
        notify-completed builds, and an authoritative flip would leave a verify
        bypass) AND ``gate_export_render`` (a blank deck finishing via notify escaped
        the P10 check). Returns CONTINUE (a gate refused — caller must not finish),
        HALT (a gate landed the terminal status / loop-breaker), or FALLTHROUGH (all
        render-verify gates clear)."""
        disp = await self.gate_workflow_output_contract(step, events)
        if disp is Disp.CONTINUE or disp is Disp.HALT:
            return disp
        events = await self._loop._events()

        if self._host_verify_authoritative():
            disp = await self.gate_host_verify(step, events)
            if disp is Disp.CONTINUE or disp is Disp.HALT:
                return disp
            events = await self._loop._events()
            disp = await self.gate_browser_verify(step, events)
        else:
            disp = await self.gate_browser_verify(step, events)
            events = await self._loop._events()
            await self.gate_host_verify(step, events)  # shadow: advisory, records telemetry
        if disp is Disp.CONTINUE or disp is Disp.HALT:
            return disp
        # [P10] Export render-correctness gate — for a files-deliverable (deck/
        # document) the app/browser gates above fall through, so THIS is the check
        # that a blank/truncated/corrupt export can't report FINISHED.
        events = await self._loop._events()
        return await self.gate_export_render(step, events)
