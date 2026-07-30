"""Execute-and-observe, hard-reset, and the C20 subagent fan-out.

Extracted from engine.py as a stateful collaborator: `Observer` holds a back-ref
to its `AgentLoop` and runs the §4.1 execute→observe contract (incl. the F9
read-dedup gate + sandbox-restart notice), the §8 hard-reset pointer flush, and
the bounded `delegate_explore` fan-out. Bodies are byte-identical to the former
AgentLoop methods with `self.` rewritten to `self._loop.` (sibling calls stay
in-collaborator). None of these methods hold `self._loop._lock` — they run
outside the conversation lock by contract.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from ..effects import ActionProfile, EffectCapability
from ..events import ActionEvent, Event, ToolResult
from ..view import View
from .messages import _workspace_paths_from_events
from .observation_dedup import (
    _has_confirmed_prior_append as _has_confirmed_prior_append,
)
from .observation_dedup import (
    _has_confirmed_prior_read as _has_confirmed_prior_read,
)
from .observation_dedup import (
    _redirect_marker_write_with_read_provenance as _redirect_marker_write_with_read_provenance,
)
from .observation_execution import (
    _error_detail as _error_detail,
)
from .observation_execution import (
    _ground_read as _ground_read,
)
from .observation_execution import (
    execute_and_observe as _execute_and_observe,
)
from .observation_execution import (
    maybe_emit_sandbox_restart as _maybe_emit_sandbox_restart,
)

if TYPE_CHECKING:
    from .engine import AgentLoop

_DELEGATE_ACTION_PROFILE = ActionProfile(
    capabilities=frozenset(
        {
            EffectCapability.WORKSPACE_CONTENT_READ,
            EffectCapability.WORKSPACE_INVENTORY_READ,
            EffectCapability.EXTERNAL_OBSERVE,
        }
    )
)


# The helper's input is the driver's `question` + `context` joined into one
# prompt. Bound the size of each so a driver cannot grow the helper's input
# unboundedly within a single segment — the result is folded back into the
# View, which the condenser manages; keeping the helper's prompt bounded
# keeps the post-fold View growth bounded. Symmetric to the search/extract
# length budget (`packages/tools/.../retrieval.py:_EXTRACT_CHAR_BUDGET`).
_FANOUT_INPUT_MAX_CHARS = 4_000


class Observer:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def collect_pointer_manifest_paths(self, events: list[Event]) -> list[str]:
        """C16 — collect on-disk artifact paths the hard_reset tombstone will
        POINT to (NOT summarize). Three sources, all checked for existence on
        disk so the manifest stays honest (every pointer resolves):

          1. Written deliverables — paths the agent mutated this run (A8's
             mutating-tool set: file_write / file_edit / file_append /
             file_replace_lines / file_insert_lines). Most-recent-first.
          2. Spill logs — `.disco-spill-<uuid>.log` files in the workspace
             (T10's overflow channel; engine.py:1787 already filters them
             from the snapshot because the model has no business re-reading
             them in bulk — but a hard_reset is exactly the time the model
             DOES need to be able to re-read them selectively, so we POINT
             at them instead of summarizing).
          3. `.pmx/MEMORY.md` — standing memory the agent recorded this run
             (C5's MEMORY channel). The view-channel copy is lost on a
             hard filesystem reset; the on-disk copy survives.

        Returns a de-duplicated list, most-recent-first. Paths that don't
        resolve (the sandbox is gone, the file was wiped) are DROPPED — the
        manifest is required to be honest, and a pointer to a missing file
        is worse than no pointer."""
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(p: str | None) -> None:
            if not p or p in seen:
                return
            seen.add(p)
            candidates.append(p)

        # 1. Working-set mutating paths (the agent's deliverable surface).
        mutated, _read = _workspace_paths_from_events(events)
        for p in mutated:
            _add(p)

        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            return candidates

        # 2. Spill logs in the workspace.
        try:
            workspace = getattr(sbx, "workspace_path", None) or ""
            if workspace:
                names = await sbx.list_dir(workspace)
            else:
                # Some backends don't expose workspace_path; fall back to
                # the root. We do not hard-fail on a missing attribute —
                # the manifest degrades gracefully (no spill pointers).
                names = await sbx.list_dir("")  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 — list_dir can raise on a dead backend
            names = []
        for name in names:
            if not isinstance(name, str):
                continue
            bn = os.path.basename(name)
            if bn.startswith(".disco-spill-"):
                _add(name)

        # 3. the standing-memory mirror if present (.disco/, or legacy .pmx/).
        for memory_path in (".disco/MEMORY.md", ".pmx/MEMORY.md"):
            try:
                await sbx.read_file(memory_path)
                _add(memory_path)
                break
            except (FileNotFoundError, NotADirectoryError):
                continue
            except Exception:  # noqa: BLE001 — a dead sandbox just skips the pointer
                break

        return candidates

    async def hard_reset(self, events: list[Event]) -> bool:
        """Forget-and-recover after a context-window error (§8). Returns True
        if a tombstone was appended (progress made).

        C16 — this is a POINTER-ONLY flush (NOT a prose recap). The tombstone's
        summary is a manifest of on-disk artifact paths the model can re-read
        selectively — never a freeform prose summary. The dropped span is
        already too large to re-summarize usefully (it overflowed the
        context window), and a lossy prose recap is worse than pointing at
        the bytes on disk. The soft-condense path (should_condense → condense
        on a token-bound trigger) is byte-unchanged — it still produces a
        prose summary via the summarizer. Only the hard_reset call site
        changes (it now passes reason="hard_reset" + the collected paths)."""
        artifact_paths = await self.collect_pointer_manifest_paths(events)
        tombstone = await self._loop.condenser.condense(
            events,
            View.of(events),
            summarizer=self._loop.summarizer,
            reason="hard_reset",
            artifact_paths=artifact_paths,
        )
        if tombstone is not None:
            await self._loop._emit(tombstone)
            return True
        return False

    async def execute_and_observe(self, action: ActionEvent) -> None:
        """Execute one action through the ordered observation collaborator."""
        await _execute_and_observe(
            self._loop,
            action,
            restart_emitter=self.maybe_emit_sandbox_restart,
        )

    async def maybe_emit_sandbox_restart(
        self,
        sbx: object | None,
        gen_before: int,
    ) -> None:
        await _maybe_emit_sandbox_restart(self._loop, sbx, gen_before)

    async def run_fanout(self, args: dict, events: list[Event], *, call_id: str = "") -> ToolResult:
        """C20 — dispatch the read-only Explore/Plan helper task and return the
        joined result as a `ToolResult` (the caller — the `delegate_explore`
        intercept path — folds it back as a paired `ObservationEvent`).

        The default implementation is a THIN, DETERMINISTIC STUB: it
        serializes the helper's question + context into a short structured
        report and returns it. This is a TEST SEAM — tests override
        `_run_fanout` on the loop instance to control the response. In a
        production wiring, the stub is replaced by a real LLM round-trip
        that offers the helper ONLY the read-only tools
        (file_read / file_list / search / extract) and folds the response
        back the same way. The shape of the return (a `ToolResult`) does
        not change between stub and production; the loop's
        observe-and-continue path is the same either way.

        Why a stub default (vs. a real LLM call here):
          * bounded test — no model required, no flakiness, no
            per-call latency, no cost (a real round-trip would burn
            tokens on every fan-out);
          * the BOUNDED cap + the dispatch+join shape are the load-
            bearing pieces for the C20 acceptance; the helper's
            INTERNAL logic is a separate concern;
          * the production hook is a one-method override; the test
            seam and the production hook are the same surface.

        Returns a `ToolResult(success=True, content=..., structured=...)`
        on a clean dispatch. The `call_id` echoes the loop's call_id so
        the resulting ObservationEvent stays properly paired with the
        proposed ActionEvent the loop just emitted (KV-cache stability,
        same discipline as the remember/serve intercept paths)."""

        # Import ToolResult locally to avoid a circular-import risk at
        # module-load time (the engine module is imported widely; keeping
        # the symbol scoped to the function is the conservative choice).
        from disco.core import ToolResult as _ToolResult

        # Length-bound the question + context (the engine truncates
        # BEFORE dispatching the seam, so a model that overrides
        # _run_fanout also sees a bounded input — the bound is a
        # property of the fan-out, not of this stub). Defensive: the
        # engine's interception path also length-bounds, so by the
        # time we get here the fields are already short; the defensive
        # bound here is a belt-and-suspenders for a direct override
        # that bypasses the engine's path (a unit test, a regression
        # case).
        question = str(args.get("question") or "").strip()
        context = str(args.get("context") or "").strip()
        # Defensive bound (the engine's path already length-bounds; this
        # is a belt-and-suspenders for a direct override).
        _trunc_marker = "…[truncated]"
        if len(question) > _FANOUT_INPUT_MAX_CHARS:
            question = question[: _FANOUT_INPUT_MAX_CHARS - len(_trunc_marker)] + _trunc_marker
        if len(context) > _FANOUT_INPUT_MAX_CHARS:
            context = context[: _FANOUT_INPUT_MAX_CHARS - len(_trunc_marker)] + _trunc_marker
        # The stub's report: a structured, model-readable summary. In a
        # production wiring this is the helper's actual response; here
        # it is a deterministic echo so tests can assert on the shape
        # (and so a caller that does not override the seam still gets
        # a clean, honest "helper ran" report).
        return _ToolResult(
            call_id=call_id,  # echoes the proposed call's call_id (KV-cache pairing)
            tool_name="delegate_explore",
            success=True,
            content=(
                f"[C20 fan-out #{self._loop._fanout_count}/{self._loop._fanout_max}] "
                f"helper dispatched: "
                f'question="{question[:80]}{"…" if len(question) > 80 else ""}"'
                + (f" context={len(context)} chars" if context else "")
                + ". (Default stub — override `_run_fanout` for a real subagent.)"
            ),
            structured={
                "fanout_index": self._loop._fanout_count,
                "fanout_max": self._loop._fanout_max,
                "question_chars": len(question),
                "context_chars": len(context),
                "stub": True,
            },
            action_profile=_DELEGATE_ACTION_PROFILE,
        )
