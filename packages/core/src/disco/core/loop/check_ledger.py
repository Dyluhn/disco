"""Check ledger — what every shell run observed, recorded where condensation cannot reach.

Phase 1 of the verification ledger (observe only). Every ``shell`` / ``shell_exec``
observation or error carries ``meta["check"]``: the call's fingerprint, the workspace
tree digest before and after the run, the exit code, and the busy sessions before and
after. Nothing is memoised or gated yet; :func:`memo_hits` counts what a later phase
would have answered from the record instead of executing.

The digest is computed inside the sandbox with coreutils only (``find | sort |
sha256sum``), so it works on every backend that has ``exec_shell``. It covers source
files: version-control, dependency, build-cache and harness directories are excluded,
and so are runtime-state files (databases, logs, sockets) that a test itself mutates.
A digest that cannot be computed is recorded as ``None``; a record without a digest
never counts as a memo hit (fail closed).
"""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from ..events import ActionEvent, AgentErrorEvent, Event, ObservationEvent
from ..script_identity import SHELL_TOOLS
from ..tool_fingerprint import tool_call_fingerprint

LEDGER_TOOLS = SHELL_TOOLS
TREE_DIGEST_EXCLUDED_DIRS: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pmx",
    ".disco",
    ".bin",
    ".cache",
    ".turbo",
    "coverage",
    "target",
)
TREE_DIGEST_EXCLUDED_GLOBS: tuple[str, ...] = (
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "*.pid",
    "*.sock",
)
TREE_DIGEST_TIMEOUT_S = 20


def tree_digest_command() -> str:
    """The in-sandbox pipeline: sha256 over sorted (path, sha256) pairs of source files."""
    prune = " -o ".join(f"-name {shlex.quote(d)}" for d in TREE_DIGEST_EXCLUDED_DIRS)
    skip = " ".join(f"! -name {shlex.quote(g)}" for g in TREE_DIGEST_EXCLUDED_GLOBS)
    return (
        f"find . \\( {prune} \\) -prune -o -type f {skip} -print0"
        " | LC_ALL=C sort -z | xargs -0r sha256sum | sha256sum | cut -d' ' -f1"
    )


@dataclass(frozen=True)
class CheckSnapshot:
    """The workspace state a check ran against: source digest + busy sessions."""

    tree_digest: str | None
    sessions: tuple[str, ...]


async def snapshot(sandbox: object | None) -> CheckSnapshot | None:
    """Digest the workspace and list busy sessions; None when there is no sandbox."""
    exec_shell = getattr(sandbox, "exec_shell", None)
    if not callable(exec_shell):
        return None
    run = cast(Callable[..., Awaitable[Any]], exec_shell)
    digest: str | None = None
    try:
        result = await run(tree_digest_command(), timeout_s=TREE_DIGEST_TIMEOUT_S)
        out = (result.stdout or "").strip()
        if result.exit_code == 0 and not result.timed_out and len(out) == 64:
            digest = out
    except Exception:  # noqa: BLE001 — the ledger never breaks the run; fail closed
        digest = None
    return CheckSnapshot(tree_digest=digest, sessions=await _busy_sessions(sandbox))


async def _busy_sessions(sandbox: object) -> tuple[str, ...]:
    manager = getattr(sandbox, "sessions", None)
    list_sessions = getattr(manager, "list", None)
    if not callable(list_sessions):
        return ()
    try:
        infos = await cast(Callable[[], Awaitable[Any]], list_sessions)()
    except Exception:  # noqa: BLE001 — same fail-closed rule as the digest
        return ()
    return tuple(sorted(info.name for info in infos if getattr(info, "busy", False)))


def ledger_tool(action: ActionEvent) -> bool:
    return action.tool_call is not None and action.tool_call.tool_name in LEDGER_TOOLS


def check_meta(
    action: ActionEvent,
    before: CheckSnapshot | None,
    after: CheckSnapshot | None,
    exit_code: int | None,
) -> dict[str, Any]:
    """The ``meta["check"]`` record for one shell run."""
    tool_call = action.tool_call
    assert tool_call is not None
    return {
        "fingerprint": check_fingerprint(tool_call),
        "tree_before": before.tree_digest if before else None,
        "tree_after": after.tree_digest if after else None,
        "exit_code": exit_code,
        "sessions_before": list(before.sessions) if before else [],
        "sessions_after": list(after.sessions) if after else [],
    }


