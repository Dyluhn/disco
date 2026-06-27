"""Definition-of-Done (DoD) spec — external acceptance predicates the agent
cannot author or mutate.

# Why (the moat)

The existing `_finish_verify_passed` in `loop/engine.py` accepts a `command`
string the AGENT supplies itself to gate its own `finish` action. That makes the
agent the judge of its own work: a lazy or self-deceiving model can pass
`echo ok` and call itself done. The structural fix is an **external** DoD
spec — a list of machine-checkable acceptance predicates the model itself
never writes, captures, or edits. The spec is captured at task start (from
the user request / `submit_plan`) and stored **outside the agent-editable
event stream** so no agent tool can rewrite it later.

This module is **storage + accessor only**. It defines the spec shape, the
immutability primitive, and the store-level write-once gate. Evaluation of
the predicates is a SEPARATE step (C1b) that runs in a fresh context.

# Why these predicate kinds

Borrowed (and tightened) from the research recommendation in
`docs/manus-gap-analysis-addendum.md` §4 ("per-step verify predicates"):

    {"kind": "file_exists", "path": "..."}
    {"kind": "shell_exit_zero", "cmd": "..."}   # NB: gated same as a normal action
    {"kind": "http_ok", "url": "...", "expect_status": 200}

Each is a STRUCTURED, machine-checkable assertion the model can't fake with
prose. A `command` predicate is the same shape as the agent's own verify
command (and passes the same hard-deny + confirmation gates) — but the agent
didn't write it. That's the load-bearing difference.

# Storage — outside the agent-editable event stream

The `EventStore` already partitions per-conversation state into:

  * `events(conversation_id, seq, id, kind, payload, ...)` — the append-only
    event log. The agent's tools (`file_*`, `shell`, `submit_plan`,
    `plan_step`, …) all become events on this log. It is, by design, the one
    surface the agent can influence.
  * `conversations(conversation_id, owner_id, ...)` — server-managed metadata
    (title, surface, status, autonomous flag, …). The agent does not have a
    tool that writes here.

The DoD spec lives in a NEW sibling table, `dod_specs`, with the same owner-
scoped discipline as `conversations`. There is NO agent-reachable path that
mutates this table: the store's only writer is `set_dod_spec()`, and that
function refuses to overwrite a spec that is already set (the WRITE-ONCE
gate). The agent's tool registry cannot reach the store's private SQLite
connection; even the shell tool, run inside the agent's sandbox, is fenced
off from `PMX_DB` (which lives outside the workspace by deployment design).

# Immutability-via-agent-surface

A agent-reachable "edit" of the stored spec is impossible by construction:

  1. There is NO agent tool that calls `set_dod_spec` or `replace_dod_spec`.
     The store methods are server-side only. The agent's tool surface is
     enumerated in `disco.tools.builtin` and the new module is not in it.
  2. The store's `set_dod_spec` is write-once per conversation: a second
     call with the same `conversation_id` raises `DoDSpecAlreadySet` and
     leaves the original intact. `replace_dod_spec` exists as a named,
     always-raise hook for any future "weaken" affordance to fail loudly
     instead of silently mutating.
  3. `DoDSpec.model_config` is `frozen=True` and the predicates are themselves
     frozen Pydantic models — even in-process mutation is a `ValidationError`.

If a future change ever needs to "weaken" a spec (e.g. user explicitly
amends their task and wants the DoD relaxed), the user-facing path must
go through a new conversation or a deliberate, audit-logged replace —
never a tool the agent can call. That's the C1c wire step; the storage
and immutability primitives here are what make that wire step correct.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class DoDSpecAlreadySet(Exception):
    """Raised when a code path attempts to write a DoD spec that already exists
    for a conversation. The original spec is preserved; no partial overwrite
    is performed. This is the WRITE-ONCE gate — every "edit" or "replace" of a
    stored spec surfaces here so it fails loudly, not silently."""


# ---- predicate kinds --------------------------------------------------------


class FileExistsPredicate(BaseModel):
    """A file must exist at `path` (relative to the conversation workspace root
    or absolute — the evaluator pins the resolution rule in C1b). The agent
    cannot pass a regex or a glob: the path is a single literal. Absence ⇒ FAIL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["file_exists"] = "file_exists"
    path: str = Field(min_length=1)
    # MONOTONIC-RENAME support (v2): when a revision MOVES a previously-required
    # deliverable to a new path, the new predicate sets `renamed_from` to the OLD
    # path. This lets the monotonic DoD extension allow a genuine rename
    # (old→new) WITHOUT allowing the model to silently DROP a committed
    # deliverable: an old path that simply vanishes (neither still required nor
    # explicitly renamed-from) is a weakening and is rejected. Comparison-ONLY —
    # the evaluator ignores it (it only checks `path`).
    renamed_from: str | None = Field(default=None)


