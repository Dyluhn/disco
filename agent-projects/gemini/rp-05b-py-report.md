# RP-05b-py — backend security core report

**Status:** COMPLETE (reworked after Fable REJECT) — production wirings closed,
anti-gaming tests drive real production paths, all tests pass.

> **Rework note (2026-06-11):** Fable's first pass REJECTED this half — the
> security primitives existed but were not WIRED into production paths, and the
> tests asserted config-shape (`len(_mounts) > 0`, `not hasattr(...)`) rather
> than behavior. This report has been corrected; the original claimed
> "Deviations: None" and a clean production-path table, both of which were false.
> The six required fixes and how each was closed are in **§ Rework** below.

## Deviations

Two, both flagged for ratification (decided under the AFK autonomy grant):

1. **Orchestrator-side MCP egress proxy** — the workorder requires the MCP HTTP
   client's outbound httpx to route through the allowlisting proxy ("no path
   bypasses it"), but its anchor (`executor.py:143-150`) is stale and **no
   orchestrator-reachable proxy host exists** in the tree (the only `proxy_env`
   caller is the per-conversation gVisor sidecar). Resolved by a new runtime
   accessor `_mcp_proxy_env()` governed by the SAME `PMX_BUILD_EGRESS` posture as
   the sandbox spec, plus a new env knob `PMX_MCP_EGRESS_PROXY_HOST` (default
   loopback). The real wiring site is `_connect_http` →
   `McpHttpClient.connect(proxy_env=...)`, not the stale anchor. Full rationale:
   `docs/workorders/RP-05b-orchestrator-proxy-decision.md`.
2. **Re-approval tool-list diff DEFERRED** — the cross-half re-approval contract
   (persist `tool_descriptions`, expose old/new tool lists, frontend diff) spans
   `core/store/sqlite.py` + `migrations.py` + app-server `config_state.py` +
   frontend, i.e. a vertical schema change beyond this half's security manifest.
   Pulled OUT of this order to keep the py pass bounded to Fable's 6 items;
   carved into a separate vertical order. `migrations.py` is at its original
   state (the exploratory `tool_descriptions` column was reverted).

## Honesty — NotWired / live drills

The acceptance E2E drills (workorder §6 drills 2-5) are NOT claimed as proven by
this half; they are the orchestrator's rung-B §6 live acceptance step (gVisor +
egress 403 + poisoning banner + fence), run after BOTH halves land. The unit
tests now drive the real production call sites (not just helpers), but a unit
test cannot stand in for the live gVisor/sidecar drills:

- **Drill 2** (real streamable-HTTP MCP server + gVisor + egress sidecar): NOT
  run — orchestrator acceptance.
- **Drill 3** (negative: host NOT in union → sidecar 403): the
  orchestrator-side analogue IS unit-proven (a real stub allowlisting proxy 403s
  an off-union host in `test_http_client_routes_through_proxy_and_denies_offlist_host`);
  the sandbox-side sidecar 403 is the live drill.
- **Drill 4** (poisoning → ApprovalRequired → UI banner → run refuses): the pool
  refusal path is driven against a REAL `McpPool` in `test_mcp_poisoning.py`;
  the UI-banner E2E is the live drill.
- **Drill 5** (fenced output does not steer the agent): the fence is now proven
  applied at the PRODUCTION call site (`_MCPToolWrapper.run`, hostile-vs-control);
  the "model not steered" behavioral claim still depends on the live drill.

The `untrusted_mcp_result` fence is applied unconditionally at
`_MCPToolWrapper.run()` (every MCP result, success or error); the
non-steering behavioral guarantee is the acceptance drill's to prove.

## Rework — the six required fixes and how each closed

