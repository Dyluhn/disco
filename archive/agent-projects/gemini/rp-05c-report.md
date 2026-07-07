# RP-05c report — §6 active-schema cap (tool_search) via ToolScope advertise/call split

**Date:** 2026-06-11
**Status:** COMPLETE — 16 new tests green, 0 regressions (544/544 pass)

## Deviations from brief

None. All five required design points and both required test modules are implemented as specified.

---

## What was shipped

### 1. `ToolScope.advertised_tools` — `registry.py`

Added `advertised_tools: frozenset[str] | None = None` to `ToolScope`. `None`
preserves today's behavior (all allowed tools shown). The field is frozen like
the rest of the model.

The docstring calls out the invariant: `readonly_tool_names` always keys off
`allowed_tools`, not the advertised subset — the planner-safety backstop is
explicitly *not* weakened.

### 2. `executor.available_tools()` — `executor.py`

```python
def available_tools(self) -> list[ToolSpec]:
    tools = self._registry.in_scope(self._scope)  # registry ∩ allowed_tools
    if self._scope.advertised_tools is not None:
        tools = [t for t in tools if t.definition.name in self._scope.advertised_tools]
    return [t.definition.to_spec() for t in tools]
```

- `in_scope()` and `registry.get()` are **unchanged** — callability still gates
  on `allowed_tools` only.
- `readonly_tool_names()` is **unchanged** — it uses `in_scope()`, which
  returns `registry ∩ allowed_tools` regardless of `advertised_tools`.
- The change is 3 lines in one method; no other code paths are touched.

### 3. `_MetaToolSearchWrapper` — `runtime.py`

New class parallel to `_MCPToolWrapper`. Holds the full MCP tool descriptor
list (`all_tool_descs`) and delegates to `_tool_search_handler` on `run()`.
Returns `ToolOutcome(structured={"results": [...]})` — `structured` typed as
`dict[str, Any]`, not list (ToolOutcome contract).

### 4. `_apply_mcp_scope()` — `runtime.py`

New module-level function that encapsulates the MCP registration + cap logic:

```
_apply_mcp_scope(executor, all_mcp_tools, call_target, *, max_active_schemas=20)
```

Over the cap (`len(all_mcp_tools) > max_active_schemas`):
- Registers all MCP wrappers (callable — in `allowed_tools`)
- Registers `_MetaToolSearchWrapper` for `tool_search`
- Adds `"tool_search"` to `allowed_tools` (callable) and sets
  `advertised_tools = non_mcp_allowed | {"tool_search"}` (LLM sees only these)

Under/at the cap:
- Registers all MCP wrappers (callable)
- `advertised_tools` stays `None` — all allowed tools shown (unchanged behavior)

`_apply_mcp_scope` is module-level so tests can import it directly — no test
seam needed, and no tautology (tests go through the production registration
function, not hand-built wrappers).

### 5. `_compose_build_loop()` updated — `runtime.py`

Replaced the inline 10-line MCP registration block with:

```python
if all_mcp_tools:
    cfg = self._config_store.load()
    max_schemas = cfg.mcp.max_active_schemas if cfg.mcp else 20
    _apply_mcp_scope(
        executor, all_mcp_tools, self._mcp_call_target,
        max_active_schemas=max_schemas,
    )
```

The comment block explaining why cap wiring was deferred is removed — it is
now implemented.

---

## Tests

### `packages/tools/tests/test_scope_advertise_split.py` — 8 tests

| Test | What it pins |
|------|--------------|
| `test_advertised_subset_restricts_visible_tools` | `advertised={a}` → `available_tools()` returns only `a` |
| `test_advertised_none_shows_all_allowed` | `None` → backward-compat, all allowed shown |
| `test_advertised_must_be_subset_of_allowed_in_practice` | advertised ∩ (registry ∩ allowed) is the correct intersection |
| `test_allowed_not_advertised_tool_is_callable` | `b` allowed, not advertised → `execute("b")` succeeds |
| `test_not_allowed_tool_yields_unknown_tool` | `d` not in allowed → `unknown_tool` even if advertised |
| `test_allowed_and_advertised_tool_is_callable` | normal path still works |
| `test_readonly_tool_names_derived_from_allowed_not_advertised` | `b`,`c` read-only + allowed but not advertised → in `readonly_tool_names` |
| `test_readonly_tool_names_not_restricted_to_advertised` | planner backstop not narrowed by advertise split |

### `packages/agent-server/tests/test_mcp_tool_search_cap.py` — 8 tests

All assert via `_apply_mcp_scope` (the production registration path); no
wrapper constructed directly.

| Test | What it pins |
|------|--------------|
| `test_over_cap_advertises_tool_search_not_mcp_schemas` | 21 tools > cap → `tool_search` visible, no `mcp__*` in advertised |
| `test_over_cap_non_mcp_tools_still_advertised` | built-in agent-scope tools remain visible |
| `test_over_cap_mcp_tool_callable_by_qualified_name` | `mcp__srv__tool_0` not advertised but callable → `execute()` succeeds |
| `test_over_cap_tool_search_callable` | `tool_search` in both `allowed_tools` and `advertised_tools` → callable |
| `test_under_cap_all_mcp_tools_advertised` | 20 tools == cap → all 20 advertised, no `tool_search` |
| `test_under_cap_no_tool_search` | 5 tools << cap → no `tool_search` |
| `test_zero_mcp_tools_noop` | empty list → no-op, scope unchanged |
| `test_over_cap_readonly_tool_names_from_allowed_not_advertised` | planner backstop not weakened after cap wiring |

---

## Files changed

```
packages/tools/src/disco/tools/registry.py        +11 lines
packages/tools/src/disco/tools/executor.py         +4 lines
packages/agent-server/src/disco/agent_server/runtime.py  +66/-29 lines
packages/tools/tests/test_scope_advertise_split.py        NEW (92 lines)
packages/agent-server/tests/test_mcp_tool_search_cap.py   NEW (101 lines)
```

## Security invariants confirmed

- `registry.get(name, scope)` unchanged — checks `allowed_tools` only.
- `readonly_tool_names()` unchanged — uses `in_scope()` = `registry ∩ allowed_tools`.
- MCP tools over the cap are callable by qualified name; the LLM can discover
  them via `tool_search` and call them — no round-trip schema mutation required.
- `tool_search` is in both `allowed_tools` (callable) and `advertised_tools`
  (visible) when the cap is active.