def exit_code_of(structured: dict[str, Any] | None) -> int | None:
    value = (structured or {}).get("exit_code")
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class CheckRecord:
    seq: int
    action_id: str
    fingerprint: str
    tree_before: str | None
    tree_after: str | None
    exit_code: int | None
    sessions_before: tuple[str, ...]
    sessions_after: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @property
    def pure(self) -> bool:
        """The run changed neither the source tree nor the set of live sessions."""
        return (
            self.tree_before is not None
            and self.tree_before == self.tree_after
            and self.sessions_before == self.sessions_after
        )


def check_records(events: list[Event]) -> list[CheckRecord]:
    """Every recorded check in seq order, from observation and error events alike."""
    records: list[CheckRecord] = []
    for event in events:
        if not isinstance(event, ObservationEvent | AgentErrorEvent) or event.seq is None:
            continue
        raw = event.meta.get("check")
        if not isinstance(raw, dict) or event.action_id is None:
            continue
        records.append(
            CheckRecord(
                seq=event.seq,
                action_id=event.action_id,
                fingerprint=str(raw.get("fingerprint") or ""),
                tree_before=raw.get("tree_before"),
                tree_after=raw.get("tree_after"),
                exit_code=raw.get("exit_code"),
                sessions_before=tuple(raw.get("sessions_before") or ()),
                sessions_after=tuple(raw.get("sessions_after") or ()),
            )
        )
    return records


def memo_source(record: CheckRecord, prior: list[CheckRecord]) -> CheckRecord | None:
    """The earlier record that already answers ``record``, or None.

    A hit needs the same call, a prior run that passed without changing anything, and
    the same source digest and live sessions now as then. Unknown digests never match.
    """
    if record.tree_before is None:
        return None
    for candidate in reversed(prior):
        if candidate.fingerprint != record.fingerprint:
            continue
        if (
            candidate.passed
            and candidate.pure
            and candidate.tree_after == record.tree_before
            and candidate.sessions_after == record.sessions_before
        ):
            return candidate
        return None
    return None


def memo_hits(events: list[Event]) -> list[tuple[CheckRecord, CheckRecord]]:
    """(run, earlier run that already answered it) for every would-be memo hit."""
    hits: list[tuple[CheckRecord, CheckRecord]] = []
    seen: list[CheckRecord] = []
    for record in check_records(events):
        source = memo_source(record, seen)
        if source is not None:
            hits.append((record, source))
        seen.append(record)
    return hits


def memo_disagreements(events: list[Event]) -> list[tuple[CheckRecord, CheckRecord]]:
    """Would-be hits whose real run exited differently from the recorded answer.

    Same call, same source, same sessions, different result: the check depends on
    something the digest does not cover (runtime state, timing, the network). In the
    observe-only phase this is the measure of how often memoisation would have lied.
    """
    return [(run, source) for run, source in memo_hits(events) if run.exit_code != source.exit_code]


# --- phase 2: show the ledger -----------------------------------------------------

MUTATE_CAPABILITY = "workspace.mutate"
CHECKS_SHOWN = 12
COMMAND_CHARS = 90
CHECKS_HEADER = "Checks (recorded shell runs; current = nothing changed since it ran):"


@dataclass(frozen=True)
class Mutation:
    """An action that may have changed source files."""

    seq: int
    tool: str
    path: str | None

    def describe(self) -> str:
        target = f" {self.path}" if self.path else ""
        return f"{self.tool}{target} at step {self.seq}"


def _profile_capabilities(result: Any) -> set[str]:
    profile = getattr(result, "action_profile", None)
    caps = getattr(profile, "capabilities", None)
    if caps is None and isinstance(profile, dict):
        caps = profile.get("capabilities")
    return {str(getattr(c, "value", c)) for c in (caps or ())}


def _actions_by_id(events: list[Event]) -> dict[str, ActionEvent]:
    return {e.id: e for e in events if isinstance(e, ActionEvent)}