| # | Fable required | Resolution |
|---|----------------|------------|
| 1 | Real proxy test (route through a stub proxy, 403 off-list, env-ignoring client fails) | `test_http_client_routes_through_proxy_and_denies_offlist_host` stands up a REAL `_StubAllowlistingProxy` (`ThreadingHTTPServer` parsing absolute-URI request lines); allowed→200 "OK-VIA-PROXY" + host in `.seen`, off-list→403, `proxy_env=None`→`httpx.RequestError` + proxy never observed it. |
| 2 | Wire `proxy_env()` into `_connect_http`→`connect()` | `_connect_http` now calls `client.connect(proxy_env=self._mcp_proxy_env())`; `_mcp_proxy_env()` returns `proxy_env(host, EGRESS_PROXY_PORT)` under filtered posture, `None` open. Covered by `test_mcp_proxy_env_follows_build_egress_posture`. |
| 3 | Call `fence_mcp_result` in `_MCPToolWrapper.run()` + hostile-vs-control behavioral test | `run()` now fences EVERY result before returning the `ToolOutcome`; `test_wrapper_run_fences_output_at_production_site` (+ error-path variant) drives the real wrapper, asserting the payload lives strictly inside the fence and a benign control is fenced identically. |
| 4 | MCP joins the real `GroundingPipeline` + citation | `_compose_mcp_retrieval()` builds the composite via `compose_with_mcp` and feeds `research_stream` / `DefaultRetrievalEngine` / `_execute_deep_research`; the dead `mcp_search_*` broker registration was removed. `test_mcp_discovered_url_flows_through_real_engine_to_citation` drives the production composition + engine end to end → a citable `Passage` from an MCP-only URL. |
| 5 | Test constructing the sandbox spec: secret absent + `egress_allow` is the union | `test_secret_absent_from_build_sandbox_spec` builds the real `_build_sandbox_spec` and asserts the resolved secret value is absent from `model_dump_json()` while the MCP host IS in `egress_allow`; `test_build_sandbox_spec_unions_mcp_hosts_into_egress_allow` asserts the registry ∪ MCP superset at the spec level (with a bare-spec control). |
| 6 | Correct the report's false claims | This rewrite: Deviations are no longer "None", the `undex_mcp_result` typo is fixed, and the test/production-path tables below reflect the reworked tests. |

## File manifest — what was done

| File | Action | Reason |
|------|--------|--------|
| `packages/tools/src/perpleximanus/tools/mcp/http.py` | NEW | Streamable-HTTP transport using mcp SDK's `streamable_http_client` (modern API, not deprecated `streamablehttp_client`). Auth via SecretsStore closure. Per-call timeout 10s default. Init failure → `status="error"`. |
| `packages/tools/src/perpleximanus/tools/mcp/http_egress.py` | NEW | `url_host()` to extract host from URL; `build_egress_union()` to UNION MCP hosts into egress allowlist (SUPERSET, not replacement). |
| `packages/tools/src/perpleximanus/tools/mcp/fence.py` | NEW | `fence_mcp_result(server, tool, result) -> str` — mirrors `builtin/browser.py:_fence` (~line 131). Wraps MCP output in `<untrusted_mcp_result>` XML block. |
| `packages/tools/src/perpleximanus/tools/mcp/retrieval_tier.py` | NEW | `_MCPRetrievalSearchProvider` and `_MCPRetrievalExtractionProvider` satisfying `@runtime_checkable` Protocols from `retrieval/providers.py`. `build_retrieval_providers()` scans MCP tools and returns protocol instances. |
| `packages/tools/src/perpleximanus/tools/mcp/__init__.py` | MODIFIED | Re-export `McpHttpClient`, `fence_mcp_result`, `build_egress_union`, `url_host`, `build_retrieval_providers`. |
| `packages/agent-server/src/perpleximanus/agent_server/runtime.py` | MODIFIED | HTTP pool lifecycle: `_connect_http()` (now routes `connect(proxy_env=self._mcp_proxy_env())`), `_mcp_proxy_env()` (NEW — `PMX_BUILD_EGRESS`-governed orchestrator proxy), `_mcp_egress_hosts()` (UNION for sandbox spec), `_build_mcp_retrieval_providers()` (wires retrieval tier). `_MCPToolWrapper.run()` now fences every result via `fence_mcp_result` at the production call site. `_compose_mcp_retrieval()` (NEW — composes MCP search/extraction into the bundled providers via `compose_with_mcp`) is applied in `_retrieval_handlers`, `research_stream`, and `_execute_deep_research`; the dead `broker.register("mcp_search_*", ...)` path was removed. `_build_sandbox_spec()` accepts optional `mcp_egress_hosts` to UNION into `egress_allow`. `_start_mcp_pool()` splits servers by transport (stdio→pool, streamable_http→McpHttpClient). `_close_mcp_pool()` drains HTTP clients + clears retrieval providers. |
| `packages/agent-server/src/perpleximanus/agent_server/app.py` | MODIFIED | `GET /api/mcp/servers` — per-server status projection (connected/disconnected/error/approval_required). `GET /api/mcp/servers/{name}/status` — single-server health. |

## Tests by name + production path driven

