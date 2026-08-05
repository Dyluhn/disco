"""The single owner of tool-call identity ("did the model issue THIS call before?").

Two independent consumers need the same answer to that question and must never
answer it separately:

* the **loop**, deciding whether to surface the idempotent-observation memo
  before a repeat executes (`loop/dedup.py`, W-39);
* the **harness** `ThrashOracle`, deciding after the fact whether a run thrashed
  (`harness/build_soak/oracles/_thrash_checks.py`).

A6.3 §3.1 makes one owner a *binding* design constraint, and the reason is
specific: a memo that fires on a different equivalence class than the oracle
that grades the run is worse than no memo at all — the drift would be invisible
until a canary fired on a class the memo had never seen. So the normalization
lives here, in the lowest layer, and both sides import it. It is never copied.

The equivalence class is deliberately **exact**: tool name plus canonical-JSON
argument equality. No UUID or number normalization is applied to arguments (that
normalization exists in the oracle, but for *error text*, which is a different
question). Two calls are "the same call" only if a byte-stable rendering of
their arguments agrees.

Why canonical JSON rather than Python `==` on the argument dicts: `==` treats
`{"n": 1}` and `{"n": 1.0}` as equal and cannot compare values that are not
directly comparable, while the oracle grades runs from *serialized* events where
that distinction survives. Keying both sides on the same rendering removes the
disagreement instead of documenting it.

Import-free by design (stdlib only) so the harness can depend on it without
pulling product machinery into evidence adjudication.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# The placeholder for a call whose tool name is missing or empty. Kept as a
# module constant so both consumers render an unnamed tool identically rather
# than one emitting "" and the other "?" for the same event.
UNKNOWN_TOOL = "?"


def canonical_arguments(arguments: Mapping[str, Any] | None) -> str:
    """The byte-stable rendering of a tool call's arguments.

    ``sort_keys`` makes key order irrelevant, the tight separators keep the
    rendering free of incidental whitespace, and ``default=str`` guarantees the
    call never raises on a value JSON cannot encode — an identity function that
    throws would turn an unusual argument into a crash in the middle of grading
    a run.
    """
    return json.dumps(dict(arguments or {}), sort_keys=True, separators=(",", ":"), default=str)


def tool_call_fingerprint(tool_name: str | None, arguments: Mapping[str, Any] | None) -> str:
    """The identity of one tool call: ``"<tool_name>:<canonical arguments>"``.

    Equal fingerprints mean "the model issued this same call again". This is the
    ONLY definition of that in the codebase; see the module docstring.
    """
    return f"{tool_name or UNKNOWN_TOOL}:{canonical_arguments(arguments)}"