def mutations(events: list[Event]) -> list[Mutation]:
    """Observed actions that may have changed source, in seq order.

    A tool that declares workspace mutation counts, except a recorded shell run whose
    digest proves the tree did not change. An unknown digest counts (fail closed).
    """
    actions = _actions_by_id(events)
    out: list[Mutation] = []
    for event in events:
        if not isinstance(event, ObservationEvent) or event.seq is None:
            continue
        action = actions.get(event.action_id)
        tool = event.tool_result.tool_name
        if action is not None and ledger_tool(action):
            # The record is the authority for a shell run: an unchanged digest proves
            # purity; a changed or unknown one counts as a mutation.
            raw = event.meta.get("check") or {}
            before, after = raw.get("tree_before"), raw.get("tree_after")
            if before is not None and before == after:
                continue
        elif MUTATE_CAPABILITY not in _profile_capabilities(event.tool_result):
            continue
        args = action.tool_call.arguments if action is not None and action.tool_call else {}
        path = args.get("path") if isinstance(args.get("path"), str) else None
        out.append(Mutation(seq=event.seq, tool=tool, path=path))
    return out


@dataclass(frozen=True)
class CheckStatus:
    fingerprint: str
    command: str
    seq: int
    exit_code: int | None
    state: str  # "current" | "stale" | "failed" | "flaky"
    staled_by: Mutation | None

    def render(self) -> str:
        exit_note = f"exit {self.exit_code}" if self.exit_code is not None else "no exit code"
        if self.state == "stale" and self.staled_by is not None:
            state = f"stale: {self.staled_by.describe()}"
        elif self.state == "flaky":
            state = "flaky: exited differently on unchanged inputs; always executed"
        else:
            state = self.state
        return f"- [{state}] {exit_note} at step {self.seq}  {self.command}"


def _command_text(action: ActionEvent | None) -> str:
    if action is None or action.tool_call is None:
        return "?"
    command = str(action.tool_call.arguments.get("command") or "")
    command = " ".join(command.split())
    return command if len(command) <= COMMAND_CHARS else command[: COMMAND_CHARS - 1] + "…"


def check_statuses(events: list[Event], *, limit: int = CHECKS_SHOWN) -> list[CheckStatus]:
    """The latest run of each distinct pure check, oldest first, capped to the most recent.

    Runs that changed the tree are not checks and are left out. A check is current when
    no mutation follows it, stale when one does, failed when its exit code is not 0.
    """
    actions = _actions_by_id(events)
    changes = mutations(events)
    records = check_records(events)
    flaky = flaky_fingerprints(records)
    latest: dict[str, CheckRecord] = {}
    for record in records:
        if record.tree_before is None or record.tree_before != record.tree_after:
            continue
        latest[record.fingerprint] = record
    statuses: list[CheckStatus] = []
    for record in sorted(latest.values(), key=lambda r: r.seq)[-limit:]:
        staled_by = next((m for m in changes if m.seq > record.seq), None)
        if record.fingerprint in flaky:
            state = "flaky"
        elif not record.passed:
            state = "failed"
        elif staled_by is not None:
            state = "stale"
        else:
            state = "current"
        statuses.append(
            CheckStatus(
                fingerprint=record.fingerprint,
                command=_command_text(actions.get(record.action_id)),
                seq=record.seq,
                exit_code=record.exit_code,
                state=state,
                staled_by=staled_by if state == "stale" else None,
            )
        )
    return statuses


def render_checks(statuses: list[CheckStatus]) -> str | None:
    if not statuses:
        return None
    return "\n".join([CHECKS_HEADER, *(s.render() for s in statuses)])


def staled_checks_line(events_before: list[Event]) -> str | None:
    """The line an edit result carries: which current passing checks this edit staled."""
    current = [s for s in check_statuses(events_before, limit=CHECKS_SHOWN) if s.state == "current"]
    if not current:
        return None
    names = "; ".join(s.command for s in current)
    plural = "" if len(current) == 1 else "s"
    return f"[staled {len(current)} recorded check{plural}: {names} — re-run after your changes]"


@dataclass(frozen=True)
class StaleSession:
    name: str
    started_seq: int
    changed_by: Mutation

    def render(self) -> str:
        return (
            f"[session '{self.name}' has been running since step {self.started_seq}; source "
            f"changed since ({self.changed_by.describe()}) — it is serving old code until restarted]"
        )