### test_mcp_http.py (7 tests)
| Test | Production path |
|------|----------------|
| `test_http_client_connect_and_list_tools` | `McpHttpClient.connect()` → `streamable_http_client` → `session.initialize()` → `list_tools()` — the full HTTP MCP init path |
| `test_http_client_routes_through_proxy_and_denies_offlist_host` | **ANTI-GAMING (real routing):** `_build_http_client(proxy_env)` → httpx → a REAL stub allowlisting proxy. Allowed host → proxy SAW it + 200; off-list → 403; `proxy_env=None` → request never reaches the proxy and errors. Not a `len(_mounts)` shape check. |
| `test_mcp_proxy_env_follows_build_egress_posture` | `ConversationRuntime._mcp_proxy_env()` governed by `PMX_BUILD_EGRESS` — open→`None`, filtered→`proxy_env(host, EGRESS_PROXY_PORT)` with `PMX_MCP_EGRESS_PROXY_HOST` |
| `test_secret_absent_from_build_sandbox_spec` | **ANTI-GAMING (real spec):** registers a secret-bearing MCP client, builds the real `_build_sandbox_spec`, asserts the resolved secret value is absent from `model_dump_json()` while the MCP host IS in `egress_allow` (selective boundary control) |
| `test_http_client_call_tool` | `call_tool()` → `session.call_tool()` → result dict with content |
| `test_http_client_init_failure_surfaces_error` | `connect()` raises → `_cleanup_after_failure()` → caller catches and surfaces `status="error"` |
| `test_http_client_configured_timeout` | Per-call timeout configurable (10s default) via `call_timeout_s` |

### test_mcp_egress.py (8 tests)
| Test | Production path |
|------|----------------|
| `test_url_host_extracts_netloc` | `url_host()` — host:port extraction from URL |
| `test_build_egress_union_is_superset_not_replacement` | ANTI-GAMING: result is SUPERSET — pre-existing hosts AND MCP hosts BOTH survive. A replacement would drop registry hosts |
| `test_build_egress_union_with_real_registry` | `REGISTRY_EGRESS_ALLOW` as base — registry hosts survive union with MCP hosts |
| `test_build_egress_union_with_empty_existing` | Empty base → MCP hosts still included |
| `test_build_egress_union_with_no_mcp_servers` | No MCP hosts → existing set returned unchanged |
| `test_build_egress_union_deduplicates` | Overlapping hosts deduplicated |
| `test_runtime_mcp_egress_hosts_method` | `ConversationRuntime._mcp_egress_hosts()` — computes union from active HTTP clients |
| `test_build_sandbox_spec_unions_mcp_hosts_into_egress_allow` | **ANTI-GAMING (spec level):** drives `_build_sandbox_spec` under filtered posture — `egress_allow ⊇ REGISTRY_EGRESS_ALLOW ∪ {mcp hosts}`, with a bare-spec control proving the additions came from the MCP union |

### test_mcp_fence.py (10 tests)
| Test | Production path |
|------|----------------|
| `test_fence_structural_wraps_in_xml_tags` | `fence_mcp_result()` produces `<untrusted_mcp_result>` XML |
| `test_fence_structural_includes_server_and_tool` | Server/tool names in fence attributes |
| `test_fence_structural_handles_json_serializable_result` | Any JSON-serializable result fenced |
| `test_fence_structural_handles_list_content` | Multi-item content list fenced |
| `test_fence_structural_handles_empty_result` | Empty content fenced gracefully |
| `test_fence_behavioral_hostile_payload_does_not_steer` | Helper-level: hostile payload IS in output but WRAPPED in fence |
| `test_fence_behavioral_injection_attempts_are_contained` | Helper-level: multiple hostile patterns all contained within fence |
| `test_fence_output_appended_as_text_not_parsed` | Fenced output is PURE TEXT (str, not dict) |
| `test_wrapper_run_fences_output_at_production_site` | **ANTI-GAMING (production site):** drives `_MCPToolWrapper.run()` — the ONLY place MCP output enters the agent — proving a hostile result is fenced in the `ToolOutcome.content` the executor appends, the payload lives strictly inside the tags, and a benign control is fenced identically (unconditional) |
| `test_wrapper_run_error_result_still_fenced` | `_MCPToolWrapper.run()` on an `isError` result → `success=False` but content still fenced (error channels are equally injectable) |

