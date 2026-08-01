"""The two decision clusters `worker_inspect._call_with_string_arg_in_position`
scans for at each candidate identifier position: is this a REAL call boundary
(not a `.member` access, not part of a longer identifier), and, if so, does the
call's first argument match the expected string (ignoring a `?`-suffixed query
string)? Splitting them out of the combined scan is what brings its own McCabe
under the cap.

`_call_first_arg_matches` calls back into the pure ``worker_inspect``
`_read_string_literal`. That import is performed INSIDE the function body (never
at this module's top level) so the reference is re-resolved on every call — if a
test ever monkeypatches that name on `worker_inspect`, this caller observes the
patch exactly as a caller still living in `worker_inspect.py` would.
`_is_call_boundary` is pure string arithmetic with no such back-reference.
"""

from __future__ import annotations


def _is_call_boundary(code: str, i: int, callee: str) -> bool:
    """True iff `code[i:]` starts with `callee` as a real (non-`.member`,
    non-longer-identifier) call-target boundary — i.e. `code[i]` is not preceded
    by an identifier character or `_$.` (so `.callee(` / `xcallee(` don't match)."""
    if not code.startswith(callee, i):
        return False
    if i == 0:
        return True
    prev = code[i - 1]
    return not (prev.isalnum() or prev in "_$.")


def _call_first_arg_matches(code: str, j: int, path: str) -> bool:
    """Starting right after a matched callee identifier at `code[j:]`, does the
    next non-whitespace token open a call `(` whose first argument is a string
    literal equal to `path` (ignoring a `?`-suffixed query string)?"""
    from disco.core.appkit.worker_inspect import _read_string_literal

    n = len(code)
    while j < n and code[j] in " \t\r\n":
        j += 1
    if j >= n or code[j] != "(":
        return False
    j += 1
    while j < n and code[j] in " \t\r\n":
        j += 1
    inner, _end = _read_string_literal(code, j)
    return inner is not None and inner.split("?", 1)[0] == path