def _session_starts(events: list[Event]) -> dict[str, int]:
    """Session name → seq of the latest `shell_exec` that left a process running in it."""
    actions = _actions_by_id(events)
    started: dict[str, int] = {}
    for event in events:
        if not isinstance(event, ObservationEvent) or event.seq is None:
            continue
        if event.tool_result.tool_name != "shell_exec":
            continue
        if not (event.tool_result.structured or {}).get("running"):
            continue
        action = actions.get(event.action_id)
        arguments = action.tool_call.arguments if action is not None and action.tool_call else {}
        name = str(arguments.get("session") or "")
        if name:
            started[name] = event.seq
    return started


def stale_sessions(events: list[Event], busy: Sequence[str]) -> list[StaleSession]:
    """Busy sessions whose process started before the latest source change."""
    started = _session_starts(events)
    changes = mutations(events)
    out: list[StaleSession] = []
    for name in busy:
        seq = started.get(name)
        if seq is None:
            continue
        change = next((m for m in changes if m.seq > seq), None)
        if change is not None:
            out.append(StaleSession(name=name, started_seq=seq, changed_by=change))
    return out


# --- phase 3: answer a repeat from the record ------------------------------------------

MEMO_HEADER = (
    "Unchanged since step {seq}: same command, same source digest, same live sessions. "
    "The recorded result (exit {exit}) follows; the command was not re-executed. "
    "Pass force=true to run it anyway."
)
FORCE_ARG = "force"


def memo_enabled() -> bool:
    """`DISCO_CHECK_MEMO` (default on) — the one switch that turns memoised answers off."""
    from ..env import disco_env

    return (disco_env("CHECK_MEMO", "on") or "").strip().lower() in {"1", "true", "yes", "on"}


def check_fingerprint(tool_call: Any) -> str:
    """The call's identity for the ledger: `force` asks for execution, it is not a different check."""
    arguments = {k: v for k, v in (tool_call.arguments or {}).items() if k != FORCE_ARG}
    return tool_call_fingerprint(tool_call.tool_name, arguments)


def forced(action: ActionEvent) -> bool:
    return bool(action.tool_call and action.tool_call.arguments.get(FORCE_ARG))


def _hits(records: list[CheckRecord]) -> list[tuple[CheckRecord, CheckRecord]]:
    hits: list[tuple[CheckRecord, CheckRecord]] = []
    seen: list[CheckRecord] = []
    for record in records:
        source = memo_source(record, seen)
        if source is not None:
            hits.append((record, source))
        seen.append(record)
    return hits


def flaky_fingerprints(records: list[CheckRecord]) -> frozenset[str]:
    """Commands that ever exited differently on unchanged inputs. Never memoised."""
    return frozenset(
        run.fingerprint for run, source in _hits(records) if run.exit_code != source.exit_code
    )


def repeatable(fingerprint: str, records: list[CheckRecord]) -> bool:
    """A command has proven repeatable when one real repeat agreed with its source and none
    ever disagreed. Its first repeat therefore always executes."""
    if fingerprint in flaky_fingerprints(records):
        return False
    return any(
        run.fingerprint == fingerprint and run.exit_code == source.exit_code
        for run, source in _hits(records)
    )


def memo_source_for(
    action: ActionEvent, before: CheckSnapshot | None, events: list[Event]
) -> CheckRecord | None:
    """The recorded run that answers this call without executing it, or None."""
    if before is None or before.tree_digest is None or not memo_enabled() or forced(action):
        return None
    assert action.tool_call is not None
    fingerprint = check_fingerprint(action.tool_call)
    records = check_records(events)
    if not repeatable(fingerprint, records):
        return None
    candidate = CheckRecord(
        seq=0,
        action_id=action.id,
        fingerprint=fingerprint,
        tree_before=before.tree_digest,
        tree_after=before.tree_digest,
        exit_code=None,
        sessions_before=before.sessions,
        sessions_after=before.sessions,
    )
    return memo_source(candidate, records)


def memo_content(source: CheckRecord, source_content: str) -> str:
    header = MEMO_HEADER.format(seq=source.seq, exit=source.exit_code)
    body = (source_content or "").rstrip()
    return f"{header}\n---\n{body}" if body else header


def memo_meta(action: ActionEvent, before: CheckSnapshot, source: CheckRecord) -> dict[str, Any]:
    record = check_meta(action, before, before, source.exit_code)
    record["memo_of"] = source.seq
    return record