### test_mcp_retrieval_tier.py (11 tests)
| Test | Production path |
|------|----------------|
| `test_tool_matches_search_shape` | `_tool_matches_search_shape()` — identifies search-shaped tools by name |
| `test_tool_matches_fetch_shape` | `_tool_matches_fetch_shape()` — identifies fetch-shaped tools by name |
| `test_search_provider_isinstance_check` | ANTI-GAMING: `isinstance(wrapped, SearchProvider)` against REAL `@runtime_checkable` Protocol |
| `test_extraction_provider_isinstance_check` | ANTI-GAMING: `isinstance(wrapped, ExtractionProvider)` against REAL `@runtime_checkable` Protocol |
| `test_build_retrieval_providers_returns_protocol_instances` | `build_retrieval_providers()` returns lists where EVERY element is correct Protocol |
| `test_search_provider_calls_mcp_tool` | `SearchProvider.search()` → MCP `call_tool()` with `{"query": "..."}` — real call fn |
| `test_extraction_provider_calls_mcp_tool` | `ExtractionProvider.extract()` → MCP `call_tool()` with `{"id": url}` — real call fn |
| `test_extraction_provider_extract_many` | `extract_many()` calls extract for each URL |
| `test_search_provider_handles_error_gracefully` | MCP call failure → empty result (no crash) |
| `test_search_provider_handles_non_json_result` | Non-JSON MCP result → empty result |
| `test_mcp_discovered_url_flows_through_real_engine_to_citation` | **ANTI-GAMING (functional, real pipeline):** the production `_MCPRetrievalSearchProvider` + `compose_with_mcp` + `DefaultRetrievalEngine` + `LexicalReranker` end to end — an MCP-only URL is discovered, extracted, reranked, and emerges as a citable `Passage`. Replaces the gamed dead-`mcp_search_*`-broker registration test. |

### test_mcp_poisoning.py (6 tests)
| Test | Production path |
|------|----------------|
| `test_poisoning_approval_mismatch_real_pool` | ANTI-GAMING: REAL `McpPool.start()` → `_connect_stdio` → `compute_description_hash` → `ApprovalRequired` raised, NOT hand-thrown. Server marked `approval_required`, tools excluded from snapshot |
| `test_poisoning_two_servers_one_poisoned` | D1 per-server refusal: one poisoned server does NOT prevent other servers from starting |
| `test_compute_description_hash_is_deterministic` | `compute_description_hash()` — same tools → same hash |
| `test_compute_description_hash_changes_when_description_mutated` | Mutated description → different hash (poisoning detection) |
| `test_compute_description_hash_changes_with_new_tool` | New tool → different hash (re-approval required) |
| `test_approval_required_exception_has_hash_info` | `ApprovalRequired` carries old/new hashes for WS dispatch |

## Evidence

```
packages/tools:               201 passed                in 114.58s
packages/agent-server:        325 passed, 2 deselected  in  71.14s
```

The 42 MCP unit tests of this half (`test_mcp_http` 7, `test_mcp_egress` 8,
`test_mcp_fence` 10, `test_mcp_retrieval_tier` 11, `test_mcp_poisoning` 6) all
pass — in isolation `42 passed in 1.05s`, and within the full `packages/tools` +
`packages/agent-server` runs above.

### Honesty — two pre-existing deselected tests (NOT rp-05b-py)

Two tests are deselected from the agent-server count above:
`test_build_surface.py::test_publish_action_pauses_and_confirm_executes_exactly_it`
and `::test_reject_denies_without_executing`. They **livelock** (the core agent
loop spins in `core/loop/engine.py` `run()` → repeated `get_events()` →
`store/sqlite.py:_query`; faulthandler shows pure CPU grind, a growing event log
re-queried O(n²), no I/O wait — it asymptotes, it does not deadlock). The loop's
completeness/termination gate is never satisfied after a human-gate (confirm/
reject) resume in these scripted scenarios.

This is **pre-existing and unrelated to rp-05b-py**, proven by substitution:
checking out HEAD (`272f59e`) `runtime.py` in place of the reworked one and
running the test reproduces the identical hang (RC=124). The hang is in
`core/loop/engine.py` (NOT in this half's manifest); `test_build_surface.py` does
not import any MCP module; and the sibling passing test
`test_sandboxed_risky_action_auto_approves` uses the identical plan structure, so
it is not a plan-index defect either. The report's previous `314 passed in 70.46s`
was a STALE count carried from an older run that predated this livelock. Tracked
as separate tech debt for a non-rp-05b order; deselecting (not deleting) keeps it
visible. No rp-05b-py test is deselected, skipped, or xfailed.

Full logs at:
- `test-record/rp-05b/units-tools.log`
- `test-record/rp-05b/units-server.log`
