"""The two lead-insert shape scanners `worker_inspect._region_has_run_insert`
decides between: the generated Drizzle `db.insert(leads).values(...).run()`
shape, and the legacy/raw `.prepare("INSERT INTO ...").bind(...).run()` shape.
Each is a cohesive, single-shape scan — splitting them out of the combined
function is what brings its own McCabe under the cap.

Both call back into a handful of pure ``worker_inspect`` matchers
(`_match_paren`, `_read_string_literal`, `_insert_in_dead_position`). Those
imports are performed INSIDE each function body (never at this module's top
level) so the reference is re-resolved on every call — if a test ever
monkeypatches one of those names on `worker_inspect`, callers here observe the
patch exactly as callers still living in `worker_inspect.py` would.
"""

from __future__ import annotations

import re


def _drizzle_run_insert(region: str) -> tuple[bool, bool] | None:
    """A Drizzle `db.insert(leads).values(...).run()` lead insert in a reachable
    (non-dead) position within `region`. `(True, True)` if found, else `None` (so
    the caller can fall back to the raw-SQL shape)."""
    from disco.core.appkit.worker_inspect import _insert_in_dead_position, _match_paren

    for m in re.finditer(r"\.insert\s*\(\s*leads\s*\)", region):
        values = re.match(r"\s*\.values\s*\(", region[m.end() :])
        if values is None:
            continue
        values_open = m.end() + values.end() - 1
        values_end = _match_paren(region, values_open)
        if values_end is None:
            continue
        if re.match(r"\s*\.run\s*\(", region[values_end:]) is None:
            continue
        if _insert_in_dead_position(region, m.start()):
            continue
        return True, True
    return None


def _raw_sql_run_insert(region: str) -> tuple[bool, bool] | None:
    """A legacy `.prepare("INSERT INTO ...").bind(...).run()` lead insert in a
    reachable (non-dead) position within `region`. `(True, False)` if found (present
    but not the ratified Drizzle shape), else `None`."""
    from disco.core.appkit.worker_inspect import _insert_in_dead_position, _match_paren
    from disco.core.appkit.worker_inspect import _read_string_literal as _read_lit
    from disco.core.appkit.worker_inspect_parts._ts_lexer import _skip_ws

    for m in re.finditer(r"\.prepare\s*\(", region):
        j = _skip_ws(region, m.end())
        lit, end = _read_lit(region, j)
        if lit is None or "INSERT INTO" not in lit.upper():
            continue
        k = _skip_ws(region, end)
        if k >= len(region) or region[k] != ")":
            continue  # `.prepare("..." + x)` etc. — literal not closed by `)`
        bind = re.match(r"\s*\.bind\s*\(", region[k + 1 :])
        if bind is None:
            continue  # no `.bind(...)` follows the prepared INSERT
        bind_open = k + 1 + bind.end() - 1  # index of the `(` opening `.bind(`
        bind_end = _match_paren(region, bind_open)
        if bind_end is None:
            continue
        if re.match(r"\s*\.run\s*\(", region[bind_end:]) is None:
            continue  # prepared+bound but never `.run()` — a dead reference
        if _insert_in_dead_position(region, m.start()):
            continue  # present but UNREACHABLE (dead branch / after early return)
        return True, False
    return None