class CommandExitPredicate(BaseModel):
    """A shell command must exit with `expect_exit` (default 0). The command
    string is a literal — the agent cannot pass a shell-quoted trick. C1b's
    evaluator will run this through the same hard-deny gate and confirmation
    policy as any other action; a gated command is a HARD-FAIL, not a
    silent pass (mirrors `_finish_verify_passed`'s discipline)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["command"] = "command"
    cmd: str = Field(min_length=1)
    expect_exit: int = 0


class HTTPOkPredicate(BaseModel):
    """An HTTP GET to `url` must respond with status `expect_status` (default
    200). The agent cannot pass arbitrary response-body assertions here; the
    status code is the only check. C1b's evaluator runs this through the same
    egress allow-list the live loop uses (no SSRF, no private IPs unless
    allowlisted)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["http_ok"] = "http_ok"
    url: str = Field(min_length=1)
    expect_status: int = 200


# The discriminated union over the three predicate kinds. Pydantic v2's
# `TypeAdapter` is the canonical way to validate/serialize it; the `kind`
# discriminator keeps wire form terse and the per-kind validation local.
DoDPredicate = Annotated[
    FileExistsPredicate | CommandExitPredicate | HTTPOkPredicate,
    Field(discriminator="kind"),
]

_doDPredicateAdapter = TypeAdapter(DoDPredicate)


def predicate_from_obj(obj: dict[str, Any]) -> DoDPredicate:
    """Validate a plain dict against the predicate union. Rejects unknown
    kinds and missing fields. Convenience for callers that have a dict
    (e.g. parsed from a request body)."""
    return _doDPredicateAdapter.validate_python(obj)


def predicate_to_dict(p: DoDPredicate) -> dict[str, Any]:
    """Serialize a predicate to a JSON-safe dict. Stable field order via
    Pydantic — round-trips through `predicate_from_obj`."""
    if isinstance(p, BaseModel):
        return p.model_dump(mode="json")
    raise TypeError(f"not a predicate: {type(p).__name__}")


# ---- the spec ---------------------------------------------------------------


class DoDSpec(BaseModel):
    """A write-once Definition-of-Done for one conversation.

    Captured at task START (from the user request or the approved
    `submit_plan`). Lives in the store's `dod_specs` table — a sibling to
    `conversations` and `events`, OUTSIDE the agent-editable event stream.
    Frozen: in-process mutation is a `ValidationError`; the store refuses
    to overwrite an existing row.

    `predicates` is the ORDERED list of acceptance checks. ALL must pass for
    the spec to be satisfied; partial pass is failure (C1b is responsible for
    the failure surface). `note` is an optional, agent-irrelevant human note
    carried alongside — not consulted by the evaluator, not LLM-converted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    predicates: list[DoDPredicate] = Field(default_factory=list)
    # ISO-8601 UTC. Carried for ops / audit only — never consulted by the
    # evaluator. Default factory so constructed-from-dict callers don't have
    # to set it; stored value is what the spec was first frozen at.
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Optional human note (e.g. "task B7 acceptance"). Never used by the
    # evaluator. Empty string by default.
    note: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize the spec to a JSON-safe dict (Pydantic round-trips)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_json_dict(cls, obj: dict[str, Any]) -> DoDSpec:
        """Validate a plain dict against the spec. Rejects unknown fields and
        invalid predicate shapes."""
        return cls.model_validate(obj)


def is_monotonic_extension(old: DoDSpec, new: DoDSpec) -> bool:
    """True iff `new` only ADDS to / RENAMES within `old` — never WEAKENS it.

    This is the v2 monotonic guard that lets a mid-build steer EXTEND the
    Definition-of-Done (e.g. "also add a Contact page" → require contact.html)
    while preserving the security property the write-once design protects: the
    agent can never DROP or relax an acceptance bar it already committed to.

    Rules (codex-reviewed, identity-based — NOT a naive path superset):
      * Every NON-file_exists predicate in `old` must appear verbatim in `new`
        (command/http_ok bars cannot be silently removed).
      * Every `old` file_exists `path` must be covered in `new` by EITHER
        a file_exists at the same `path` (unchanged), OR a file_exists whose
        `renamed_from` equals that path (an explicit, audited rename). An old
        path that simply vanishes is a WEAKENING → not monotonic.
      * Every `renamed_from` claimed in `new` MUST reference a real `old` path —
        a rename-from-nothing is a forged tie used to drop scope → rejected.
    Additions (new file_exists paths with no `old` counterpart) are always fine.
    """
    old_fe = {p.path for p in old.predicates if isinstance(p, FileExistsPredicate)}
    new_paths = {p.path for p in new.predicates if isinstance(p, FileExistsPredicate)}
    new_renames = {
        p.renamed_from
        for p in new.predicates
        if isinstance(p, FileExistsPredicate) and p.renamed_from
    }
    # A claimed rename must reference an actual prior deliverable.
    if any(rf not in old_fe for rf in new_renames):
        return False
    # Every old deliverable must still be required, or explicitly renamed away.
    for op in old_fe:
        if op not in new_paths and op not in new_renames:
            return False
    # Non-file_exists bars cannot be dropped.
    old_other = [p for p in old.predicates if not isinstance(p, FileExistsPredicate)]
    new_other = [p for p in new.predicates if not isinstance(p, FileExistsPredicate)]
    for p in old_other:
        if p not in new_other:
            return False
    return True
