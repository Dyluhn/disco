"""First-class shape classification for `ThrashOracle` reds (amendment A11 §2).

**The defect this removes.** `ThrashOracle` emitted **four** structurally
different failures under the single code `TOOL_CALL_THRASH`:

| shape | what actually happened |
|---|---|
| `identical_tool_call_streak` | the model re-issued a byte-identical call |
| `repeated_semantic_shell_verification` | it re-ran the same *script*, spelled differently |
| `repeated_background_script_restart` | it restarted the same background script |
| `bounded_stuck_valve` | the product's own no-progress valve fired |

They were separable only by reading `first_broken_link` — a free-text field —
and the campaign's own design record fell into exactly that trap: A6.3 §1 states
*"`first_broken_link` names it exactly: `tool_call -> identical_tool_call_streak`"*
over a table of all three historical firings, but the frozen
`classification.json` for 10-C (`6606503c`, seqs 148/155/176) says
`repeated_semantic_shell_verification`. Two of three, not three of three. A
classification that a careful reader gets wrong is not a classification.

**The fix.** The shape is a first-class field on the result, the failure code
carries it, and both are derived HERE from one table — so a site cannot emit a
`first_broken_link` that disagrees with its shape, or a shape that disagrees
with its code.

**Severity is deliberately unchanged.** Every shape code maps to **P1**, exactly
what `TOOL_CALL_THRASH` mapped to. A11 §4 forbids weakening anything, and a
severity that moved — in either direction — would be a weakening dressed as a
refactor. `TOOL_CALL_THRASH` itself stays registered so the historical artifacts
that carry it remain `is_known_code`.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ..events import KIND_ACTION, KIND_OBSERVATION, action_id_of, kind_of, seq_of
from .schema import OracleResult, failing

_ORACLE = "ThrashOracle"

# The four shapes. These names are the ones the historical artifacts already use
# in `first_broken_link`, so evidence stays greppable across the change.
SHAPE_IDENTICAL_STREAK = "identical_tool_call_streak"
SHAPE_SEMANTIC_SHELL = "repeated_semantic_shell_verification"
SHAPE_BACKGROUND_RESTART = "repeated_background_script_restart"
SHAPE_STUCK_VALVE = "bounded_stuck_valve"

# shape -> the `first_broken_link` it must always be reported with.
LINK_BY_SHAPE: dict[str, str] = {
    SHAPE_IDENTICAL_STREAK: "tool_call -> identical_tool_call_streak",
    SHAPE_SEMANTIC_SHELL: "tool_call -> repeated_semantic_shell_verification",
    SHAPE_BACKGROUND_RESTART: "tool_call -> repeated_background_script_restart",
    SHAPE_STUCK_VALVE: "model_turns -> bounded_stuck_valve",
}

# shape -> the failure code it must always be reported with.
CODE_BY_SHAPE: dict[str, str] = {
    SHAPE_IDENTICAL_STREAK: fc.TOOL_CALL_THRASH_IDENTICAL_STREAK,
    SHAPE_SEMANTIC_SHELL: fc.TOOL_CALL_THRASH_SEMANTIC_SHELL,
    SHAPE_BACKGROUND_RESTART: fc.TOOL_CALL_THRASH_BACKGROUND_RESTART,
    SHAPE_STUCK_VALVE: fc.TOOL_CALL_THRASH_STUCK_VALVE,
}

SHAPES = frozenset(LINK_BY_SHAPE)

# The historical parent every shape refines. Retained so a reader of an old
# artifact can still map it forward, and so `severity_for` keeps resolving.
PARENT_CODE = fc.TOOL_CALL_THRASH

# How much of the ignored prior result to carry into the red. Enough to identify
# the answer the model already had; bounded so a red never inlines a whole build
# log into `classification.json`.
_RESULT_EXCERPT_CHARS = 400


def link_for(shape: str) -> str:
    """The `first_broken_link` for a shape. Unknown shapes are a programming
    error, not a runtime condition — fail loudly rather than emit a red that
    misclassifies itself."""
    try:
        return LINK_BY_SHAPE[shape]
    except KeyError:  # pragma: no cover - guarded by test_thrash_shapes
        raise ValueError(f"unknown thrash shape: {shape!r}") from None


def code_for(shape: str) -> str:
    """The failure code for a shape. See `link_for` on unknown shapes."""
    try:
        return CODE_BY_SHAPE[shape]
    except KeyError:  # pragma: no cover - guarded by test_thrash_shapes
        raise ValueError(f"unknown thrash shape: {shape!r}") from None


def _excerpt(text: str) -> str:
    if len(text) <= _RESULT_EXCERPT_CHARS:
        return text
    return text[: _RESULT_EXCERPT_CHARS - 1] + "…"


def _action_by_seq(events: list[dict[str, Any]], seq: int) -> dict[str, Any] | None:
    for event in events:
        if kind_of(event) == KIND_ACTION and seq_of(event) == seq:
            return event
    return None


def _observation_for_action(
    events: list[dict[str, Any]], action_id: str
) -> dict[str, Any] | None:
    if not action_id:
        return None
    for event in events:
        if kind_of(event) == KIND_OBSERVATION and str(event.get("action_id") or "") == action_id:
            return event
    return None


def ignored_prior_result(
    events: list[dict[str, Any]], occurrence_seqs: list[int]
) -> dict[str, Any] | None:
    """The result the repetition ignored (A11 §3).

    The model already had an answer before it repeated itself; that answer is
    the observation belonging to the FIRST occurrence in the group. Carrying it
    into the red is what lets a future reader classify the firing without
    transcript archaeology — which is the whole cost A11 §3 is removing.

    Returns ``None`` when the observation genuinely is not on the log (an action
    with no observation is a *different* failure, `ACTION_NO_OBSERVATION`, and
    must not be silently rendered as an ignored result).
    """
    if not occurrence_seqs:
        return None
    first = _action_by_seq(events, occurrence_seqs[0])
    if first is None:
        return None
    action_id = str(action_id_of(first) or "")
    observation = _observation_for_action(events, action_id)
    if observation is None:
        return None
    result = observation.get("tool_result") or {}
    success = result.get("success") is True
    error = str(result.get("error") or "")
    return {
        "seq": seq_of(observation),
        "action_id": action_id,
        "action_seq": occurrence_seqs[0],
        "success": success,
        "content_excerpt": _excerpt(str(result.get("content") or "")),
        "error": _excerpt(error) if error else None,
    }


def occurrences(events: list[dict[str, Any]], occurrence_seqs: list[int]) -> list[dict[str, Any]]:
    """Each occurrence's seq, with its action id and success where resolvable.

    The seq list alone was already carried; pairing it with the action id is
    what makes an occurrence addressable in the event log.
    """
    out: list[dict[str, Any]] = []
    for seq in occurrence_seqs:
        action = _action_by_seq(events, seq)
        action_id = str(action_id_of(action) or "") if action is not None else ""
        entry: dict[str, Any] = {"seq": seq, "action_id": action_id or None}
        observation = _observation_for_action(events, action_id)
        if observation is not None:
            result = observation.get("tool_result") or {}
            entry["success"] = result.get("success") is True
        out.append(entry)
    return out


def repetition_facts(
    events: list[dict[str, Any]],
    *,
    shape: str,
    fingerprint: str,
    occurrence_seqs: list[int],
) -> dict[str, Any]:
    """The A11 §3 evidence block: what repeated, where, and what it ignored."""
    return {
        "shape": shape,
        "fingerprint": fingerprint,
        "occurrences": occurrences(events, occurrence_seqs),
        "ignored_prior_result": ignored_prior_result(events, occurrence_seqs),
    }


def shaped_red(shape: str, *, facts: dict[str, Any]) -> OracleResult:
    """Emit a thrash red whose code, link and shape are derived from one table.

    Every `ThrashOracle` repetition/valve red is constructed here so a site
    cannot report a shape that disagrees with its code or its
    `first_broken_link` (A11 §2). Construction lives beside the tables rather
    than beside the checks for that reason: a builder one import away from its
    own lookup tables is a builder that can be bypassed.
    """
    return failing(
        _ORACLE,
        code_for(shape),
        first_broken_link=link_for(shape),
        facts=facts,
        shape=shape,
    )


def repetition_red(
    events: list[dict[str, Any]],
    *,
    shape: str,
    fingerprint: str,
    count: int,
    allowed: int,
    occurrence_seqs: list[int],
    extra: dict[str, Any] | None = None,
) -> OracleResult:
    """A repetition red, with the standard facts every repetition shape carries.

    `fingerprint`, `count`, `allowed` and `action_seqs` are the pre-A11 fact set
    and stay byte-compatible for readers of frozen artifacts; the A11 §3
    enrichment is merged on top.
    """
    facts: dict[str, Any] = {
        "fingerprint": fingerprint,
        "count": count,
        "allowed": allowed,
        "action_seqs": occurrence_seqs,
        **repetition_facts(
            events, shape=shape, fingerprint=fingerprint, occurrence_seqs=occurrence_seqs
        ),
    }
    if extra:
        facts.update(extra)
    return shaped_red(shape, facts=facts)
