"""View projection for the Disco Operator.

Extracted from ``OperatorClient._derive_view`` (PKG-08-VERIFY) so the
event-folding logic is cohesive pure helpers rather than one large inline
loop body. The parent module's ``_derive_view`` delegates to
``fold_view_events``; this module owns the per-event folding and the
observation-tool dispatch that produce the exact same view dict.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["fold_view_events"]


def _observation_updates(
    name: str | None,
    content: str,
    st: dict[str, Any],
    files: dict[str, int],
    terminal: list[str],
) -> tuple[dict[str, Any] | None, Any, str | None, list[Any]]:
    """Process one observation's tool-specific fields, mutating files/terminal.

    ``files`` and ``terminal`` are mutated in place so ``file_list`` preserves
    the original ``setdefault`` semantics (never overwrites a known file's
    byte count). Returns ``(browser, progress, answer, sources)`` — the scalar
    accumulators the caller merges into its state dict.
    """
    browser: dict[str, Any] | None = None
    progress: Any = None
    answer: str | None = None
    sources: list[Any] = []
    if name == "file_write":
        m = re.search(r"wrote\s+(\d+)\s+bytes?\s+to\s+(.+)", content)
        if m:
            files[m.group(2).strip()] = int(m.group(1))
    elif name == "file_list" and isinstance(st.get("entries"), list):
        for p in st["entries"]:
            files.setdefault(str(p), 0)
    elif name in ("run_command", "bash", "exec", "server_status"):
        terminal.append(content[:300])
    elif name == "browser":
        browser = {kk: st.get(kk) for kk in ("url", "title", "console", "network")}
    elif name == "update_plan_progress":
        progress = st.get("steps") or content
    elif name in ("research_answer", "answer"):
        answer = content or None
        sources = st.get("sources") or st.get("citations") or []
    return browser, progress, answer, sources


def _fold_observation(
    e: dict[str, Any],
    activity: list[str],
    files: dict[str, int],
    terminal: list[str],
    state: dict[str, Any],
) -> None:
    """Fold one observation event into the accumulators.

    ``state`` holds the mutable scalar accumulators (``browser``, ``progress``,
    ``answer``, ``sources``) keyed by name so this helper can update them
    without a long return tuple. The activity/files/terminal lists are passed
    directly and mutated in place.
    """
    tr = e.get("tool_result") or {}
    name = tr.get("tool_name")
    content = str(tr.get("content") or "")
    st = tr.get("structured") or {}
    activity.append(f"{name}: {content[:90]}" if content else f"{name}")
    u_browser, u_progress, u_answer, u_sources = _observation_updates(
        name, content, st, files, terminal
    )
    if u_browser is not None:
        state["browser"] = u_browser
    if u_progress is not None:
        state["progress"] = u_progress
    if u_answer is not None:
        state["answer"] = u_answer or state.get("answer")
    if u_sources:
        state["sources"] = u_sources


def fold_view_events(status: str | None, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the FULL visible state of the surface from the event log.

    This is the same things a human sees with their eyes: the plan + its
    progress, the activity feed, the workspace files, terminal/server output,
    the rendered-page observation, the deliverables, the answer/sources, and
    the pending gate.
    """
    plan: dict[str, Any] | None = None
    progress: Any = None
    activity: list[str] = []
    files: dict[str, int] = {}
    terminal: list[str] = []
    browser: dict[str, Any] | None = None
    deliverables: list[dict[str, Any]] = []
    messages: list[str] = []
    answer: str | None = None
    sources: list[Any] = []
    # Mutable scalar accumulators passed to _fold_observation so it can update
    # them without a long return tuple.
    state: dict[str, Any] = {"browser": None, "progress": None, "answer": None, "sources": []}
    for e in events:
        k = e.get("kind")
        if k == "plan":
            plan = {"summary": e.get("summary"), "steps": e.get("steps")}
        elif k == "observation":
            _fold_observation(e, activity, files, terminal, state)
        elif k == "deliverable":
            deliverables.append(
                {
                    "kind": e.get("artifact_kind"),
                    "path": e.get("path"),
                    "url": e.get("deployment_url"),
                }
            )
        elif k == "message" and e.get("source") == "agent":
            msg = e.get("message", {}).get("content")
            if msg:
                messages.append(str(msg)[:600])
    browser = state["browser"]
    progress = state["progress"]
    answer = state["answer"]
    sources = state["sources"]
    return {
        "status": status,
        "plan": plan,
        "plan_progress": progress,
        "activity": activity[-25:],
        "files": files,
        "terminal": terminal[-8:],
        "browser": browser,
        "deliverables": deliverables,
        "messages": messages[-4:],
        "answer": answer,
        "sources": sources[:8] if sources else [],
    }