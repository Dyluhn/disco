"""The conversation runtime — runs the agent loop with real inference (Stage 2).

Phase 0 only appended events. This wires `AgentLoop` (core) + a real Qwen-backed
router into the request path: a user message KICKS the loop, which runs in the
background, calls the model via the OpenAI adapter, and appends events — which the
store already publishes to the WebSocket the UI consumes (history-then-live).

The Research surface defaults: `NeverConfirm` (no human gate) + no tools (the
model answers directly). The grounded research pipeline (Stage 4) is exposed
separately via `research_stream()` — it is NOT the agent loop; it is the
rewrite→search→extract→rerank→generate→verify pipeline streamed as the UI's
grounded-answer frames. The Build surface's `BlastRadiusConfirm` stays dormant.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from disco.core import (
    DEFAULT_OWNER_ID,
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    ConversationStatus,
    Event,
    EventFilter,
    EventSource,
    KnowledgeEvent,
    LLMMessage,
    LLMSummarizingCondenser,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    ReportEvent,
    SkillStore,
    StatusEvent,
    ToolCall,
    ToolResult,
    render_skills_for_prompt,
)
from disco.core.env import disco_env
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    DriverPrompts,
    ModelRole,
    OperatingMode,
    RouterSummarizer,
    SandboxSettings,
    SecretStore,
)
from disco.core.llm.config import RouterConfig
from disco.core.llm.secrets import OPENROUTER_API_KEY_ENV
from disco.core.llm.wiring import build_providers
from disco.core.loop import (
    AgentLoop,
    BlastRadiusConfirm,
    BuildAgent,
    NeverConfirm,
    ResearchAgent,
    RouterAgent,
)
from disco.core.loop.engine import _BOOKKEEPING_TOOLS
from disco.core.security import RuleBasedAnalyzer
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval.deep_research import (
    DeepResearchRun,
    DepthTier,
    decompose_query,
)
from disco.retrieval.wiring import retrieval_capability_handlers
from disco.tools import (
    REGISTRY_EGRESS_ALLOW,
    Capability,
    CapabilityBroker,
    DefaultToolExecutor,
    ProcessSandboxService,
    SandboxService,
    SandboxSession,
    SandboxSpec,
    ToolDef,
    agent_scope,
    build_default_registry,
)

# MCP client pool (RP-05 rung A) — built once at start, snapshotted per conversation.
from disco.tools.mcp import McpPool, McpServerConfig
from disco.tools.projects import (
    ProjectStore,
    StorageStatus,
    rehydrate_workspace,
    snapshot_workspace,
)
from disco.tools.sandbox import (
    GvisorSandboxService,
    LocalSandboxService,
    PodmanSandboxService,
    SandboxConfig,
)
from disco.tools.sandbox._container import PREVIEW_PORT, USER_PORTS
from disco.tools.sandbox.port_owner import port_owners
from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView


class _MCPToolWrapper:
    """Thin Tool-protocol wrapper that adapts an MCP ToolDef for the registry.
    MCP tools run through the pool's per-server client (routed by qualified name),
    not through the default executor's sandbox path. The definition is the ToolDef
    built at pool start; run() invokes the real tool via the pool."""

    def __init__(self, tdef: Any, pool: McpPool) -> None:
        self.definition = tdef
        self._pool = pool

    async def run(self, args: Any, ctx: Any) -> Any:
        from disco.tools.anatomy import ToolOutcome
        from disco.tools.mcp import fence_mcp_result
        from disco.tools.mcp.naming import split_qualified_name

        qn = self.definition.name
        parts = split_qualified_name(qn)
        if parts is None:
            return ToolOutcome(
                success=False,
                content=f"MCP tool {qn!r}: not a valid qualified name",
                error="invalid qualified name",
            )
        server, tool = parts
        try:
            # Convert validated pydantic model back to plain dict
            if hasattr(args, "model_dump"):
                raw_args = args.model_dump()
            else:
                raw_args = dict(args)
            result = await self._pool.call_tool(server, tool, raw_args)
            # RP-05b §4: MCP output is UNTRUSTED — every MCP channel is injectable
            # (CyberArk/MCPTox). Fence the raw result so the model sees it as data,
            # not instruction; the fenced block is appended as text and is NEVER fed
            # back into the tool-call parser. THIS is the production call site for
            # fence_mcp_result — the fence is dead unless it wraps output here.
            content = fence_mcp_result(server, tool, result)
            return ToolOutcome(
                success=not result.get("isError", False),
                content=content,
            )
        except Exception as exc:
            return ToolOutcome(
                success=False,
                content=f"MCP tool {qn!r} call failed: {exc}",
                error=str(exc),
            )

class _MetaToolSearchWrapper:
    """Wraps the tool_search meta-tool with the full MCP tool list for searching.

    The LLM calls this when the pool is over the schema cap. It runs an
    orchestrator-side keyword search over ALL MCP tools (including those hidden
    from the advertised set) and returns qualified names + descriptions. The
    active schema set is never mutated — discovery only.
    """

    def __init__(self, tdef: Any, all_tool_descs: list[dict]) -> None:
        self.definition = tdef
        self._all_tool_descs = all_tool_descs

    async def run(self, args: Any, ctx: Any) -> Any:
        import json

        from disco.tools.anatomy import ToolOutcome
        from disco.tools.mcp.tool_search import _tool_search_handler

        results = await _tool_search_handler(
            query=args.query,
            limit=args.limit,
            all_tools=self._all_tool_descs,
        )
        content = json.dumps(results, indent=2) if results else "No matching tools found."
        return ToolOutcome(success=True, content=content, structured={"results": results})


def _apply_mcp_scope(
    executor: DefaultToolExecutor,
    all_mcp_tools: list,
    call_target: Any,
    *,
    max_active_schemas: int = 20,
) -> None:
    """Register MCP tools in the executor and apply the §6 advertised/callable split.

    All MCP tools are registered as callable (added to allowed_tools). Over the
    cap, only non-MCP tools + tool_search are advertised to the LLM; under the
    cap, advertised_tools stays None so all allowed tools are shown (unchanged
    behavior). The planner-safety readonly_tool_names backstop always keys off
    allowed_tools, not the advertised subset.
    """
    if not all_mcp_tools:
        return

    mcp_names = frozenset(t.name for t in all_mcp_tools)
    non_mcp_allowed = executor._scope.allowed_tools

    # Extend the security allowlist: ALL MCP tools are callable by qualified name.
    executor._scope = executor._scope.model_copy(
        update={"allowed_tools": non_mcp_allowed | mcp_names}
    )
    for tdef in all_mcp_tools:
        executor._registry.register(_MCPToolWrapper(tdef, call_target))

    # §6 cap: over the limit, restrict the ADVERTISED set to non-MCP + tool_search.
    if len(all_mcp_tools) > max_active_schemas:
        from disco.tools.mcp.tool_search import meta_tool_search

        ts_def = meta_tool_search()
        all_tool_descs = [
            {"name": t.name, "description": t.description}
            for t in all_mcp_tools
        ]
        executor._registry.register(_MetaToolSearchWrapper(ts_def, all_tool_descs))
        # tool_search must be in allowed_tools (callable) and advertised_tools (visible).
        executor._scope = executor._scope.model_copy(
            update={
                "allowed_tools": executor._scope.allowed_tools | {"tool_search"},
                "advertised_tools": non_mcp_allowed | {"tool_search"},
            }
        )
    # else: advertised_tools remains None → all allowed tools shown (current behavior)


_LOG = logging.getLogger(__name__)


def _has_unfinished_plan(events: list) -> bool:
    """True when an approved plan exists but FINISHED was never recorded — the
    conversation was interrupted mid-execution (server crash, manual cancel)."""
    has_plan = any(isinstance(e, PlanEvent) for e in events)
    if not has_plan:
        return False
    has_finished = any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
        for e in events
    )
    return not has_finished


def build_sandbox_service(settings: SandboxSettings) -> SandboxService:
    """Map the persisted SandboxSettings → the concrete SandboxBackend — the ONE place
    that knows the backend↔config mapping (settings drive the active backend). Podman is
    real, verified backend code, but a STUB in THIS environment (VM 202 destroyed) — it
    constructs but isn't live/verifiable here; completed at the Meta deployment."""
    cfg = SandboxConfig(
        backend=settings.backend,
        docker_socket=settings.docker_socket,
        podman_url=settings.podman_url,
        runtime=settings.runtime,
        image=settings.image,
        workspace_root=settings.workspace_root,
        # the host previews are reachable at — set PMX_PREVIEW_HOST to a LAN/tailnet IP so
        # previews work from other devices, not just the agent-server's host (else derived).
        preview_host=disco_env("PREVIEW_HOST", ""),
    )
    if settings.backend == "gvisor":
        return GvisorSandboxService(cfg)
    if settings.backend == "local":
        return LocalSandboxService(cfg)
    if settings.backend == "podman":
        return PodmanSandboxService(cfg)  # stub-in-this-env (see docstring)
    return ProcessSandboxService()  # "process"/unknown → the dev backend (runs on host)


def _model_label(model_id: str) -> str:
    """A short human label from a model_id (drops the gguf/quant noise + provider path)."""
    base = model_id.split("/")[-1].removesuffix(".gguf")
    for suffix in ("-UD-Q5_K_XL", "-UD-Q4_K_XL", "-Q5_K_M", "-Q4_K_M", "-Q4_K_S", "-IQ4_XS"):
        base = base.replace(suffix, "")
    return base


# Live model-server probe: derive the ACTUALLY-SERVED model name + context window
# from the backend, rather than trusting the static ModelEntry — which drifts (a
# config still saying "Qwen3.6-27B @131072" while llama.cpp serves gemma-4-E4B at
# whatever -c it was launched with). Same ground-truth-over-declaration principle
# as the build loop's live workspace snapshot: read the truth, don't assume it.
# Best-effort + cached per base_url; ANY failure falls back to the static config.
# Each entry is (result, monotonic_ts) so a model hot-swap (the driver is
# restarted with a different served model / n_ctx) is re-probed once the
# cached entry ages past _PROBE_TTL_S. Bare-dict entries are tolerated as a
# legacy / test-only direct-set form and are always served fresh.
_LIVE_MODEL_PROBE_CACHE: dict[str, tuple[dict[str, Any], float] | dict[str, Any]] = {}
# 60s is long enough that an idle agent-server isn't re-probing on every
# /models or /health hit, and short enough that a llama.cpp restart with a
# different served model is picked up within a minute.
_PROBE_TTL_S: float = 60.0
# base_urls with a probe thread currently in flight — so N concurrent on-loop
# callers (a /models + /health + first kick arriving together) schedule at most ONE
# worker thread per url instead of one each.
_LIVE_MODEL_PROBE_INFLIGHT: set[str] = set()


def _do_live_model_probe(base_url: str, api_key: str | None) -> dict[str, Any]:
    """The BLOCKING probe body. Must run OFF the event loop (worker thread / sync
    context) — `httpx.get` here waits up to 2s. Populates the module cache on any
    partial success. Never raises."""
    out: dict[str, Any] = {"model_id": None, "n_ctx": None}
    try:
        import httpx

        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(f"{root}/props", headers=headers, timeout=2.0)
        if r.status_code == 200:
            d = r.json()
            gen = d.get("default_generation_settings") or {}
            n = gen.get("n_ctx")
            # bool is a subclass of int — exclude it so a stray {"n_ctx": true}
            # can't masquerade as a context window of 1.
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                out["n_ctx"] = n
            mp = d.get("model_path") or d.get("model")
            if isinstance(mp, str) and mp.strip():
                out["model_id"] = mp
    except Exception:  # noqa: BLE001 — best effort; the static ModelEntry is the fallback
        pass
    # Cache only a SUCCESSFUL probe — so a server that was down at first call is
    # picked up once it comes online (self-healing), instead of being pinned to
    # the static fallback for the agent-server's whole lifetime. Store the
    # monotonic ts alongside the value so _probe_live_model can apply a TTL
    # and re-probe on a model hot-swap (T6/E2).
    if out["model_id"] is not None or out["n_ctx"] is not None:
        _LIVE_MODEL_PROBE_CACHE[base_url] = (out, time.monotonic())
    return out


def _probe_live_model(base_url: str | None, api_key: str | None = None) -> dict[str, Any]:
    """Return {"model_id": str|None, "n_ctx": int|None} for a llama.cpp /
    OpenAI-compatible server, from a cached /props probe. NEVER blocks a running
    event loop — that was the North Star #25 wedge: /props can hang the full 2s when
    the backend is slow/down, and this is reached from async routes (/models,
    /health) AND from kick()'s synchronous loop composition, all on the loop. When a
    loop is running, the blocking probe is offloaded to the default threadpool
    (fire-and-forget — it fills the cache) and THIS call returns the static fallback;
    the next call is served live from cache. Off the loop (CLI / worker thread) it
    blocks directly. Best-effort: Nones on any miss → caller uses the static config."""
    if not base_url:
        return {"model_id": None, "n_ctx": None}
    cached = _LIVE_MODEL_PROBE_CACHE.get(base_url)
    if cached is not None:
        # Canonical form: (result, monotonic_ts). Within _PROBE_TTL_S the
        # cached value is served as-is — no second /props call. Past the
        # TTL we fall through to re-probe so a model hot-swap (driver
        # restarted with a different served model / n_ctx) is observed
        # within a minute (T6/E2).
        if isinstance(cached, tuple) and len(cached) == 2:
            value, ts = cached
            if (time.monotonic() - ts) <= _PROBE_TTL_S:
                return value
        else:
            # Legacy / test-only direct-set: bare dict, no ts. Treat as
            # fresh — preserves the existing non-blocking test that
            # simulates a worker thread filling the cache directly.
            return cached
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        # Don't block the loop. Schedule ONE probe thread per base_url; concurrent
        # callers that arrive before the cache fills skip (in-flight) rather than each
        # spawning a redundant thread. The wrapper clears the in-flight marker in a
        # finally, so a FAILED probe (cache stays empty) can be retried on a later
        # call. The in-flight check runs only on the single-threaded loop, so the
        # check-then-add is race-free; the worker thread only discards.
        if base_url not in _LIVE_MODEL_PROBE_INFLIGHT:
            _LIVE_MODEL_PROBE_INFLIGHT.add(base_url)

            def _probe_then_clear(u: str = base_url, k: str | None = api_key) -> None:
                try:
                    _do_live_model_probe(u, k)
                finally:
                    _LIVE_MODEL_PROBE_INFLIGHT.discard(u)

            running.run_in_executor(None, _probe_then_clear)
        return {"model_id": None, "n_ctx": None}
    return _do_live_model_probe(base_url, api_key)


class _NoToolExecutor:
    """A read-only surface with no tools: the model answers directly. Any tool the
    model hallucinates fails loudly as an observation (it has none to call)."""

    def available_tools(self) -> list:
        return []

    async def execute(self, call) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=False,
            content="",
            error="no tools are available on this surface",
        )


def _release_process_memory() -> None:
    """Hand freed heap pages back to the OS after a memory-heavy run.

    Python frees objects, but glibc's allocator keeps the arenas instead of
    `munmap`-ing them — so after a bursty Deep Research run (concurrent legs
    fetching full pages + stacking ONNX batches) the process RSS stays pinned at
    the peak high-water mark forever, leaving a long-lived agent-server bloated
    and the next heavy op with less headroom. `gc.collect()` drops any lingering
    cycle-held buffers; `malloc_trim(0)` then returns the now-free arena pages.

    This is the retention half of the OOM root cause — its companion is the
    gather-leg concurrency cap that bounds the PEAK in the first place. Best
    effort: a non-glibc libc (musl / macOS) simply has no `malloc_trim`, and the
    gc pass still ran. Set `DISCO_DR_MALLOC_TRIM=0` to disable.
    """
    import gc

    gc.collect()
    if (os.environ.get("DISCO_DR_MALLOC_TRIM") or "1").strip().lower() in (
        "0", "off", "false", "none",
    ):
        return
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        trim = getattr(libc, "malloc_trim", None)
        if trim is not None:
            trim(0)
    except (OSError, AttributeError, ValueError):  # non-glibc / unavailable
        pass


class ConversationRuntime:
    """Builds a router from the CURRENT persisted config per request, plus one
    AgentLoop per conversation. `kick(cid)` schedules the loop in the background.

    Model assignments live in a shared ConfigStore (PMX_CONFIG) that the Settings
    UI writes; `_router_now()` reloads it each request, so reassigning a role takes
    effect on the next research call / new conversation without a restart."""

    def __init__(
        self,
        store: SqliteEventStore,
        *,
        config: RouterConfig | None = None,
        config_store: ConfigStore | None = None,
        secret_store: SecretStore | None = None,
        router: DefaultLLMRouter | None = None,
        enable_thinking: bool = False,
        mode: OperatingMode = OperatingMode.INTERACTIVE,
        research_providers: dict[str, Any] | None = None,
        sandbox_service: SandboxService | None = None,
        sandbox_spec: SandboxSpec | None = None,
        skill_store: SkillStore | None = None,
    ) -> None:
        self._store = store
        # The user's reusable instruction modules (.md skills). Read per-request
        # so a skill toggled in Settings affects the next conversation without a
        # restart — same live-reload model as the config + sandbox stores.
        self._skill_store = skill_store or SkillStore()
        # The Build surface runs tools through a SandboxBackend. An explicitly injected
        # service is an OVERRIDE (tests / `PMX_SANDBOX` at startup); otherwise the backend
        # is read PER-REQUEST from the persisted SandboxSettings (the Settings selector),
        # mirroring how `_router_now()` reloads model assignments — so a settings change
        # drives the next conversation's sandbox without a restart.
        self._injected_sandbox = sandbox_service
        self._sandbox_spec = sandbox_spec or SandboxSpec()
        # per-conversation driver model override (the Build chat model picker → the
        # AGENT_DRIVER for that conversation; RouterAgent applies it).
        # B0: PERSISTED (not just in-memory) — a server restart used to silently revert
        # every conversation's picked model to the default. Persisted to a JSON sidecar
        # next to the event DB (PMX_DB) so a resumed conversation keeps its model.
        db_path = disco_env("DB", "")
        self._override_path = f"{db_path}.overrides.json" if db_path else ""
        self._model_override: dict[str, str] = self._load_overrides()
        # Per-conversation surface ("research" | "build" | "deep_research"); set at
        # create time. PERSISTED to a JSON sidecar (B0 pattern, same as the model
        # override above) — DC-05 re-run #7 (2026-06-11): after a server restart the
        # in-memory dict was empty, _surface_of's recovery ladder needs a project
        # manifest to derive "build" but the manifest is only written by
        # _maybe_snapshot (which no-ops without a configured projects_root), so a
        # resumed Build conversation silently composed on the research surface →
        # _NoToolExecutor → the model's ONLY offered tool was `finish`. The whole
        # post-resume "degeneration" was a toolless loop, not model failure. The
        # _surface_of ladder stays as the fallback for pre-fix sidecar-less DBs.
        self._surface_path = f"{db_path}.surfaces.json" if db_path else ""
        self._surface: dict[str, str] = self._load_surfaces()
        # Per-conversation AUTONOMOUS flag (issue A), same B0 sidecar pattern as
        # surface. True = headless/unattended: the loop withholds ask_user, auto-
        # approves the plan, and forfeits cleanly instead of halting for a human.
        self._autonomous_path = f"{db_path}.autonomous.json" if db_path else ""
        self._autonomous: dict[str, bool] = self._load_autonomous()
        # Per-conversation ASSIST tier flag (T1), same B0 sidecar pattern.
        self._assist_path = f"{db_path}.assist.json" if db_path else ""
        self._assist: dict[str, bool] = self._load_assist()
        # Per-conversation server-side uploads sidecar directory (B0 pattern).
        # DC-07 (2026-06-11): uploads survive sandbox recreation.
        self._uploads_base = f"{db_path}.uploads" if db_path else ""
        self._executors: dict[str, DefaultToolExecutor] = {}
        self._pending_sessions: dict[str, SandboxSession] = {}
        self._cap_handlers: dict[str, Any] | None = None
        # per-conversation Deep Research depth tier (set at submit time).
        self._depth: dict[str, str] = {}
        # The encrypted-at-rest secret store (OpenRouter key). Its decrypted key is
        # overlaid into the provider env per request; if it's locked/empty the env
        # value (if any) is used instead.
        self._secret_store = secret_store or SecretStore()
        # The live retrieval/grounding providers for research_stream(). Injected
        # in tests (hermetic fakes); else lazily built from env on first use so
        # importing the runtime doesn't pull httpx until research is actually run.
        self._injected_research_providers = research_providers  # test fake (or None)
        self._research_providers: dict[str, Any] | None = None  # lazy cache (non-test)
        # the (remote, reranker_url, embedder_url, nli_url) tuple the cache was built
        # for — rebuild when the mode OR any endpoint URL changes.
        self._research_encoders_key: tuple | None = None
        # A statically-injected router (test seam) pins routing; otherwise the
        # router is rebuilt per request from the shared, persisted config store so
        # Settings assignments are actually honored.
        self._injected_router = router
        self._enable_thinking = enable_thinking
        if config_store is not None:
            self._config_store = config_store
        elif config is not None:
            self._config_store = ConfigStore(base_factory=lambda: config)
        else:
            self._config_store = ConfigStore()
        self._mode = mode
        self._loops: dict[str, AgentLoop] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # Cooperative-cancellation flags for Deep Research (whose engine isn't an
        # AgentLoop and can't be soft-cancelled the loop's way). Stop sets the flag;
        # the engine polls it at each sub-question/section boundary and halts,
        # keeping the partial report (resumable). See _execute_deep_research.
        self._cancel_flags: dict[str, asyncio.Event] = {}
        # Auto-suspend (lifecycle G): a build session is live only while a UI is
        # watching it. Track open WS connections per conversation; when the last one
        # closes, free the idle sandbox after a grace period (a quick reconnect — or
        # the WS-reconnect backoff — cancels it).
        self._connections: dict[str, int] = {}
        self._suspend_tasks: dict[str, asyncio.Task] = {}
        # BP-14: per-(cid, name) coalescing cache for capture-pane calls.
        self._session_view_cache: dict[tuple[str, str], tuple[float, SessionView]] = {}
        self._session_view_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._wake_locks: dict[str, asyncio.Lock] = {}
        # DC-04b: last-known session list per cid for stale-on-failure degradation.
        self._last_sessions: dict[str, list[SessionInfo]] = {}
        # RP-05 rung A: MCP client pool — built once at start from RouterConfig.mcp.
        self._mcp_pool: McpPool | None = None
        # Servers that need re-approval (ApprovalRequired at startup). Emitted to
        # WS clients as mcp_approval_required frames.
        self._mcp_approval_pending: dict[str, dict] = {}
        # RP-05 rung B: MCP HTTP clients (streamable_http transport). Managed
        # separately from the stdio pool; tools are merged at compose time.
        self._mcp_http_clients: dict[str, Any] = {}  # server_name -> McpHttpClient
        self._mcp_http_tools: dict[str, ToolDef] = {}  # qualified_name -> ToolDef
        # RP-05 rung B: retrieval-tier MCP providers (search/extract Protocol wrappers).
        self._mcp_retrieval_searches: list = []
        self._mcp_retrieval_extractions: list = []

    # The generative (text-producing) roles a model PICK drives. NLI_VERIFIER is a
    # cross-encoder (entailment scorer), NOT a chat model — pointing it at a picked
    # LLM would break verification, so it always follows its own assignment.
    _GENERATIVE_ROLES: tuple[ModelRole, ...] = (
        ModelRole.AGENT_DRIVER,
        ModelRole.RAG_ANSWERER,
        ModelRole.QUERY_REWRITER,
        ModelRole.SUMMARIZER,
    )

    def _router_now(
        self,
        pick: str | None = None,
        *,
        enable_thinking: bool | None = None,
        surface: str | None = None,
        autonomous: bool = False,
    ) -> DefaultLLMRouter:
        """The router for the CURRENT assignments. Cheap to rebuild (providers are
        plain objects; the HTTP client is created per call), so we reload the config
        each request rather than cache a stale router. `pick` (the model pill) is a
        catalogue key the user explicitly chose for THIS conversation; `enable_thinking`
        overrides the default reasoning mode for this request (the Think toggle)."""
        if self._injected_router is not None:
            return self._injected_router
        cfg = self._config_store.load()
        # A model PICK drives the ENTIRE generative pipeline, not just one role.
        # When the user picks (say) an OpenRouter DeepSeek, the expectation is that
        # DeepSeek does the whole job — the brain AND the context condensation AND
        # query rewriting AND answer synthesis — not that the picked model "leads"
        # while local models quietly do the summarizing/rewriting underneath. So we
        # reassign every generative role to the pick for this request. (Unknown keys
        # are ignored — fail safe to the saved assignment.)
        if pick and pick in cfg.models:
            reassigned = {**cfg.assignments, **{r: pick for r in self._GENERATIVE_ROLES}}
            cfg = cfg.model_copy(update={"assignments": reassigned})
        # Overlay the decrypted OpenRouter key into the env build_providers reads,
        # so OR models (api_key_env=PMX_OPENROUTER_API_KEY) authenticate without the
        # secret ever being on disk in plaintext.
        env = dict(os.environ)
        or_key = self._secret_store.get_openrouter_key()
        if or_key:
            env[OPENROUTER_API_KEY_ENV] = or_key
        thinking = self._enable_thinking if enable_thinking is None else enable_thinking
        providers = build_providers(cfg, env=env, enable_thinking=thinking)
        # DriverPrompts gives the AGENT_DRIVER role phase-aware system prompts (the
        # plan→approve→build flow); every other role/mode defers to the default
        # provider, so Research is unaffected. Enabled SKILLS (the user's reusable
        # .md instructions) are rendered and prepended to the driver prompts —
        # read fresh each request so a Settings toggle takes effect next run.
        # `surface` filters to skills scoped to it (build vs agent) — None = all.
        skills_block = render_skills_for_prompt(self._skill_store.enabled(), surface=surface)
        # The "agent" surface gets the task-agent prompt flavor (an identity reframe);
        # build + every other surface keep the build driver prompts unchanged.
        flavor = "agent" if surface == "agent" else "build"
        return DefaultLLMRouter(
            cfg,
            providers,
            prompt_provider=DriverPrompts(
                skills_block=skills_block, flavor=flavor, autonomous=autonomous
            ),
        )

    # Four surfaces: research (single-pass /ws/research stream), build (agent +
    # tools + plan gate, framed for software), agent (the SAME agent machinery
    # framed as a general task agent), deep_research (long-horizon plan → iterate
    # → report). "agent" is a framing copy of "build" — identical loop/tools/
    # sandbox/persistence — so it travels with build through every surface branch.
    _VALID_SURFACES: frozenset[str] = frozenset({"research", "build", "agent", "deep_research"})
    # The surfaces that compose the build agent loop (tools + sandbox + gate +
    # workspace snapshot/rehydrate). "build" and "agent" are behaviorally identical;
    # they differ only in frontend framing + entry. Branch on this set, never on the
    # bare string, so a new build-like surface can't silently miss a call site.
    _BUILD_LIKE_SURFACES: frozenset[str] = frozenset({"build", "agent"})
    # Surfaces that have a PLAN GATE the autonomous flag should auto-approve.
    # deep_research is NOT build-like (no build tools/sandbox) but its
    # plan→iterate→report flow DOES halt at AWAITING_PLAN_APPROVAL — so a
    # headless/autonomous DR run must auto-approve here too, or it stalls
    # forever. (research has no plan gate, so it stays excluded.)
    _AUTONOMOUS_SURFACES: frozenset[str] = frozenset({"build", "agent", "deep_research"})

    def set_surface(self, conversation_id: str, surface: str) -> None:
        """Select a conversation's surface before it runs. Build composes tools +
        sandbox + the BlastRadiusConfirm gate; Deep Research composes the plan-gate +
        the long-horizon engine; Research stays read-only + ungated. Idempotent
        until the loop is built. PERSISTED so a server restart cannot demote a
        Build/DR conversation to the toolless research default (DC-05 re-run #7)."""
        self._surface[conversation_id] = (
            surface if surface in self._VALID_SURFACES else "research"
        )
        self._save_surfaces()

    def _surface_of(self, conversation_id: str) -> str:
        """The conversation's surface, recovered durably across server restarts.
        In-memory `_surface` is authoritative when set — at create time via
        set_surface (which persists to the sidecar) or reloaded from the sidecar
        at startup. When it's missing — a pre-sidecar DB, or a sidecar lost with
        its DB — we recover from durable signals, MOST AUTHORITATIVE FIRST:
        - the `conversations.surface` column (written at create, app.py) — the
          real answer, durable in the same DB. This is the only rung that can tell
          "agent" from "build" (the project manifest can't — they snapshot
          identically) and it also fixes a pre-existing hole where a plan-gated
          Build with no manifest yet mis-derived deep_research below.
        - a project manifest on disk → a build-like surface (Research never snapshots).
        - a ReportEvent on the conversation log → Deep Research.
        Defaults to "research" when no durable signal exists. Recoveries are
        written through to the sidecar so the ladder runs at most once per cid."""
        cached = self._surface.get(conversation_id)
        if cached is not None:
            return cached
        # Rung 0 — the authoritative DB column (set at create). Cheap sync SQL on
        # the same connection the rungs below already use. This makes the
        # heuristic rungs a fallback only for legacy rows with a NULL surface.
        try:
            conn0 = getattr(self._store, "_conn", None)
            if conn0 is not None:
                row = conn0.execute(
                    "SELECT surface FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is not None and row["surface"] in self._VALID_SURFACES:
                    self._surface[conversation_id] = row["surface"]
                    self._save_surfaces()
                    return row["surface"]
        except Exception:  # noqa: BLE001 — best-effort recovery, fall through to heuristics
            pass
        store = self._project_store_now()
        if store is not None and store.status() == StorageStatus.OK:
            try:
                if store.get(conversation_id) is not None:
                    # cache + persist the recovery so subsequent lookups (and the
                    # next restart) don't re-derive
                    self._surface[conversation_id] = "build"
                    self._save_surfaces()
                    return "build"
            except Exception:  # noqa: BLE001 — best-effort recovery
                pass
        # Deep Research recovery: a ReportEvent on the log is the durable marker
        # (Research/Build never emit ReportEvent). Cheap SQL check — no need to
        # deserialize the whole log just to read the discriminator column.
        try:
            conn = getattr(self._store, "_conn", None)
            if conn is not None:
                kinds = {
                    row["kind"]
                    for row in conn.execute(
                        "SELECT DISTINCT kind FROM events WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                }
                if "report" in kinds:
                    self._surface[conversation_id] = "deep_research"
                    self._save_surfaces()
                    return "deep_research"
                # plan-event without any action-event marks a deep-research paused
                # at its plan gate (Build that survived a restart would also have
                # an on-disk project manifest, caught above).
                if "plan" in kinds and "action" not in kinds:
                    self._surface[conversation_id] = "deep_research"
                    self._save_surfaces()
                    return "deep_research"
        except Exception:  # noqa: BLE001 — best-effort recovery
            pass
        return "research"

    def _sandbox_service_now(self) -> SandboxService:
        """The active sandbox backend: the injected override if present, else built from
        the persisted SandboxSettings (reloaded each time — the Settings selector drives it)."""
        if self._injected_sandbox is not None:
            return self._injected_sandbox
        return build_sandbox_service(self._config_store.load().sandbox)

    def _driver_context_window(self) -> int | None:
        """The context window of the model currently assigned to AGENT_DRIVER, for
        A-S1's model-aware condensation threshold. Best-effort: None on any lookup
        miss so the condenser falls back to its safe defaults."""
        try:
            cfg = self._config_store.load()
            key = cfg.model_for(ModelRole.AGENT_DRIVER)
            entry = cfg.entry_for(key)
            # Prefer the model server's ACTUAL n_ctx over the static config — the
            # condenser must budget against the window the backend really serves,
            # not a config that may assume 128k (the "assuming 128k context" bug).
            live = _probe_live_model(
                entry.base_url,
                os.environ.get(entry.api_key_env) if entry.api_key_env else None,
            )
            return live["n_ctx"] or entry.context_window
        except Exception:  # noqa: BLE001 — never block loop construction on this
            return None

    async def prewarm_model_probe(self) -> None:
        """Warm the live-model /props cache OFF the event loop at startup, so the
        FIRST build's condenser budgets against the REAL context window, not the
        static config. _probe_live_model is non-blocking on the loop (it returns the
        static fallback and fills the cache from a worker thread), so without a
        pre-warm the very first _compose_build_loop reads the fallback and caches it
        for that loop's life. Here we await the blocking probe in a thread, so by the
        time any conversation composes, the cache is hot. Best-effort: never blocks
        boot."""
        try:
            cfg = self._config_store.load()
            key = cfg.model_for(ModelRole.AGENT_DRIVER)
            entry = cfg.entry_for(key)
            if entry and entry.base_url:
                api_key = (
                    os.environ.get(entry.api_key_env) if entry.api_key_env else None
                )
                await asyncio.to_thread(_do_live_model_probe, entry.base_url, api_key)
        except Exception:  # noqa: BLE001 — best effort; the static config is the fallback
            pass

    def _load_overrides(self) -> dict[str, str]:
        if self._override_path and os.path.exists(self._override_path):
            try:
                with open(self._override_path) as f:
                    data = json.load(f)
                return {str(k): str(v) for k, v in data.items() if v}
            except Exception:  # noqa: BLE001 — corrupt/missing → start empty, never crash
                return {}
        return {}

    def _save_overrides(self) -> None:
        if not self._override_path:
            return
        import tempfile
        try:
            dir_name = os.path.dirname(self._override_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._model_override, f)
                tmp_name = f.name
            os.replace(tmp_name, self._override_path)
        except Exception:  # noqa: BLE001 — persistence is best-effort, never fatal
            pass

    def _load_surfaces(self) -> dict[str, str]:
        """The persisted surface map (sidecar next to PMX_DB). Unknown values are
        dropped (treated as never-set → the recovery ladder still applies), so a
        hand-edited or future-versioned sidecar can't compose an invalid loop."""
        if self._surface_path and os.path.exists(self._surface_path):
            try:
                with open(self._surface_path) as f:
                    data = json.load(f)
                return {
                    str(k): str(v) for k, v in data.items() if v in self._VALID_SURFACES
                }
            except Exception:  # noqa: BLE001 — corrupt/missing → start empty, never crash
                return {}
        return {}

    def _save_surfaces(self) -> None:
        if not self._surface_path:
            return
        import tempfile
        try:
            dir_name = os.path.dirname(self._surface_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._surface, f)
                tmp_name = f.name
            os.replace(tmp_name, self._surface_path)
        except Exception:  # noqa: BLE001 — persistence is best-effort, never fatal
            pass

    def _load_autonomous(self) -> dict[str, bool]:
        if self._autonomous_path and os.path.exists(self._autonomous_path):
            try:
                with open(self._autonomous_path) as f:
                    return {str(k): bool(v) for k, v in json.load(f).items()}
            except Exception:  # noqa: BLE001 — corrupt/missing → start empty
                return {}
        return {}

    def _save_autonomous(self) -> None:
        if not self._autonomous_path:
            return
        import tempfile
        try:
            dir_name = os.path.dirname(self._autonomous_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._autonomous, f)
                tmp_name = f.name
            os.replace(tmp_name, self._autonomous_path)
        except Exception:  # noqa: BLE001 — best-effort
            pass

    def set_autonomous(self, conversation_id: str, value: bool = True) -> None:
        """Mark a conversation autonomous (headless) BEFORE it runs. Persisted (B0)."""
        self._autonomous[conversation_id] = bool(value)
        self._save_autonomous()

    def _effective_autonomous(self, conversation_id: str) -> bool:
        """The SINGLE source of truth for "is this conversation actually running
        headless". Autonomous governs the plan-gate auto-approve + ask/clarify
        suppression, so it only takes effect on surfaces that HAVE a plan gate:
        build, agent, AND deep_research (plan→iterate→report). Gating in ONE place
        keeps the prompt prefix (router), the loop's tool-suppression/auto-approve,
        AND the UI badge from disagreeing. The plain `research` surface has no plan
        gate, so an autonomous=True flag there is uniformly treated as interactive."""
        return (
            self._autonomous.get(conversation_id, False)
            and self._surface_of(conversation_id) in self._AUTONOMOUS_SURFACES
        )

    def is_autonomous(self, conversation_id: str) -> bool:
        # The public read (UI badge via /state extras) — gated, so the badge can't
        # show "autonomous" on a surface that has no headless affordance.
        return self._effective_autonomous(conversation_id)

    def _is_small_assist_default(self, entry: Any) -> bool:
        if not entry or not getattr(entry, "base_url", None):
            return False
        base_url = str(entry.base_url).lower()
        is_local = any(x in base_url for x in ("localhost", "127.0.0.1", "192.168.", ".local"))
        return is_local and "openrouter" not in base_url

    def _load_assist(self) -> dict[str, bool]:
        if self._assist_path and os.path.exists(self._assist_path):
            try:
                with open(self._assist_path) as f:
                    return {str(k): bool(v) for k, v in json.load(f).items()}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save_assist(self) -> None:
        if not self._assist_path:
            return
        import tempfile
        try:
            dir_name = os.path.dirname(self._assist_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._assist, f)
                tmp_name = f.name
            os.replace(tmp_name, self._assist_path)
        except Exception:  # noqa: BLE001
            pass

    def set_assist(self, conversation_id: str, value: bool = True) -> None:
        """Mark a conversation assist tier (T1). Persisted."""
        self._assist[conversation_id] = bool(value)
        self._save_assist()

    def _effective_assist(self, conversation_id: str) -> bool:
        """The SINGLE source of truth for the assist gate."""
        if conversation_id in self._assist:
            return self._assist[conversation_id]
        
        # Default policy
        override = self._model_override.get(conversation_id)
        router = self._router_now(pick=override)
        from disco.core.llm import ModelRole
        key = router._config.model_for(ModelRole.AGENT_DRIVER, override=override)
        entry = router._config.models.get(key)
        return self._is_small_assist_default(entry)

    def is_assist(self, conversation_id: str) -> bool:
        return self._effective_assist(conversation_id)

    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
        """Pin the driver model for a conversation (the Build chat model picker). The id
        is a catalogue KEY; RouterAgent reassigns AGENT_DRIVER to it. Must be set before
        the loop is built (at create time). PERSISTED (B0) so a restart keeps the pick."""
        if model_id:
            self._model_override[conversation_id] = model_id
            self._save_overrides()

    def driver_models(self) -> dict[str, Any]:
        """The driver-eligible models (live + tool-calling), deduped by underlying model,
        for the Build model picker — with the current default. Cost-legible: provider +
        free flag. Sourced from the live router config (Settings assignments honored)."""
        from disco.core.llm import ModelRole, Requirement

        cfg = self._config_store.load()
        seen: set[str] = set()
        models: list[dict[str, Any]] = []
        for key, m in cfg.models.items():
            if m.base_url is None or Requirement.TOOL_CALLING not in m.capabilities:
                continue
            if m.model_id in seen:
                continue
            seen.add(m.model_id)
            # Ground truth over declaration: prefer the live-served model name +
            # context window; fall back to the static ModelEntry on any probe miss.
            live = _probe_live_model(
                m.base_url, os.environ.get(m.api_key_env) if m.api_key_env else None
            )
            label = _model_label(live["model_id"] or m.model_id)
            ctx = live["n_ctx"] or m.context_window
            models.append(
                {
                    "id": key,
                    "label": label,
                    "provider": "openrouter" if m.provider == "openrouter" else "local",
                    "free": m.price_out_per_m == 0.0,
                    "context_window": ctx,
                }
            )
        try:
            default = cfg.model_for(ModelRole.AGENT_DRIVER)
        except Exception:  # noqa: BLE001 — no assignment → no default highlight
            default = None
        return {"models": models, "default": default}

    def _retrieval_handlers(self) -> dict[str, Any]:
        """Lazily build the search/extract capability handlers from the research
        providers — so the Build agent toolset's search/extract are REAL, not a false
        affordance, without eagerly importing httpx."""
        if self._cap_handlers is None:
            deps = self._research()
            search, extraction = self._compose_mcp_retrieval(deps)
            self._cap_handlers = retrieval_capability_handlers(search, extraction)
        return self._cap_handlers

    def _compose_mcp_retrieval(self, deps: dict[str, Any]) -> tuple[Any, Any]:
        """Compose the bundled search/extraction providers with the conversation's
        MCP retrieval providers (RP-05b §3) so MCP-discovered URLs flow through the
        SAME GroundingPipeline as bundled hits — reranked, extracted, NLI-verified,
        cited identically. Inert (returns the primaries unchanged) when no MCP
        retrieval-shaped tools are configured. THIS is the join that makes the
        retrieval tier reach the pipeline — registering providers under unconsumed
        broker names did not."""
        from disco.tools.mcp.retrieval_tier import compose_with_mcp

        return compose_with_mcp(
            deps["search"],
            deps["extraction"],
            mcp_searches=self._mcp_retrieval_searches,
            mcp_extractions=self._mcp_retrieval_extractions,
        )

    def _build_broker(self) -> CapabilityBroker:
        """The orchestrator-side capability broker for a Build conversation. search/
        extract are backed by the research providers (lazy); provider keys never reach
        the sandbox (§6). The kill switch calls broker.revoke_all()."""
        broker = CapabilityBroker()

        async def _search(*, query: str, limit: int = 8) -> Any:
            return await self._retrieval_handlers()["search"](query=query, limit=limit)

        async def _extract(*, url: str) -> Any:
            return await self._retrieval_handlers()["extract"](url=url)

        broker.register("search", _search)
        broker.register("extract", _extract)
        return broker

    def _loop_for(self, conversation_id: str) -> AgentLoop:
        loop = self._loops.get(conversation_id)
        if loop is None:
            # The per-conversation model PICK drives the WHOLE generative pipeline:
            # build the router with it so the brain AND the summarizer (context
            # condensation) AND any other generative role all run on the picked
            # model — not just the driver while local models summarize underneath.
            override = self._model_override.get(conversation_id)
            # Surface FIRST: it scopes which skills the router injects (a build-only
            # skill shouldn't reach an agent conversation's prompt, and vice versa).
            surface = self._surface_of(conversation_id)
            # Single source of truth (gated by surface) shared with the loop compose
            # below and the UI badge — they can't desync.
            autonomous = self._effective_autonomous(conversation_id)
            router = self._router_now(pick=override, surface=surface, autonomous=autonomous)
            # Research↔Build isolation: the SURFACE picks the agent class, so
            # completion semantics (prose=answer for Research vs affirmative
            # `finish` for Build) are owned by type, not a shared mode flag.
            # "agent" is a build-like surface — same BuildAgent + build loop.
            if surface in self._BUILD_LIKE_SURFACES:
                agent: RouterAgent = BuildAgent(
                    router, conversation_id=conversation_id, model_override=override
                )
            else:
                agent = ResearchAgent(
                    router, conversation_id=conversation_id, model_override=override
                )
            if surface in self._BUILD_LIKE_SURFACES:
                loop = self._compose_build_loop(conversation_id, router, agent)
            elif surface == "deep_research":
                loop = self._compose_deep_research_loop(conversation_id, router, agent)
            else:
                loop = AgentLoop(
                    conversation_id,
                    self._store,
                    agent,
                    _NoToolExecutor(),
                    router,
                    RuleBasedAnalyzer(),
                    NeverConfirm(),  # Research surface: no human gate
                    NoOpCondenser(),
                    RouterSummarizer(router),
                    mode=self._mode,
                )
            # Watch-it-write: wire the loop's stream sink to the store's ephemeral
            # broadcast so streamed file-content frames reach the conversation's WS
            # subscribers live (never persisted). Bound to this cid.
            loop.stream_sink = lambda frame, _cid=conversation_id: self._store.publish_ephemeral(
                _cid, frame
            )
            self._loops[conversation_id] = loop
        return loop

    def _mcp_egress_hosts(self) -> frozenset[str]:
        """Compute the UNION of MCP HTTP server hosts for egress allowlisting.

        Returns hosts from enabled HTTP servers' URLs + allowed_hosts config.
        Empty frozenset if no HTTP servers are configured or started.
        """
        from disco.tools.mcp.http_egress import build_egress_union

        url_hosts: list[str] = []
        allowed_hosts: list[str] = []

        for client in self._mcp_http_clients.values():
            url_hosts.append(client._url)
            allowed_hosts.extend(client.allowed_hosts)

        if not url_hosts and not allowed_hosts:
            return frozenset()

        return build_egress_union(
            frozenset(),
            http_server_urls=url_hosts,
            http_server_allowed_hosts=allowed_hosts,
        )

    def _mcp_proxy_env(self) -> dict[str, str] | None:
        """The HTTP(S)_PROXY env the orchestrator-side MCP HTTP client routes
        through, governed by the SAME PMX_BUILD_EGRESS posture as the sandbox spec
        (single source of truth — no divergent egress policy).

        filtered → proxy_env(host, EGRESS_PROXY_PORT); a host outside the unioned
        allowlist is denied 403 by the proxy, so the client cannot bypass the
        sidecar (workorder §2 "no path bypasses it"). BP-G10: filtered is now
        the default (matches the new default sandbox posture). open (explicit
        PMX_BUILD_EGRESS=open) → None (direct). host comes from
        PMX_MCP_EGRESS_PROXY_HOST (default loopback). See
        docs/workorders/RP-05b-orchestrator-proxy-decision.md."""
        posture = disco_env("BUILD_EGRESS", "filtered").lower().strip()
        if posture != "filtered":
            return None
        from disco.tools.sandbox._container import EGRESS_PROXY_PORT, proxy_env

        host = disco_env("MCP_EGRESS_PROXY_HOST", "127.0.0.1")
        return proxy_env(host, EGRESS_PROXY_PORT)

    def _build_sandbox_spec(
        self,
        *,
        mcp_egress_hosts: frozenset[str] | None = None,
    ) -> SandboxSpec:
        """The egress-posture spec for a Build sandbox. FILTERED by default
        (BP-G10: build boxes get the allowlisting proxy now that E8 wired the
        proxy on every backend — gVisor, podman, local). Open only when
        PMX_BUILD_EGRESS=open is set explicitly (an escape hatch for debug
        / dev when a real network is genuinely required). Used both by
        _compose_build_loop and upload_session so pending sessions and build
        sessions share the same spec.

        When mcp_egress_hosts is provided, they are UNIONed into the egress_allow set
        (SUPERSET, not replacement) — the pre-existing registry hosts AND the MCP
        hosts both survive (rung B egress-proxy routing)."""
        egress = disco_env("BUILD_EGRESS", "filtered").lower().strip()
        if egress == "filtered":
            base_allow = REGISTRY_EGRESS_ALLOW
            if mcp_egress_hosts:
                base_allow = frozenset(base_allow | mcp_egress_hosts)
            return self._sandbox_spec.model_copy(update={"egress_allow": base_allow})
        # Open mode grants full NETWORK — there is no allowlist to union MCP hosts
        # into; they are already reachable. mcp_egress_hosts only matters under
        # filtered posture (above).
        spec_update = {"permitted": self._sandbox_spec.permitted | {Capability.NETWORK}}
        return self._sandbox_spec.model_copy(update=spec_update)

    def upload_session(self, conversation_id: str) -> SandboxSession:
        """Session uploads write through. The executor's live session when a
        loop exists; otherwise a pending session the NEXT build loop adopts."""
        executor = self._executors.get(conversation_id)
        if executor is not None:
            return executor._sandbox
        if conversation_id not in self._pending_sessions:
            self._pending_sessions[conversation_id] = SandboxSession(
                self._sandbox_service_now(),
                self._build_sandbox_spec(mcp_egress_hosts=self._mcp_egress_hosts()),
                conversation_id=conversation_id,
                on_recreate=lambda: self._rehydrate_after_recreate(conversation_id),
            )
        return self._pending_sessions[conversation_id]

    def store_upload(self, conversation_id: str, filename: str, data: bytes) -> None:
        """[DC-07] Store an uploaded file in the server-side sidecar directory."""
        if not self._uploads_base:
            return
        path = Path(self._uploads_base) / conversation_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get_upload_names(self, conversation_id: str) -> set[str]:
        """[DC-07] List filenames currently held server-side for this conversation."""
        if not self._uploads_base:
            return set()
        path = Path(self._uploads_base) / conversation_id
        if not path.is_dir():
            return set()
        return {p.name for p in path.iterdir() if p.is_file()}

    def get_upload_size(self, conversation_id: str) -> int:
        """[DC-07] Total bytes of server-side uploads for this conversation."""
        if not self._uploads_base:
            return 0
        path = Path(self._uploads_base) / conversation_id
        if not path.is_dir():
            return 0
        return sum(p.stat().st_size for p in path.iterdir() if p.is_file())

    def _compose_build_loop(
        self, conversation_id: str, router: DefaultLLMRouter, agent: RouterAgent
    ) -> AgentLoop:
        """[Agent surface] Compose — not reinvent — the loop for Build mode: the agent
        toolset (Prompt 1) over a resilient SandboxSession, the SecurityAnalyzer, the
        BlastRadiusConfirm gate (NOT Research's NeverConfirm), and the real condenser. The
        executor is held so the kill switch can revoke caps + tear down the sandbox."""
        broker = self._build_broker()
        # Adopt a pending session (created by upload_session for a pre-kick upload) so
        # the build's workspace IS the one uploads landed in — object identity, not a
        # re-read of the directory. Create a fresh session only when there is none.
        session = self._pending_sessions.pop(conversation_id, None)
        if session is None:
            session = SandboxSession(
                self._sandbox_service_now(),
                self._build_sandbox_spec(mcp_egress_hosts=self._mcp_egress_hosts()),
                conversation_id=conversation_id,
                # Mid-run death (transport drop / OOM): restore the last snapshot
                # into the fresh instance before the agent retries (bp-13 §2).
                on_recreate=lambda: self._rehydrate_after_recreate(conversation_id),
            )
        executor = DefaultToolExecutor(
            build_default_registry(),
            agent_scope(),
            sandbox=session,
            broker=broker,
            conversation_id=conversation_id,
            assist=self._effective_assist(conversation_id),
        )
        # RP-05 rung A+B: extend the registry with MCP tools from the pool
        # snapshot (stdio) AND the HTTP-managed tools (rung B streamable_http).
        # The snapshot is frozen per conversation — list_changed notifications do
        # NOT mutate this list.
        all_mcp_tools: list[ToolDef] = []
        if self._mcp_pool is not None and self._mcp_pool.started:
            all_mcp_tools.extend(self._mcp_pool.snapshot())
        if self._mcp_http_tools:
            all_mcp_tools.extend(list(self._mcp_http_tools.values()))

        if all_mcp_tools:
            # RP-05c: apply advertised/callable split. Over the cap, only
            # non-MCP tools + tool_search are advertised; all remain callable.
            cfg = self._config_store.load()
            max_schemas = cfg.mcp.max_active_schemas if cfg.mcp else 20
            _apply_mcp_scope(
                executor, all_mcp_tools, self._mcp_call_target,
                max_active_schemas=max_schemas,
            )

            # RP-05b §3: the retrieval-tier MCP providers are composed into the
            # bundled search/extraction in `_compose_mcp_retrieval` (consumed by
            # research_stream, _execute_deep_research, and the Build broker's
            # search/extract handlers) — so MCP-discovered URLs flow through the
            # real GroundingPipeline. Registering them here under `mcp_search_*` /
            # `mcp_extract_*` broker names was DEAD: nothing called those names
            # (the Build agent calls `search`/`extract`; Research bypasses the
            # broker). The cap-handler cache is invalidated when the providers are
            # (re)built so the composition picks up the live set.

        # Emit mcp_approval_required frames for any servers that need re-approval
        for server, info in self._mcp_approval_pending.items():
            try:
                self._store.publish_ephemeral(
                    conversation_id,
                    {
                        "type": "mcp_approval_required",
                        "server": server,
                        "description_hash": info.get("new_hash", ""),
                        "old_description_hash": info.get("old_hash", ""),
                    },
                )
            except Exception:
                pass  # best-effort; WS frame emission is not critical
        self._executors[conversation_id] = executor
        return AgentLoop(
            conversation_id,
            self._store,
            agent,
            executor,
            router,
            RuleBasedAnalyzer(),
            # DC-03: sandboxed ops auto-approve (confinement is the blast radius);
            # host-scope/unknown ops keep ConfirmRisky semantics; publish always gates.
            BlastRadiusConfirm(),
            # A-S1: derive condensation thresholds from the AGENT_DRIVER model's
            # context window (soft 65% / hard 80%) instead of the old bare 24k/32k.
            LLMSummarizingCondenser(context_window=self._driver_context_window()),
            RouterSummarizer(router),
            # Build starts in PLANNING. The planner has a context-rich surface — it
            # can READ to explore (file_list/file_read in the workspace, search/extract
            # on the web) before calling `submit_plan`. This mirrors Claude Code's plan
            # mode: writes/edits/shell are off the table until approval, but the
            # planner can gather context first. approve_plan flips to execution; the
            # per-action gate above still governs the build that follows.
            mode=OperatingMode.PLANNING,
            planning_tools=frozenset(
                {"submit_plan", "file_list", "file_read", "search", "extract"}
            ),
            # Same gated source of truth as the router prefix and the UI badge —
            # _compose_build_loop only runs for build-like surfaces today, but reading
            # the gated value means a future caller can't desync the loop's behavior
            # from the prompt prefix.
            autonomous=self._effective_autonomous(conversation_id),
            assist=self._effective_assist(conversation_id),
        )

    # ---- deep research surface ---------------------------------------------

    def _compose_deep_research_loop(
        self, conversation_id: str, router: DefaultLLMRouter, agent: RouterAgent
    ) -> AgentLoop:
        """Compose the loop frame for Deep Research. The loop itself doesn't drive
        the research — `_run_with_persistence` short-circuits `loop.run()` for
        this surface and runs `DeepResearchRun` directly. The AgentLoop exists
        here as the host for the plan-approval gate (we reuse Build's plan
        machinery: same PlanEvent, same AWAITING_PLAN_APPROVAL status, same
        approve_plan WS frame). No tools, no risk gate, no condenser — Deep
        Research's iteration lives in the engine, not the loop."""
        return AgentLoop(
            conversation_id,
            self._store,
            agent,
            _NoToolExecutor(),
            router,
            RuleBasedAnalyzer(),
            NeverConfirm(),  # no risky action gate — read-only research
            NoOpCondenser(),  # the engine manages its own corpus; no View condense
            RouterSummarizer(router),
            mode=OperatingMode.PLANNING,
            # Configure planning_tools so the loop's mode-tracking is consistent
            # with Build (PLANNING → LONG_HORIZON on approve_plan). The actual
            # plan-event is emitted synthetically by _run_with_persistence; the
            # loop never sees a submit_plan tool call.
            planning_tools=frozenset({"submit_plan"}),
        )

    def _depth_for(self, conversation_id: str) -> DepthTier:
        """The depth tier for this conversation. Stored in `_depth` per cid (set
        at create-time by the agent-server's POST /conversations handler).
        Defaults to STANDARD_DEEP — the everyday Deep Research run."""
        raw = self._depth.get(conversation_id) if hasattr(self, "_depth") else None
        if raw is None:
            return DepthTier.STANDARD_DEEP
        try:
            return DepthTier(raw)
        except ValueError:
            return DepthTier.STANDARD_DEEP

    def set_depth(self, conversation_id: str, tier: str | None) -> None:
        """Pin the Deep Research depth tier for this conversation (set at submit
        time by the UI's tier selector). Stored in memory; recovery falls to the
        default after restart, which is acceptable for a follow-up turn (rare
        for Deep Research — the run is the conversation)."""
        if not hasattr(self, "_depth"):
            self._depth: dict[str, str] = {}
        if tier and tier in {t.value for t in DepthTier}:
            self._depth[conversation_id] = tier

    # ---- research surface (Stage 4) -----------------------------------------

    def _research(self) -> dict[str, Any]:
        # A statically-injected provider set (tests) is used as-is. Otherwise build
        # from the PERSISTED encoder mode, and rebuild if the Settings toggle changed
        # it — so flipping local↔remote takes effect on the next research run without
        # a restart (rebuild is cheap: fastembed models are module-cached, not per
        # provider instance).
        if self._injected_research_providers is not None:
            return self._injected_research_providers
        cfg = self._config_store.load()
        enc, sch, ext = cfg.encoders, cfg.search, cfg.extraction
        # paid-provider keys resolve from os.environ by the configured env-var NAME
        # (same mechanism as model api_key_env); bundled providers need no key.
        search_key = os.environ.get(sch.api_key_env, "") if sch.api_key_env else ""
        ext_key = os.environ.get(ext.api_key_env, "") if ext.api_key_env else ""
        key = (
            enc.remote, enc.reranker_url, enc.embedder_url, enc.nli_url,
            sch.provider, sch.base_url, sch.api_key_env,
            ext.provider, ext.base_url, ext.api_key_env,
        )
        if self._research_providers is None or self._research_encoders_key != key:
            from disco.retrieval.live import build_live_retrieval

            self._research_providers = build_live_retrieval(
                remote=enc.remote,
                reranker_url=enc.reranker_url,
                embedder_url=enc.embedder_url,
                nli_url=enc.nli_url,
                search_provider=sch.provider,
                search_base_url=sch.base_url,
                search_api_key=search_key,
                extraction_provider=ext.provider,
                extraction_base_url=ext.base_url,
                extraction_api_key=ext_key,
            )
            self._research_encoders_key = key
        return self._research_providers

    def research_stream(
        self,
        query: str,
        *,
        model_override: str | None = None,
        drop_weak: bool = False,
        domains_deny: frozenset[str] = frozenset(),
        think: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a live grounded answer as the UI's research frames (state →
        token… → final). Composes the shared router with the live retrieval
        providers, honoring the re-scope controls: the pill picks the answerer
        model, `domains_deny` filters discovery, `drop_weak` prunes the answer, and
        `think` runs the answerer in reasoning mode (it thinks, then the answer
        streams; the reasoning is never emitted as answer tokens)."""
        from disco.retrieval.streaming import stream_research_answer

        deps = self._research()
        # RP-05b §3: MCP retrieval providers join the citation path here too — the
        # composite hands MCP-discovered hits to the SAME GroundingPipeline.
        search, extraction = self._compose_mcp_retrieval(deps)
        return stream_research_answer(
            query,
            router=self._router_now(pick=model_override, enable_thinking=think),
            search=search,
            extraction=extraction,
            reranker=deps["reranker"],
            nli=deps["nli"],
            domains_deny=domains_deny,
            drop_weak=drop_weak,
            think=think,
        )

    def kick(self, conversation_id: str) -> None:
        """Schedule the loop to run (idempotent: a no-op if already running). The
        loop itself decides if there's unprocessed work and streams events.

        Wraps `loop.run()` in a task that — for Build conversations with a valid
        ProjectStore configured — first REHYDRATES the persisted workspace on
        the very first kick, and SNAPSHOTS it after every run that ends in
        FINISHED. Failures are reported as ambient system-reminder events on
        the conversation log (the user + model both see them), never as
        exceptions that crash the task."""
        existing = self._tasks.get(conversation_id)
        if existing is not None and not existing.done():
            return  # already running; the new message is picked up at the next step
        loop = self._loop_for(conversation_id)
        self._tasks[conversation_id] = asyncio.create_task(
            self._run_with_persistence(conversation_id, loop)
        )

    async def _run_with_persistence(
        self, conversation_id: str, loop: AgentLoop
    ) -> Any:
        """Snapshot/rehydrate wrapper around `loop.run()`. Surface-aware:
        - Build → snapshot+rehydrate the workspace as before.
        - Deep Research → short-circuit `loop.run()` on the post-plan-approval
          turn and run the DeepResearchRun engine; emit the ReportEvent +
          StatusEvent(FINISHED) directly.
        - Research → unchanged (the loop runs, no persistence)."""
        surface = self._surface_of(conversation_id)

        if surface == "deep_research":
            # Short-circuit the loop for Deep Research. The plan-mode intercept
            # the loop would otherwise run isn't useful here — Deep Research
            # decomposes the query algorithmically (not via an LLM planning
            # round) and the engine handles the rest. The loop's plan-approval
            # state machine is reused via control-op routing; the loop's
            # `run()` itself is not the right driver.
            await self._maybe_run_deep_research(conversation_id)
            # Return the (possibly-updated) state. The store reflects whatever
            # we emitted.
            return await self._store.get_state(conversation_id)

        # Rehydrate hook: BEFORE the loop runs for the first time, if a project
        # storage path is configured AND a prior snapshot exists for this cid,
        # write its files into the (lazy) sandbox so the agent sees them. Reads
        # are cheap; the rehydrate only writes if there are files on disk.
        if surface in self._BUILD_LIKE_SURFACES:
            await self._maybe_rehydrate(conversation_id)
            # DC-07: re-materialize server-side uploads into the fresh sandbox.
            # This is the SINGLE chokepoint that covers ALL paths:
            #   - post-restart resume → fresh sandbox via lazy compose
            #   - first build after upload (pending session adoption or fresh)
            #   - mid-run recreation (also handled by _rehydrate_after_recreate)
            await self._rematerialize_uploads(conversation_id)

        state = await loop.run()

        # Snapshot hook: capture the workspace whenever a build run ENDS — not only
        # on FINISHED. A run that ends STUCK/ERROR/PAUSED still wrote real files (the
        # model may have built most of the deliverable before getting stuck); snapshot
        # so that work is NOT lost (it was — a stuck iteration silently discarded a
        # whole feature it had written). Snapshot is idempotent + cheap.
        _ENDED = {
            ConversationStatus.FINISHED,
            ConversationStatus.STUCK,
            ConversationStatus.ERROR,
            ConversationStatus.PAUSED,
        }
        if surface in self._BUILD_LIKE_SURFACES and state.execution_status in _ENDED:
            await self._maybe_snapshot(conversation_id)
            # FINISHED now rides the idle sweep like STUCK/ERROR/PAUSED;
            # suspend = sweep_idle_once -> _suspend
        return state

    async def _teardown_sandbox(self, conversation_id: str) -> None:
        """Destroy a conversation's sandbox session (frees the container/port/memory +
        the idle preview server) while KEEPING the event log + the project snapshot. A
        later run re-creates the sandbox and rehydrates. Callers MUST ensure the
        workspace is durable (snapshotted) first — this does not snapshot."""
        executor = self._executors.pop(conversation_id, None)
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.kill()  # destroys the sandbox instance (§6.4)
        pending = self._pending_sessions.pop(conversation_id, None)
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.destroy()
        self._loops.pop(conversation_id, None)  # force a fresh sandbox on the next run
        # BP-14: drop the capture-pane coalescing cache + locks for this conversation —
        # each cache entry pins up to 100KB of captured output and would otherwise
        # accumulate for the life of the server process.
        for key in [k for k in self._session_view_cache if k[0] == conversation_id]:
            del self._session_view_cache[key]
        for key in [k for k in self._session_view_locks if k[0] == conversation_id]:
            del self._session_view_locks[key]
        self._wake_locks.pop(conversation_id, None)
        self._last_sessions.pop(conversation_id, None)  # don't ghost a stale list (DC-04b)
        # The sandbox (and its files) are gone, so the NEXT run must rehydrate the
        # snapshot into a fresh sandbox. Clear the rehydrate-once flag — otherwise
        # `_maybe_rehydrate` skips it and the continuation runs in an EMPTY workspace,
        # silently losing all prior work (the "can't keep building after the first
        # plan finished" bug — the second iteration started from nothing).
        rehydrated = getattr(self, "_rehydrated", None)
        if rehydrated is not None:
            rehydrated.discard(conversation_id)

    async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        """Startup reconciliation. A conversation whose latest status is RUNNING but
        whose loop died with the previous server process is an ORPHAN: it shows
        'RUNNING' forever in History / the Deep Research read-only view, and its
        sandbox/GPU may have leaked. On boot there are NO live loops, so every
        RUNNING conversation is stale. Mark each PAUSED (resumable) + drop an
        environment note so the user can pick it up. Returns the count reconciled.

        Single-owner ('local') today; extend across owners when auth lands.
        """
        reconciled = 0
        cursor: str | None = None
        page = 200
        while True:
            ids = await self._store.list_conversations(owner_id=owner_id, limit=page, cursor=cursor)
            if not ids:
                break
            for cid in ids:
                # One unreadable conversation must never abort server boot.
                with contextlib.suppress(Exception):
                    state = await self._store.get_state(cid)
                    if state.execution_status is ConversationStatus.RUNNING:
                        await self._store.append(
                            cid,
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(
                                    role="user",
                                    content=(
                                        "⚠️ This run was interrupted when the server restarted, "
                                        "so its sandbox was reclaimed. It's paused — send a "
                                        "message to pick it up (your saved files restore on the "
                                        "next step)."
                                    ),
                                ),
                            ),
                        )
                        await self._store.append(
                            cid,
                            StatusEvent(
                                status=ConversationStatus.PAUSED,
                                detail="reconciled: orphaned RUNNING after server restart",
                            ),
                        )
                        reconciled += 1
            if len(ids) < page:
                break
            cursor = str((int(cursor) if cursor else 0) + len(ids))
        if reconciled:
            _LOG.info("reconciled %d orphaned RUNNING conversation(s) on startup", reconciled)

        # Orphan container sweep: destroy backend containers for conversations that are
        # terminal or suspended. These accumulate when sandboxes are not torn down cleanly
        # (transport drops, crashes, etc.) and consume memory/GPU on the sandbox host.
        # The sweep also covers pmx-egr-* egress sidecars (same label).
        _TERMINAL = {
            ConversationStatus.FINISHED,
            ConversationStatus.STUCK,
            ConversationStatus.ERROR,
            ConversationStatus.PAUSED,
            # IDLE = stopped-by-user (the PRIMARY live stop path — bp-12). After a
            # restart its container is unreachable garbage like any other: handles
            # are in-memory and a resume always builds a FRESH instance.
            ConversationStatus.IDLE,
        }
        await self._sweep_orphan_containers(owner_id=owner_id, terminal_statuses=_TERMINAL)

        return reconciled

    async def _sweep_orphan_containers(
        self,
        *,
        owner_id: str,
        terminal_statuses: set,
    ) -> int:
        """Destroy containers for conversations that are terminal or suspended.
        Called from reconcile_orphaned_runs at startup. Returns count destroyed."""
        service = self._sandbox_service_now()
        live_cids = await service.list_live_instances()
        destroyed = 0
        for cid in live_cids:
            with contextlib.suppress(Exception):
                if not await self._store.conversation_exists(cid):
                    await service.destroy_by_conversation(cid)
                    destroyed += 1
                    continue
                state = await self._store.get_state(cid)
                if state.execution_status in terminal_statuses:
                    await service.destroy_by_conversation(cid)
                    _LOG.info(
                        "swept orphan container cid=%s status=%s",
                        cid,
                        state.execution_status.value,
                    )
                    destroyed += 1
        # Process backend: sweep workspace dirs older than 7 days (no container layer).
        if isinstance(service, ProcessSandboxService):
            with contextlib.suppress(Exception):
                stale = await service.sweep_stale_workspaces()
                if stale:
                    _LOG.info("swept %d stale process workspace dir(s)", stale)
            # Also sweep orphaned /tmp/pmx-sbx-* root dirs from prior process runs
            # (each ProcessSandboxService.__init__ creates a NEW root via mkdtemp,
            # so roots accumulate across restarts without this cleanup).
            with contextlib.suppress(Exception):
                stale_roots = await service.sweep_stale_roots()
                if stale_roots:
                    _LOG.info("swept %d orphaned pmx-sbx-* root dir(s)", stale_roots)
        if destroyed:
            _LOG.info("swept %d orphan container(s) at startup", destroyed)
        return destroyed

    # ---- MCP pool lifecycle (RP-05 rung A) ---------------------------------

    async def _start_mcp_pool(self) -> None:
        """Start the MCP client pool if mcp.enabled + servers are configured.
        Called once at runtime startup (from the app lifespan). Loads approvals
        from the mcp_approvals table. On ApprovalRequired, the affected server
        is refused and the WS frame is dispatched; other servers still start.

        Rung B: also starts streamable_http servers via McpHttpClient."""
        cfg = self._config_store.load()
        mcp_cfg = cfg.mcp
        if not mcp_cfg.enabled or not mcp_cfg.servers:
            return

        from disco.tools.mcp.approval import ApprovalRequired
        from disco.tools.mcp.config import (
            McpServerConfig as TypedMcpServerConfig,
        )
        from disco.tools.mcp.config import (
            McpSettings as TypedMcpSettings,
        )
        from disco.tools.mcp.migrations import list_mcp_approvals

        # Read existing approvals from the DB (D1: security gate production path)
        approvals: dict[str, str] = {}
        try:
            conn = getattr(self._store, "_conn", None)
            if conn is not None:
                for row in list_mcp_approvals(conn):
                    approvals[row["server"]] = row["description_hash"]
        except Exception:
            _LOG.warning("MCP pool: failed to read approvals from DB", exc_info=True)

        # Split servers: stdio → pool, streamable_http → HTTP clients. The
        # core RouterConfig.mcp.servers schema is `dict[str, dict]` (loose,
        # for Settings writeback), so the value type isn't McpServerConfig
        # out of the loader — upgrade each entry to the typed model so the
        # transport/risk-tier fields are real attributes the pool/HTTP
        # branch can branch on. The Pydantic coerce also surfaces any
        # misconfiguration as a clear ValidationError at startup.
        http_servers: dict[str, McpServerConfig] = {}
        stdio_servers: dict[str, McpServerConfig] = {}
        for name, srv_raw in mcp_cfg.servers.items():
            srv = TypedMcpServerConfig.model_validate({"name": name, **srv_raw})
            if srv.transport == "streamable_http":
                http_servers[name] = srv
            else:
                stdio_servers[name] = srv

        # Start stdio pool
        if stdio_servers:
            typed = TypedMcpSettings(
                enabled=mcp_cfg.enabled,
                servers=stdio_servers,
                max_active_schemas=mcp_cfg.max_active_schemas,
            )
            self._mcp_pool = McpPool(typed, secrets=self._secret_store, approvals=approvals)
            try:
                await self._mcp_pool.start()
            except Exception:
                _LOG.warning("MCP pool: failed to start", exc_info=True)

            # D1/D3: collect servers that need re-approval from the pool status
            if self._mcp_pool is not None:
                # E6 (#10): the pool is the source of the AUTHORITATIVE new_hash
                # (the SHA-256 of the canonicalized tool descriptions the live
                # server just advertised). Persist it to the shared
                # mcp_approval_pending table so the app-server — which serves
                # GET /api/mcp to the frontend — can surface it on the
                # ApprovalDiff. Without this row, the UI only sees the STORED
                # (old) hash from mcp_approvals, which makes the diff useless
                # (or worse, shows the same value for both old and new).
                pending_db_conn = getattr(self._store, "_conn", None)
                for name, info in self._mcp_pool.approval_pending().items():
                    old_hash = info.get("old_hash", "")
                    new_hash = info.get("new_hash", "")
                    self._mcp_approval_pending[name] = {
                        "old_hash": old_hash,
                        "new_hash": new_hash,
                    }
                    _LOG.warning(
                        "MCP pool: server %r refused — re-approval required "
                        "(old=%s… new=%s…)",
                        name, old_hash[:12], new_hash[:12],
                    )
                    if pending_db_conn is not None and old_hash and new_hash:
                        try:
                            from disco.tools.mcp.migrations import (
                                set_mcp_approval_pending,
                            )
                            set_mcp_approval_pending(
                                pending_db_conn, name, old_hash, new_hash,
                            )
                        except Exception:
                            _LOG.warning(
                                "MCP pool: failed to persist pending approval "
                                "for %r (UI will fall back to stored hash only)",
                                name,
                                exc_info=True,
                            )

        # Start HTTP servers (rung B)
        for name, srv in http_servers.items():
            if not srv.enabled:
                _LOG.debug("McpPool: HTTP server %r is disabled — skipping", name)
                continue

            try:
                await self._connect_http(name, srv, approvals)
            except ApprovalRequired as exc:
                _LOG.warning(
                    "McpPool: HTTP server %r refused — description_hash changed "
                    "(%s → %s) — re-approval required",
                    name, exc.old_hash[:12], exc.new_hash[:12],
                )
                self._mcp_approval_pending[name] = {
                    "old_hash": exc.old_hash,
                    "new_hash": exc.new_hash,
                }
                # E6: same drift-persistence path as the stdio branch — write
                # the AUTHORITATIVE new_hash the live HTTP server advertised to
                # the shared mcp_approval_pending table so the app-server's
                # GET /api/mcp can surface it on the ApprovalDiff.
                http_db_conn = getattr(self._store, "_conn", None)
                if http_db_conn is not None:
                    try:
                        from disco.tools.mcp.migrations import (
                            set_mcp_approval_pending,
                        )
                        set_mcp_approval_pending(
                            http_db_conn, name, exc.old_hash, exc.new_hash,
                        )
                    except Exception:
                        _LOG.warning(
                            "McpPool: failed to persist pending approval for "
                            "HTTP server %r (UI will fall back to stored hash)",
                            name,
                            exc_info=True,
                        )
                # Clean up the client
                client = self._mcp_http_clients.pop(name, None)
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.close()
            except Exception as exc:
                _LOG.warning(
                    "McpPool: HTTP server %r failed to start: %s", name, exc
                )
                if name in self._mcp_http_clients:
                    client = self._mcp_http_clients.pop(name)
                    with contextlib.suppress(Exception):
                        await client.close()

        # Build retrieval-tier MCP providers from the tool list
        await self._build_mcp_retrieval_providers()

    async def _connect_http(
        self,
        name: str,
        srv: McpServerConfig,
        approvals: dict[str, str],
    ) -> None:
        """Connect one HTTP MCP server, build ToolDefs, verify approval hash."""
        from disco.tools.mcp.approval import (
            ApprovalRequired,
            compute_description_hash,
        )
        from disco.tools.mcp.http import McpHttpClient
        from disco.tools.mcp.naming import qualified_name
        from disco.tools.mcp.pool import _UNTRUSTED_DESC_WRAPPER, _schema_to_args_model

        client = McpHttpClient(
            server=srv,
            secrets=self._secret_store,
            call_timeout_s=10.0,
        )

        # RP-05b §2: route the orchestrator-side MCP client's outbound httpx
        # through the egress allowlisting proxy when the build posture is
        # filtered, so a host outside the unioned allowlist is denied 403 — the
        # client must NOT bypass the sidecar. open posture → None (direct). See
        # docs/workorders/RP-05b-orchestrator-proxy-decision.md.
        try:
            await client.connect(proxy_env=self._mcp_proxy_env())
        except Exception:
            self._mcp_http_clients[name] = client  # register for close
            raise

        self._mcp_http_clients[name] = client

        # List tools and build ToolDefs
        raw_tools = await client.list_tools()

        # Compute the description hash and check approval
        tool_descs = [
            {"name": t.name, "description": t.description or ""}
            for t in raw_tools
        ]
        new_hash = compute_description_hash(tool_descs)

        stored = approvals.get(name)
        if stored is not None and stored != new_hash:
            raise ApprovalRequired(name, stored, new_hash)

        # Build ToolDefs
        allowed = set(srv.allowed_tools) if srv.allowed_tools is not None else None

        for tool in raw_tools:
            if allowed is not None and tool.name not in allowed:
                _LOG.debug("McpPool: HTTP tool %r not in allowlist for %r", tool.name, name)
                continue

            qname = qualified_name(name, tool.name)
            if qname in self._mcp_http_tools:
                _LOG.warning("McpPool: HTTP tool %r already registered — skipping", qname)
                continue

            fenced_desc = _UNTRUSTED_DESC_WRAPPER.format(
                name=name, desc=tool.description or "(no description)"
            )

            self._mcp_http_tools[qname] = ToolDef(
                name=qname,
                description=fenced_desc,
                args_model=_schema_to_args_model(tool),
                needs=frozenset(),
                base_risk=srv.risk_tier,
                runs_in="in_process",  # HTTP always runs in_process (orchestrator-side)
                read_only=False,
                uses_capabilities=frozenset(),
            )

        _LOG.info(
            "McpPool: HTTP server %r connected — %d tool(s) registered",
            name, len(raw_tools),
        )

    @property
    def _mcp_call_target(self) -> Any:
        """A callable that routes MCP tool invocations to the right transport.

        Stdio tools go through the pool; HTTP tools go through _mcp_http_clients.
        This is used by _MCPToolWrapper at invocation time.
        """
        pool = self._mcp_pool
        http_clients = self._mcp_http_clients

        class _MergedCallTarget:
            async def call_tool(self, server, tool, arguments):
                # Try HTTP first (faster path), then stdio
                if server in http_clients:
                    return await http_clients[server].call_tool(tool, arguments)
                if pool is not None:
                    return await pool.call_tool(server, tool, arguments)
                raise RuntimeError(
                    f"MCP server {server!r} is not connected"
                )

        return _MergedCallTarget()

    async def _build_mcp_retrieval_providers(self) -> None:
        """Build retrieval-tier MCP providers from registered MCP tools.

        Scans both the stdio pool snapshot and HTTP tools for search/fetch-shaped
        tools and wraps them as SearchProvider / ExtractionProvider Protocol
        instances. These are registered into the broker at compose time.
        """
        from disco.tools.mcp.retrieval_tier import build_retrieval_providers

        # Collect tools from both transports
        mcp_entries: list[dict[str, Any]] = []

        # Stdio tools from the pool
        if self._mcp_pool is not None and self._mcp_pool.started:
            for tdef in self._mcp_pool.snapshot():
                from disco.tools.mcp.naming import split_qualified_name
                parts = split_qualified_name(tdef.name)
                if parts is None:
                    continue
                server, tool_name = parts
                mcp_entries.append({
                    "server": server,
                    "tool_name": tool_name,
                    "tool": tdef,
                })

        # HTTP tools
        for qname, tdef in self._mcp_http_tools.items():
            from disco.tools.mcp.naming import split_qualified_name
            parts = split_qualified_name(qname)
            if parts is None:
                continue
            server, tool_name = parts
            mcp_entries.append({
                "server": server,
                "tool_name": tool_name,
                "tool": tdef,
            })

        if mcp_entries:
            self._mcp_retrieval_searches, self._mcp_retrieval_extractions = \
                build_retrieval_providers(
                    mcp_entries,
                    call_fn=self._mcp_call_target.call_tool,
                )
            _LOG.info(
                "MCP retrieval: built %d search + %d extraction provider(s)",
                len(self._mcp_retrieval_searches),
                len(self._mcp_retrieval_extractions),
            )
            # Drop any cached Build cap-handlers so the next build composes over
            # the freshly-built MCP providers (RP-05b §3).
            self._cap_handlers = None

    async def _close_mcp_pool(self) -> None:
        if self._mcp_pool is not None:
            await self._mcp_pool.aclose()
            self._mcp_pool = None
        for client in list(self._mcp_http_clients.values()):
            with contextlib.suppress(Exception):
                await client.close()
        self._mcp_http_clients.clear()
        self._mcp_http_tools.clear()
        self._mcp_retrieval_searches.clear()
        self._mcp_retrieval_extractions.clear()
        self._cap_handlers = None

    def mcp_approval_state(self) -> dict[str, dict]:
        """Return the pending approval state for WS frame dispatch (D3).

        Each key is a server name; value has 'old_hash' and 'new_hash'.
        """
        return dict(self._mcp_approval_pending)

    # ---- auto-suspend (lifecycle G): live only while a UI is watching ----------

    def on_connect(self, conversation_id: str) -> None:
        """A UI WebSocket connected — track it and cancel any pending idle-suspend
        (the user is back before the grace elapsed, or the WS reconnected)."""
        self._connections[conversation_id] = self._connections.get(conversation_id, 0) + 1
        task = self._suspend_tasks.pop(conversation_id, None)
        if task is not None:
            task.cancel()

    def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:
        """A UI WebSocket closed. When the LAST connection for a conversation goes,
        schedule an idle-suspend after `grace_s` — long enough that a brief blip (the
        WS-reconnect backoff) reconnects and cancels it before it fires."""
        n = self._connections.get(conversation_id, 0) - 1
        if n > 0:
            self._connections[conversation_id] = n
            return
        self._connections.pop(conversation_id, None)
        old = self._suspend_tasks.pop(conversation_id, None)
        if old is not None:
            old.cancel()
        self._suspend_tasks[conversation_id] = asyncio.create_task(
            self._suspend_after_grace(conversation_id, grace_s)
        )

    async def _suspend_after_grace(self, conversation_id: str, grace_s: float) -> None:
        try:
            await asyncio.sleep(grace_s)
        except asyncio.CancelledError:
            return
        if self._connections.get(conversation_id, 0) <= 0:
            await self._suspend(conversation_id)
        self._suspend_tasks.pop(conversation_id, None)

    async def _suspend(self, conversation_id: str) -> None:
        """Free an IDLE build's sandbox (its last UI closed): snapshot first, then
        tear down the container/port/memory/preview-server. Skips when there's no
        live sandbox, when storage isn't configured (no durable snapshot → keep the
        sandbox so nothing is lost), or when the loop is actively RUNNING (don't
        interrupt in-flight work — that run continues in the background). Resume (or
        the next message) re-creates the sandbox and rehydrates from the snapshot."""
        if conversation_id not in self._executors:
            return
        if not self._config_store.load().projects.projects_root.strip():
            return
        state = await self._store.get_state(conversation_id)
        if state.execution_status is ConversationStatus.RUNNING:
            return
        with contextlib.suppress(Exception):
            await self._maybe_snapshot(conversation_id)
            await self._teardown_sandbox(conversation_id)
            _LOG.info("auto-suspended idle conversation %s (no UI connected)", conversation_id)

    def sandbox_state(self, conversation_id: str) -> str | None:
        """Return 'active' when a live executor or pending session exists for
        conversation_id. Return 'suspended' when a snapshot record exists (the sandbox
        was torn down but is restorable). Return None when no sandbox context exists
        (research surface / no snapshot)."""
        if conversation_id in self._executors or conversation_id in self._pending_sessions:
            return "active"
        store = self._project_store_now()
        if store is None:
            return None
        try:
            record = store.get(conversation_id)
        except Exception:  # noqa: BLE001 — unreadable manifest: treat as absent
            return None
        return "suspended" if record is not None else None

    async def sweep_idle_once(self) -> int:
        """Single idle-TTL sweep pass: suspend all tracked sandboxes that are not
        RUNNING, have no live UI connections, and whose last event is older than
        PMX_IDLE_SUSPEND_S (default 1800 s). Returns the count suspended.

        Exposed so unit tests can drive it directly without sleeping."""
        ttl_env = disco_env("IDLE_SUSPEND_S")
        if ttl_env is not None:
            ttl_s = float(ttl_env)
        else:
            ttl_s = float(self._config_store.load().sandbox.idle_ttl_s)
        suspended = 0
        for cid in list(self._executors):
            with contextlib.suppress(Exception):
                state = await self._store.get_state(cid)
                if state.execution_status is ConversationStatus.RUNNING:
                    continue
                if self._connections.get(cid, 0) > 0:
                    continue
                # Use the last event's timestamp from the store — no parallel clock.
                events = await self._store.get_events(
                    cid,
                    EventFilter(after_seq=state.last_seq - 1) if state.last_seq > 0 else None,
                )
                if not events:
                    continue
                last_ts = events[-1].timestamp
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=UTC)
                idle_s = (datetime.now(tz=UTC) - last_ts).total_seconds()
                if idle_s < ttl_s:
                    continue
                await self._suspend(cid)
                _LOG.info("suspended idle sandbox cid=%s idle_s=%.0f", cid, idle_s)
                suspended += 1
        return suspended

    async def _idle_sweep_loop(self) -> None:
        """Background task: periodically sweep idle sandboxes. Created by the app
        lifespan alongside reconcile_orphaned_runs; cancelled cleanly on shutdown.

        Also reclaims the bundled audio-overview TTS model (RP-09): the in-process
        Kokoro engine stays resident after a synth, so this sweep unloads it once it
        has been idle past its TTL — freeing ~0.5 GB without the user toggling Audio
        off. Lazy-imported and suppressed so the optional `tts` extra need not be
        installed, and a sweep failure never disturbs the sandbox sweep."""
        while True:
            interval_s = float(disco_env("IDLE_SWEEP_INTERVAL_S", "60"))
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                return
            with contextlib.suppress(Exception):
                await self.sweep_idle_once()
            with contextlib.suppress(Exception):
                ttl_s = float(disco_env("TTS_IDLE_TTL_S", "1800"))
                from disco.agent_server import tts_local

                await tts_local.maybe_unload_if_idle(ttl_s=ttl_s)

    async def _maybe_run_deep_research(self, conversation_id: str) -> None:
        """The Deep Research driver. Inspects the conversation state to decide
        what to do this turn:
        - No PlanEvent yet + a USER message → decompose + emit synthetic
          PlanEvent + AWAITING_PLAN_APPROVAL. Wait for the user to approve.
        - PlanEvent exists + status is RUNNING with detail="plan_approved" +
          no ReportEvent yet → run the engine, emit progress events, emit
          ReportEvent + StatusEvent(FINISHED).
        - Anything else → no-op (waiting on the user, or already finished).

        All actions persist via the event store; the WS surface streams them.
        Failures surface as ErrorEvent on the log — never raise out of the
        background task."""
        events = await self._store.get_events(conversation_id)
        state = await self._store.get_state(conversation_id)
        plans = [e for e in events if isinstance(e, PlanEvent)]
        reports = [e for e in events if isinstance(e, ReportEvent)]

        # Phase 1: no plan yet → decompose + propose
        if not plans:
            await self._propose_deep_research_plan(conversation_id, events)
            return

        # Phase 2b: RESUME a stopped run (status PAUSED) → continue from the
        # checkpoint. The plan is already approved; flip back to RUNNING and execute,
        # carrying the partial ReportEvent's completed sections so the engine skips
        # the sub-questions that already finished (instead of redoing them). The new
        # full ReportEvent supersedes the partial one from the stop.
        if state.execution_status == ConversationStatus.PAUSED:
            partial = reports[-1] if reports else None
            await self._store.append(
                conversation_id,
                StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
            )
            await self._execute_deep_research(
                conversation_id, plans[-1], resume_from=partial
            )
            return

        # Phase 2: plan approved, no report yet → run the engine
        if (
            state.execution_status == ConversationStatus.RUNNING
            and not reports
        ):
            # Distinguish "RUNNING because plan was just approved" from "RUNNING
            # because we're already deep in the engine and the task re-fired."
            # The marker: the last StatusEvent's detail is "plan_approved".
            last_status = next(
                (e for e in reversed(events) if isinstance(e, StatusEvent)), None
            )
            if last_status is not None and last_status.detail == "plan_approved":
                await self._execute_deep_research(conversation_id, plans[-1])

        # Phase 3: FOLLOW-UP — a FINISHED report exists AND there's a new user
        # message since the last report. Run a follow-up synthesis that reuses
        # the existing report's passages as grounding (RP-13).
        elif reports and self._has_fresh_user_message(events, reports):
            await self._follow_up_deep_research(conversation_id, events, reports[-1])

    async def _propose_deep_research_plan(
        self, conversation_id: str, events: list
    ) -> None:
        """Decompose the latest user query into sub-questions and emit a
        synthetic PlanEvent + AWAITING_PLAN_APPROVAL. Same shape Build's plan
        gate uses — the UI reuses the existing approve_plan / request_plan
        affordances without modification."""
        # Find the most recent USER message — the query.
        query = next(
            (
                e.message.content
                for e in reversed(events)
                if isinstance(e, MessageEvent) and e.source == EventSource.USER
            ),
            None,
        )
        if not query or not query.strip():
            return  # nothing to plan; wait

        await self._store.append(
            conversation_id,
            StatusEvent(status=ConversationStatus.RUNNING),
        )
        tier = self._depth_for(conversation_id)
        from disco.retrieval.deep_research import bounds_for

        bound = bounds_for(tier)
        # Decompose via QUERY_REWRITER. The decompose call IS the planning
        # step; we emit the result as a PlanEvent directly (no LLM "planning
        # mode" loop needed — the engine owns the work).
        # Honor the model pill here too: the post-approval engine already passes
        # the override (see the DeepResearchRun compose below) — without it HERE,
        # a user's pick silently applied to gather/synthesis but NOT to the
        # decompose/plan step (the exact half-applied-pill bug).
        router = self._router_now(pick=self._model_override.get(conversation_id))
        try:
            subqs = await decompose_query(
                router, query, max_subq=bound.max_subquestions
            )
        except Exception as exc:  # noqa: BLE001 — surface as a system reminder
            await self._store.append(
                conversation_id,
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            f"Plan decomposition failed: {type(exc).__name__}: {exc}. "
                            "Try a more specific query.\n"
                            "</system-reminder>"
                        ),
                    ),
                ),
            )
            return

        steps = [PlanStep(title=s.title) for s in subqs]
        summary = (
            f"Multi-section research report on: {query.strip()[:140]}. "
            f"Will gather sources across {len(steps)} sub-questions "
            f"(tier: {tier.value}; cap: {bound.max_sources} sources, "
            f"{bound.max_rounds_per_subq} rounds/subq)."
        )
        plan = PlanEvent(
            summary=summary,
            steps=steps,
            revision=1,
            context=(
                f"**Query:** {query.strip()}\n\n"
                f"**Depth tier:** {tier.value}\n\n"
                f"**Sub-questions** (each becomes a section of the report):\n\n"
                + "\n".join(f"{i + 1}. {s.title}" for i, s in enumerate(subqs))
            ),
        )
        await self._store.append(conversation_id, plan)
        if self._effective_autonomous(conversation_id):
            # Headless/autonomous DR: no human to approve the plan. Auto-approve
            # inline (emit the same RUNNING/plan_approved StatusEvent approve_plan
            # would) and run the engine directly — otherwise the run stalls forever
            # at AWAITING_PLAN_APPROVAL. Mirrors the Build loop's autonomous
            # plan auto-approve (engine.py).
            await self._store.append(
                conversation_id,
                StatusEvent(
                    status=ConversationStatus.RUNNING, detail="plan_approved"
                ),
            )
            await self._execute_deep_research(conversation_id, plan)
            return
        await self._store.append(
            conversation_id,
            StatusEvent(
                status=ConversationStatus.AWAITING_PLAN_APPROVAL, detail=plan.id
            ),
        )

    async def _execute_deep_research(
        self,
        conversation_id: str,
        plan: PlanEvent,
        *,
        resume_from: ReportEvent | None = None,
    ) -> None:
        """The post-approval driver: run DeepResearchRun on the approved plan,
        emitting Action/Observation events for every retrieval round + section
        synthesis, ending with a ReportEvent + StatusEvent(FINISHED).

        `resume_from` is a prior stopped run's partial ReportEvent: its completed
        sections (+ their cited passages / discovered hits) are carried into the
        engine so resume continues from the checkpoint instead of redoing the
        sub-questions that already finished."""
        # Find the query from the user's last (pre-plan) message.
        events = await self._store.get_events(conversation_id)
        query = next(
            (
                e.message.content
                for e in events
                if isinstance(e, MessageEvent) and e.source == EventSource.USER
            ),
            plan.summary,
        )
        plan_steps = [s.title for s in plan.steps]
        tier = self._depth_for(conversation_id)

        # Build the engine. Providers come from the existing research-stream
        # plumbing (search, extract, reranker, embedder, nli); the vectorstore
        # is per-run (InMemoryVectorStore). The router honors model_override
        # from the leader pill (the existing _router_now path).
        from disco.retrieval import (
            DefaultRetrievalEngine,
            InMemoryVectorStore,
            RouterQueryRewriter,
        )

        deps = self._research()
        # RP-05b §3: deep research's discovery/extraction also flows MCP providers
        # through the SAME engine, so deep-research citations can come from the MCP
        # tier identically to bundled providers.
        search, extraction = self._compose_mcp_retrieval(deps)
        router = self._router_now(
            pick=self._model_override.get(conversation_id)
        )
        retrieval_engine = DefaultRetrievalEngine(
            search=search,
            extraction=extraction,
            reranker=deps["reranker"],
            embedder=deps.get("embedder"),
            rewriter=RouterQueryRewriter(router),
        )
        # RAM-aware peak-memory guard (OOM fix): bound how many gather legs run
        # their fetch/extract/embed/rerank body at once. Applies on EVERY tier —
        # not OOMing is correctness, not a free-tier perk. K is derived from
        # available RAM, so a big box runs every leg (effectively unbounded) and a
        # small box is protected. The encoder mode only TUNES the per-leg RAM
        # estimate (in-process FastEmbed legs are heavier than remote-encoder
        # ones); it does NOT gate the cap on/off.
        from disco.retrieval.deep_research.concurrency import gather_concurrency_for

        in_process_encoders = not self._config_store.load().encoders.remote
        gather_cap = gather_concurrency_for(
            in_process_encoders=in_process_encoders,
            n_subquestions=len(plan_steps),
            env=os.environ,
        )
        run = DeepResearchRun(
            query=query,
            router=router,
            retrieval_engine=retrieval_engine,
            embedder=deps.get("embedder"),
            vector_store=InMemoryVectorStore(),
            nli=deps["nli"],
            depth=tier,
            conversation_id=conversation_id,
            gather_concurrency=gather_cap,
        )

        # Emit callback: every engine event becomes an Action/Observation pair
        # on the conversation log so the UI's activity feed reflects progress.
        #
        # CORRELATION: gather runs sub-questions CONCURRENTLY, so "the last
        # ActionEvent appended" is usually some OTHER sub-question's search by the
        # time an observation arrives — which left 5 of 6 searches permanently
        # "running" in the UI (the observation pointed at the wrong action). The
        # engine's payloads already carry `subquestion` (+ `round` for search/
        # observation), so we correlate per sub-question here, in this run-scoped
        # map — no engine/protocol change.
        action_by_key: dict[str, str] = {}

        def _corr_key(payload: dict[str, Any]) -> str | None:
            subq = payload.get("subquestion")
            if subq is None:
                return None
            rnd = payload.get("round")
            # gap_reason has no round → correlate to the sub-question's latest search.
            return f"{subq}|{rnd}" if rnd is not None else f"{subq}|latest"

        async def emit(kind: str, payload: dict[str, Any]) -> None:
            # Treat phase + search + synthesize_section as actions (the agent
            # "doing something"), observation kinds as observations (results).
            if kind == "observation" or kind == "gap_reason":
                key = _corr_key(payload)
                action_id = action_by_key.get(key or "") or action_by_key.get(
                    f"{payload.get('subquestion')}|latest", ""
                )
                if not action_id:
                    # No subquestion in the payload (or pre-correlation emit) —
                    # fall back to the previous best-effort "last action".
                    last_action = next(
                        (
                            e
                            for e in reversed(
                                await self._store.get_events(conversation_id)
                            )
                            if isinstance(e, ActionEvent)
                        ),
                        None,
                    )
                    action_id = last_action.id if last_action else ""
                await self._store.append(
                    conversation_id,
                    ObservationEvent(
                        tool_result=ToolResult(
                            call_id=f"call_{kind}",
                            tool_name=kind,
                            success=bool(payload.get("ok", True)),
                            content=str(
                                {k: v for k, v in payload.items() if k != "ok"}
                            ),
                            structured=payload,
                        ),
                        action_id=action_id,
                    ),
                )
                return
            # action-shaped events
            event = ActionEvent(
                thought=f"Deep Research: {kind}",
                tool_call=ToolCall(tool_name=kind, arguments=payload),
            )
            await self._store.append(conversation_id, event)
            key = _corr_key(payload)
            if key is not None:
                action_by_key[key] = event.id
                action_by_key[f"{payload.get('subquestion')}|latest"] = event.id

        # Checkpointed resume: rebuild the retrieval-typed passages/hits from the
        # prior partial ReportEvent's plain dicts so the engine can carry its
        # completed sections forward (and skip those sub-questions).
        resume_sections = list(resume_from.sections) if resume_from else None
        resume_passages = None
        resume_all_hits = None
        if resume_from:
            from disco.retrieval.models import Passage, SearchHit

            resume_passages = [
                Passage.model_validate(p) for p in resume_from.passages
            ]
            resume_all_hits = [
                SearchHit.model_validate(h) for h in resume_from.all_hits
            ]

        # Fresh cancel flag for this execution; the engine polls it at each
        # sub-question/section boundary so Stop actually halts the run.
        flag = asyncio.Event()
        self._cancel_flags[conversation_id] = flag
        try:
            result = await run.run(
                plan_steps,
                emit=emit,
                should_cancel=flag.is_set,
                resume_sections=resume_sections,
                resume_passages=resume_passages,
                resume_all_hits=resume_all_hits,
            )
        except Exception as exc:  # noqa: BLE001 — surface as ErrorEvent
            from disco.core import ErrorEvent

            await self._store.append(
                conversation_id,
                ErrorEvent(
                    code="deep_research_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
            return
        finally:
            self._cancel_flags.pop(conversation_id, None)
            # Return the run's transient working set (fetch buffers + ONNX batch
            # temporaries, already freed by the time run.run() returned) back to
            # the OS, so a long-lived server doesn't accumulate a permanent RSS
            # floor from bursty DR runs. The retention half of the OOM fix.
            _release_process_memory()

        # Emit the partial-or-final ReportEvent. If the user pressed Stop, the run
        # halted at a checkpoint (bounded_by="stopped") with the partial report
        # preserved → emit PAUSED (resumable), NOT FINISHED.
        await self._store.append(conversation_id, result.to_event())
        stopped = getattr(result, "bounded_by", None) == "stopped"
        await self._store.append(
            conversation_id,
            StatusEvent(
                status=ConversationStatus.PAUSED if stopped else ConversationStatus.FINISHED,
                detail="stopped" if stopped else None,
            ),
        )

    def project_store(self) -> ProjectStore | None:
        """Public accessor for the live project store (used by the agent-server's
        projects endpoints). Returns None if no path is configured; the caller
        checks `.status()` for the full validation classification."""
        return self._project_store_now()

    # ---- share export (RP-06) ----------------------------------------------

    async def share_export(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> dict[str, Any]:
        """Produce a scrubbed, versioned JSON bundle from a conversation's full
        event log. The bundle is the canonical static-replay format (RP-00's
        cassette format, RP-06's static viewer) — the static viewer reads it
        directly with no WebSocket dependency, the harness re-runs against
        it as a deterministic event source. Both consumers get one projection
        of the same event log.

        Pure with respect to the event log: same log → same bundle bytes
        (modulo dict ordering, which we control via the dump mode). Scrubbed
        via `redaction.redact_event_payload` so credentials, tokens, and
        env-var dumps never leave the boundary.

        Returns a dict that `json.dumps` to a self-contained bundle:
          - `bundle_version`: int — the locked shape version. Bump on any
            backward-incompatible change to the bundle structure (viewers
            can refuse to render unknown versions cleanly).
          - `conversation_id`, `owner_id`, `exported_at`: provenance.
          - `surface`: the conversation's surface at export time.
          - `events`: list[dict] — the full scrubbed event log, ascending seq.
          - `state`: dict — the reconstructed final state (drives the
            viewer's "finished at …" header and the "any gates open" badge).

        The order of operations matters: events are serialized via
        `event.model_dump(mode="json")` (ISO datetimes, enums by value) and
        ONLY THEN scrubbed — scrubbing the Pydantic model directly would
        risk mutating non-string fields and corrupting the schema.
        """
        # Provenance: conversation + surface + state at export time. The
        # surface name is the "this is a build" / "this is a deep report"
        # banner the viewer renders, so it MUST be present and trustworthy
        # (no surface marker in the log → we recover via the runtime's
        # `_surface_of` ladder, same as a fresh loop composition).
        summaries = await self._store.list_conversation_summaries(
            owner_id=owner_id, limit=500, cursor=None
        )
        row = next((s for s in summaries if s.conversation_id == conversation_id), None)
        if row is None:
            return {
                "ok": False,
                "reason": "conversation_not_found",
            }
        events = await self._store.get_events(conversation_id)
        state = await self._store.get_state(conversation_id)

        # Scrub the events. The redactor walks every text field of every
        # event payload; non-text fields (seq, id, timestamps, booleans)
        # pass through unchanged. The result IS the bundle's event log.
        from .redaction import redact_event_payload

        scrubbed_events: list[dict[str, Any]] = []
        for ev in events:
            payload = ev.model_dump(mode="json")
            scrubbed_events.append(redact_event_payload(payload))

        # Build the bundle. `bundle_version` is the locked contract; bump
        # on any backward-incompatible change (removing a field, changing
        # the redaction label format, etc.) and document the change in
        # the order's report.
        # D10: `cassette` is the single-source service-call projection (the
        # same row format the harness `Cassette` class produces) derived from
        # the SAME scrubbed events the viewer renders. The harness replay
        # path reads `bundle["cassette"]` via `Cassette.from_rows(...)` —
        # one projection, two readers, no divergent serializer.
        from harness.projection import project_cassette_rows

        cassette_rows = project_cassette_rows(scrubbed_events)
        bundle = {
            "bundle_version": 1,
            "conversation_id": conversation_id,
            "owner_id": row.owner_id,
            "surface": row.surface or self._surface_of(conversation_id),
            "title": row.title,
            "exported_at": datetime.now(UTC).isoformat(),
            "last_seq": state.last_seq,
            "state": {
                "execution_status": state.execution_status.value,
                "iteration": state.iteration,
                "last_seq": state.last_seq,
                "pending_action_id": state.pending_action_id,
                "pending_plan_id": state.pending_plan_id,
            },
            "events": scrubbed_events,
            "cassette": cassette_rows,
        }
        return {"ok": True, "bundle": bundle}

    _IMPORT_MAX_EVENTS = 20_000

    async def share_import(
        self, bundle: Any, *, owner_id: str = DEFAULT_OWNER_ID
    ) -> dict[str, Any]:
        """Import an exported share bundle as a READ-ONLY local conversation (rp-06
        residue). A bundle is UNTRUSTED third-party data: fail-closed validation, an
        importer-minted cid (never trust the bundle's), re-scrub on ingest (exporter
        scrubbing is a claim, not a property — re-running the idempotent redactor
        protects this instance's future re-export), and an `origin="imported"` marker
        that the server edge enforces read-only against. Returns {ok, conversation_id}
        or {ok: False, reason} — the endpoint maps reason → 422. No partial imports."""
        import uuid as _uuid

        from disco.core import EventAdapter, migrate_event

        from .redaction import redact_event_payload

        if not isinstance(bundle, dict):
            return {"ok": False, "reason": "bundle_not_an_object"}
        if bundle.get("bundle_version") != 1:
            return {"ok": False, "reason": "unsupported_bundle_version"}
        raw_events = bundle.get("events")
        if not isinstance(raw_events, list) or not raw_events:
            return {"ok": False, "reason": "bundle_has_no_events"}
        if len(raw_events) > self._IMPORT_MAX_EVENTS:
            return {"ok": False, "reason": "bundle_too_large"}

        # Surface coerced into the known set (an attacker-set surface can't pick an
        # unhandled code path); title is text, truncated (React escapes it on render).
        surface = bundle.get("surface")
        if surface not in self._VALID_SURFACES:
            surface = "build"
        raw_title = bundle.get("title")
        title = (raw_title[:200] if isinstance(raw_title, str) else None) or "(imported)"

        # Validate + RE-SCRUB every event. Reject the WHOLE bundle on the first bad
        # event (no partial import). Event ids are preserved (action/observation
        # pairing is by id); seqs are reassigned by the store under the fresh cid.
        events: list[Event] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                return {"ok": False, "reason": "malformed_event"}
            try:
                migrated = migrate_event(raw)
                validated = EventAdapter.validate_python(migrated)  # extra="forbid"
                rescrubbed = redact_event_payload(validated.model_dump(mode="json"))
                events.append(EventAdapter.validate_python(migrate_event(rescrubbed)))
            except Exception:  # noqa: BLE001 — any validation failure rejects the bundle
                return {"ok": False, "reason": "malformed_event"}

        cid = f"conv_{_uuid.uuid4().hex}"  # importer-minted — never trust bundle.conversation_id
        self._store.create_conversation(
            cid, owner_id=owner_id, title=title, surface=surface, origin="imported"
        )
        await self._store.append_many(cid, events)
        return {"ok": True, "conversation_id": cid}

    @staticmethod
    def _has_fresh_user_message(
        events: list[Event], reports: list[ReportEvent]
    ) -> bool:
        """True when a USER message arrived AFTER the latest ReportEvent —
        a follow-up question the user asked on a finished report."""
        if not reports:
            return False
        last_report_seq = reports[-1].seq or 0
        for e in reversed(events):
            if (
                isinstance(e, MessageEvent)
                and e.source == EventSource.USER
                and (e.seq or 0) > last_report_seq
            ):
                return True
        return False

    async def _follow_up_deep_research(
        self,
        conversation_id: str,
        events: list[Event],
        prior_report: ReportEvent,
    ) -> None:
        """Run a follow-up synthesis on an existing deep-research report.

        Reuses the prior report's corpus (passages) as grounding context so
        the follow-up answer is source-backed. The user's follow-up question
        is the most recent USER message after the report. The answer is
        emitted as message events (agent response) on the conversation log,
        and a new lightweight ReportEvent captures the follow-up.

        This is the RP-13 report-follow-up path — same event-stream-append
        pattern as RP-08's scheduled-task re-injection."""
        # Find the follow-up question (most recent USER message since the report).
        last_report_seq = prior_report.seq or 0
        follow_up_query = next(
            (
                e.message.content
                for e in reversed(events)
                if isinstance(e, MessageEvent)
                and e.source == EventSource.USER
                and (e.seq or 0) > last_report_seq
            ),
            None,
        )
        if not follow_up_query or not follow_up_query.strip():
            return

        await self._store.append(
            conversation_id,
            StatusEvent(status=ConversationStatus.RUNNING, detail="follow_up"),
        )

        # Reuse the prior report's passages as the grounding corpus.
        passages = prior_report.passages or []
        if not passages:
            # No corpus to ground on — just answer directly.
            router = self._router_now()
            try:
                answer = await router.complete(
                    prompt=follow_up_query,
                    mode=OperatingMode.INTERACTIVE,
                    role=ModelRole.RAG_ANSWERER,
                )
                await self._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(role="assistant", content=answer.text),
                    ),
                )
            except Exception as exc:
                await self._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"Follow-up failed: {type(exc).__name__}: {exc}\n"
                                "</system-reminder>"
                            ),
                        ),
                    ),
                )
                await self._store.append(
                    conversation_id,
                    StatusEvent(status=ConversationStatus.ERROR),
                )
                return
        else:
            # Build a grounding block from the report's cited passages so the
            # answerer can cite them. Limit to a reasonable context window.
            MAX_PASSAGE_CHARS = 12_000
            passage_blocks: list[str] = []
            total = 0
            for p in passages:
                pid = str(p.get("id", ""))
                ptext = str(p.get("text", ""))
                src = str(p.get("source_title", p.get("source_url", "")))
                block = f"[{pid}] ({src})\n{ptext}\n"
                if total + len(block) > MAX_PASSAGE_CHARS:
                    break
                passage_blocks.append(block)
                total += len(block)

            grounding = "\n---\n".join(passage_blocks)
            prompt = (
                f"You are answering a follow-up question about a research report. "
                f"The original query was: {prior_report.query}\n\n"
                f"The report summary: {prior_report.summary}\n\n"
                f"Below are the source passages the report was grounded on. "
                f"Use them to answer the follow-up question. Cite sources "
                f"with [[passage_id]] markers.\n\n"
                f"--- SOURCE PASSAGES ---\n{grounding}\n"
                f"--- END SOURCES ---\n\n"
                f"Follow-up question: {follow_up_query}"
            )

            router = self._router_now()
            try:
                answer = await router.complete(
                    prompt=prompt,
                    mode=OperatingMode.INTERACTIVE,
                    role=ModelRole.RAG_ANSWERER,
                )
                await self._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(role="assistant", content=answer.text),
                    ),
                )
            except Exception as exc:
                await self._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"Follow-up failed: {type(exc).__name__}: {exc}\n"
                                "</system-reminder>"
                            ),
                        ),
                    ),
                )
                await self._store.append(
                    conversation_id,
                    StatusEvent(status=ConversationStatus.ERROR),
                )
                return

        await self._store.append(
            conversation_id,
            StatusEvent(status=ConversationStatus.FINISHED, detail="follow_up_complete"),
        )

    async def export_report(
        self,
        conversation_id: str,
        fmt: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> tuple[bytes, str, str] | None:
        """Export the latest ReportEvent from a conversation as MD, PDF, or DOCX.

        Returns (payload_bytes, media_type, filename_extension) on success,
        or None when no ReportEvent exists for this conversation (the caller
        maps None → 404). Raises ValueError for unknown `fmt` (the caller
        maps ValueError → 400).

        The endpoint is generic over ReportEvent — today only deep_research
        conversations emit one; standard research and build do not."""
        from .report_export import export_report as _export

        events = await self._store.get_events(conversation_id)
        reports = [e for e in events if isinstance(e, ReportEvent)]
        if not reports:
            return None
        # The latest report (deep research emits only one; safe for future
        # multi-report conversations).
        report = reports[-1]
        if fmt == "docx":
            # DOCX renders via pandoc INSIDE a transient sandbox (pandoc ships in
            # the sandbox image, not the host) — jailed like marp, no host install.
            from .report_export import EXTENSIONS, MEDIA_TYPES

            payload = await self._render_docx_in_sandbox(report, owner_id=owner_id)
            return payload, MEDIA_TYPES["docx"], EXTENSIONS["docx"]
        payload, media_type, ext = _export(report, fmt)  # md, pdf — in-process
        return payload, media_type, ext

    async def _render_docx_in_sandbox(self, report: Any, *, owner_id: str) -> bytes:
        """Spin a throwaway render sandbox, render the report to .docx via pandoc
        in-box, read the bytes, destroy the sandbox. The export endpoint isn't tied
        to a live conversation sandbox, so it gets its own transient one."""
        import uuid as _uuid

        from .report_export import serialize_docx

        svc = self._sandbox_service_now()
        cid = f"export-docx-{_uuid.uuid4().hex[:12]}"
        instance = await svc.create(
            self._sandbox_spec, owner_id=owner_id, conversation_id=cid
        )
        try:
            return await serialize_docx(report, instance)
        finally:
            try:
                await instance.destroy()
            except Exception:  # noqa: BLE001 — teardown best-effort
                _LOG.exception("export render sandbox teardown failed (cid=%s)", cid)

    def create_share_link(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> dict[str, Any]:
        """DEPRECATED: synchronous scaffold kept off the hot path. The
        production issuer is `create_share_link_async` (it reads the live
        last_seq so the share_tokens row carries an accurate seq hint)."""
        import secrets

        token = secrets.token_urlsafe(16).replace("-", "a").replace("_", "b")[:22]
        return {"ok": True, "token": token, "owner_id": owner_id}

    async def create_share_link_async(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> dict[str, Any]:
        """Issue a revocable base62 token pointing at a conversation. The
        token is the URL slug for `/share/<token>`. The `share_tokens`
        table gives us:
          - cheap O(1) lookup on every viewer request
          - per-owner scoping (a token can only be revoked by its issuer)
          - revocation by `revoked_at` (a revoked row is invisible to lookups)

        16 random bytes → 22 base62 chars (62^22 ≈ 2^131) — enough to make
        enumeration infeasible; small enough to fit in a URL slug.
        Double-issuance is a no-op (the token PK is the random string and
        `INSERT OR IGNORE` is the cheap defense against a double-clicked
        "Share" button)."""
        import secrets

        # Confirm the conversation exists for this owner (and avoid
        # silently issuing tokens for unknown ids).
        summaries = await self._store.list_conversation_summaries(
            owner_id=owner_id, limit=500, cursor=None
        )
        row = next((s for s in summaries if s.conversation_id == conversation_id), None)
        if row is None:
            return {"ok": False, "reason": "conversation_not_found"}

        # Read the live last_seq so the share_tokens row can carry the
        # "bundle_seq" hint (re-exports reuse it; the UI surfaces a
        # "newer events available" badge when the live last_seq exceeds
        # the recorded one).
        state = await self._store.get_state(conversation_id)
        bundle_seq = state.last_seq

        token = secrets.token_urlsafe(16).replace("-", "a").replace("_", "b")[:22]
        self._store.create_share_token(
            token,
            conversation_id,
            owner_id,
            bundle_seq=bundle_seq,
        )
        return {
            "ok": True,
            "token": token,
            "conversation_id": conversation_id,
            "owner_id": owner_id,
            "bundle_seq": bundle_seq,
        }

    def lookup_share_link(self, token: str) -> dict | None:
        """Resolve a share token to its (conversation_id, owner_id) row.
        Returns None for missing OR revoked tokens — the two cases are
        intentionally conflated so a revoked link is indistinguishable
        from a never-issued one to a probe. The `share_tokens` table
        has the same idempotent semantics on `INSERT OR IGNORE` so a
        double-clicked "Share" never writes twice."""
        return self._store.lookup_share_token(token)

    def list_share_links(self, *, owner_id: str) -> list[dict]:
        """List the active (non-revoked) share links for one owner. The
        UI's "shared links" affordance consumes this; revoked links are
        filtered out — the user sees a clean active-only list."""
        return self._store.list_share_tokens(owner_id=owner_id)

    def revoke_share_link(self, token: str, *, owner_id: str) -> bool:
        """Revoke a share link. OWNER-SCOPED — only the issuer can revoke
        (the WHERE clause filters by both token AND owner_id). Returns
        True if a row was marked revoked, False otherwise. Idempotent: a
        second revoke call returns False (no row matched the
        `revoked_at IS NULL` clause)."""
        return self._store.revoke_share_token(token, owner_id=owner_id)

    def _project_store_now(self) -> ProjectStore | None:
        """Build a ProjectStore from the current settings — None if no path is
        configured. Built per-call (cheap; matches the rest of the runtime's
        "reload the config each request" discipline). The validity status is
        checked at the use site so a bad-but-set path can be reported clearly."""
        root = self._config_store.load().projects.projects_root
        if not root.strip():
            return None
        return ProjectStore(root)

    async def _maybe_rehydrate(self, conversation_id: str) -> None:
        """Restore the workspace files from a prior snapshot into the live
        sandbox, if a snapshot exists. Idempotent: tracked via an in-process
        flag so a second kick on the same conversation doesn't re-write the
        files."""
        if getattr(self, "_rehydrated", None) is None:
            self._rehydrated: set[str] = set()
        if conversation_id in self._rehydrated:
            return
        self._rehydrated.add(conversation_id)
        store = self._project_store_now()
        if store is None or store.status() != StorageStatus.OK:
            return
        record = None
        try:
            record = store.get(conversation_id)
        except Exception:  # noqa: BLE001 — manifest unreadable: treat as no record
            return
        if record is None or record.files_missing:
            return
        executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return
        try:
            await rehydrate_workspace(session, store.path_for(conversation_id))
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
            await self._emit_persistence_reminder(
                conversation_id,
                f"Could not restore project files: {exc}",
            )

    async def _rehydrate_after_recreate(self, conversation_id: str) -> None:
        """Mid-run recreate (transport drop / OOM-killed box): SandboxSession
        replaced a dead instance with a FRESH one whose workspace is EMPTY — the
        conv_f3bdc842 production incident ("all files were lost"). Clear the
        idempotency flag and rehydrate so the agent's retry lands on its files,
        not a bare dir. _maybe_rehydrate writes through the executor's session,
        which already points at the new instance — no extra plumbing needed.
        Best-effort: with no snapshot yet (first run), there is nothing to
        restore and the agent rebuilds, exactly as before."""
        rehydrated = getattr(self, "_rehydrated", None)
        if rehydrated is not None:
            rehydrated.discard(conversation_id)
        await self._maybe_rehydrate(conversation_id)
        # DC-07: also re-materialize uploaded files.
        await self._rematerialize_uploads(conversation_id)

    async def _rematerialize_uploads(self, conversation_id: str) -> None:
        """[DC-07] Copy server-held uploads back into the fresh sandbox."""
        if not self._uploads_base:
            return

        # We need the session to write files. The executor's session if a loop
        # exists; otherwise the pending session if it's a pre-kick recreation.
        executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            session = self._pending_sessions.get(conversation_id)

        if session is None:
            return

        uploads_dir = Path(self._uploads_base) / conversation_id
        if not uploads_dir.is_dir():
            return

        # Write them back into the sandbox.
        written = 0
        for p in uploads_dir.iterdir():
            if p.is_file():
                try:
                    data = p.read_bytes()
                    await session.write_file(f"uploads/{p.name}", data)
                    written += 1
                except Exception:  # noqa: BLE001 — best effort
                    _LOG.warning(
                        "[dc-07] failed to re-materialize upload %r for %s",
                        p.name, conversation_id,
                    )
        if written:
            _LOG.info(
                "[dc-07] re-materialized %d upload(s) for %s",
                written, conversation_id,
            )

    async def _maybe_snapshot(self, conversation_id: str) -> None:
        """Mirror the live workspace out to disk + update the manifest."""
        store = self._project_store_now()
        if store is None:
            return
        status = store.status()
        if status != StorageStatus.OK:
            await self._emit_persistence_reminder(
                conversation_id,
                f"project storage is {status.value}; this build was NOT saved.",
            )
            return
        executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return
        # Title pulled from the conversations table; created_at is the row's
        # creation timestamp. Both are cheap reads we surface in the list view.
        title: str | None = None
        created_at: str | None = None
        owner_id: str | None = None
        try:
            summaries = await self._store.list_conversation_summaries(
                owner_id=DEFAULT_OWNER_ID, limit=500, cursor=None
            )
            row = next((s for s in summaries if s.conversation_id == conversation_id), None)
            if row is not None:
                title = row.title
                created_at = row.created_at
                owner_id = row.owner_id
        except Exception:  # noqa: BLE001 — metadata is best-effort
            pass
        try:
            result = await snapshot_workspace(session, store.path_for(conversation_id))
            store.write_manifest(
                conversation_id,
                title=title,
                owner_id=owner_id,
                created_at=created_at,
                file_count=result.file_count,
                total_bytes=result.total_bytes,
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            await self._emit_persistence_reminder(
                conversation_id,
                f"snapshot failed: {exc}",
            )

    async def _emit_persistence_reminder(self, conversation_id: str, body: str) -> None:
        """Surface a project-persistence problem on the event log as an implicit
        system-reminder — same pattern as the loop's other gates, so the model
        and the UI both see what went wrong, named."""
        await self._store.append(
            conversation_id,
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        f"Project persistence note: {body}\n"
                        "</system-reminder>"
                    ),
                ),
            ),
        )

    def resolve_cid_prefix(self, cid8: str) -> str | None:
        """Full conversation id whose uuid part starts with cid8 — live executors only
        (a preview without a live sandbox is a 503 anyway). Ambiguous (>1) → None."""
        matches = [
            cid
            for cid in self._executors.keys()
            if cid.removeprefix("conv_").startswith(cid8)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def preview_upstream(self, conversation_id: str) -> str | None:
        """The URL the AGENT-SERVER can reach the conversation's dev server at (the backend
        owns how — localhost for local, the remote host's tailnet IP for gVisor). The
        browser never touches this; the agent-server proxies it (single origin)."""
        return self.port_upstream(conversation_id, PREVIEW_PORT)

    def port_upstream(self, conversation_id: str, port: int) -> str | None:
        """Generalized upstream resolution for any curated USER port (BP-10).
        expose_port itself refuses non-USER ports — defense stays in the backend."""
        executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return None
        if getattr(getattr(session, "_service", None), "name", "?") == "podman":
            return None  # stub here
        return session.expose_port(port)

    async def wake_for_preview(self, cid8: str, port: int) -> str | None:
        """Wake a suspended sandbox if a preview request hits it.
        Restores the workspace and the built-in static preview server on 8000.
        It does NOT restart agent-started dev servers (vite/express) — requests
        for ports nothing listens on after wake will proxy to a 502.
        """
        cid = self.resolve_cid_prefix(cid8)
        if cid is not None:
            return self.port_upstream(cid, port)

        try:
            summaries = await self._store.list_conversation_summaries(
                owner_id=DEFAULT_OWNER_ID, limit=500, cursor=None
            )
            matches = [
                s.conversation_id
                for s in summaries
                if s.conversation_id.removeprefix("conv_").startswith(cid8)
            ]
            if len(matches) != 1:
                return None
            cid = matches[0]
        except Exception:
            return None

        lock = self._wake_locks.setdefault(cid, asyncio.Lock())
        async with lock:
            if cid in self._executors:
                return self.port_upstream(cid, port)
            woke = await self.ensure_preview(cid)
            if woke:
                return self.port_upstream(cid, port)
            return None

    def live_session(self, conversation_id: str) -> SandboxSession | None:
        """Read-only sandbox accessor (BP-14). Does NOT create a session — a GET must
        have no creation side-effects. Returns None when no executor/sandbox exists."""
        executor = self._executors.get(conversation_id)
        return getattr(executor, "_sandbox", None) if executor is not None else None

    def sandbox_backend_name(self) -> str | None:
        """Name of the active sandbox backend ('gvisor', 'podman', 'local', 'process').
        Returns None on any lookup failure."""
        try:
            return self._sandbox_service_now().name
        except Exception:  # noqa: BLE001
            return None

    _SESSIONS_LIST_RETRIES: int = 2
    _SESSIONS_LIST_BACKOFF_S: float = 0.25

    async def sessions_snapshot(self, conversation_id: str) -> tuple[list[SessionInfo], bool]:
        """Session list + staleness. Fresh on success (cache updated); on
        transport failure retry twice (0.25 s apart), then degrade to the
        last-known list marked stale=True — a read-only listing must never
        500 the UI poll loop (DEFECT-1). No sandbox -> ([], False)."""
        session = self.live_session(conversation_id)
        if session is None:
            return ([], False)
        last_exc: BaseException | None = None
        for attempt in range(self._SESSIONS_LIST_RETRIES + 1):
            if attempt > 0:
                await asyncio.sleep(self._SESSIONS_LIST_BACKOFF_S)
            try:
                all_sessions = await session.sessions.list()
                filtered = [s for s in all_sessions if not s.name.startswith("__")]
                self._last_sessions[conversation_id] = filtered
                return (filtered, False)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        _LOG.warning(
            "sessions_snapshot: %s failed after %d attempts: %s",
            conversation_id,
            self._SESSIONS_LIST_RETRIES + 1,
            last_exc,
        )
        return (self._last_sessions.get(conversation_id, []), True)

    async def sessions_list(self, conversation_id: str) -> list[SessionInfo]:
        """Compat wrapper — returns only the list, degraded on failure (DEFECT-1)."""
        return (await self.sessions_snapshot(conversation_id))[0]

    _SESSION_VIEW_CACHE_TTL: float = 0.5
    _SESSION_VIEW_MAX_CHARS: int = 100_000

    async def session_view(
        self, conversation_id: str, name: str, tail_chars: int
    ) -> SessionView | None:
        """Coalesced capture-pane: at most one in-flight call per (cid, name),
        result cached 0.5s so concurrent polls share one exec_shell round-trip."""
        session = self.live_session(conversation_id)
        if session is None:
            return None
        key = (conversation_id, name)
        lock = self._session_view_locks.setdefault(key, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            cached = self._session_view_cache.get(key)
            if cached is not None and (now - cached[0]) < self._SESSION_VIEW_CACHE_TTL:
                view = cached[1]
            else:
                view = await session.sessions.view(name, tail_chars=self._SESSION_VIEW_MAX_CHARS)
                self._session_view_cache[key] = (now, view)
        if len(view.output) > tail_chars:
            return SessionView(running=view.running, output=view.output[-tail_chars:])
        return view

    async def preview(self, conversation_id: str) -> dict[str, Any]:
        """Backend-aware live preview availability. The browser iframes the agent-server's
        proxy (/conversations/{id}/preview-app/), which forwards to the active backend's
        dev server — so previews work over the tailnet via the one reachable origin, with no
        random container ports exposed. Podman is an honest labeled stub; never a fake URL."""
        executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return {"available": False, "reason": "The agent hasn't started a sandbox yet."}
        if getattr(getattr(session, "_service", None), "name", "?") == "podman":
            return {
                "available": False,
                "stub": True,
                "reason": "Preview isn't wired for the Podman backend in this environment "
                "— it's completed at deployment.",
            }
        # Passive probe ONLY: this GET is polled by the UI, and a read path must not
        # create a sandbox (that's _ensure()'s side effect) or surface its failures
        # as a 500. No live instance → no preview, plainly stated.
        inst = getattr(session, "_instance", None)
        if inst is None:
            return {
                "available": False,
                "reason": "The agent's sandbox isn't running yet.",
                "owner": None,
            }
        try:
            owners = await port_owners(inst, sorted(USER_PORTS))
        except Exception:  # noqa: BLE001 — a probe must never 500 the preview endpoint
            owners = {}
        owner = owners.get(PREVIEW_PORT)

        ns = f"pmx-{session.sessions.namespace}"

        def _owner_json(o):  # bound ports only; normalized session name
            sess = o.session
            if sess and sess.startswith(ns):
                sess = sess[len(ns):]
            return {"pid": o.pid, "cmdline": o.cmdline, "session": sess}

        ports_payload = [
            {"port": p, "owner": _owner_json(o)}
            for p, o in sorted(owners.items())
            if o is not None and o.pid is not None
        ]

        if owner is None or owner.pid is None:
            return {
                "available": False,
                "reason": f"No dev server detected. Run one on port {PREVIEW_PORT} inside the "
                "sandbox to see a live preview.",
                "owner": None,
                "ports": ports_payload,
            }

        return {
            "available": True,
            "proxy": True,
            "owner": _owner_json(owner),
            "ports": ports_payload,
        }

    async def ensure_preview(self, conversation_id: str) -> bool:
        """Backend half of the UI 'Restart preview' button (§E7). Bounded + safe: the
        same idempotent restart as SandboxSession.ensure_preview.

        After a clean FINISH the sandbox is torn down (the G safe-leak fix in `_run`),
        which would make this button a dead affordance. Instead, re-materialize through
        the documented resume path: re-compose the loop/executor (lazy sandbox),
        rehydrate the snapshot, then start the preview — the user explicitly asked to
        see the artifact again, and that's exactly what the snapshot is for."""
        executor = self._executors.get(conversation_id)
        if executor is None:
            store = self._project_store_now()
            if store is None or store.status() != StorageStatus.OK:
                return False
            try:
                record = store.get(conversation_id)
            except Exception:  # noqa: BLE001 — manifest unreadable: nothing to revive
                return False
            if record is None or record.files_missing:
                return False  # never had a build snapshot (e.g. research) — no preview
            self._loop_for(conversation_id)  # registers a fresh executor + lazy sandbox
            await self._maybe_rehydrate(conversation_id)
            executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return False
        try:
            return await session.ensure_preview()
        except Exception:  # noqa: BLE001
            return False

    # ---- control ops: the confirmation gate + kill switch (BoD §13.4/§13.6) -----

    async def confirm(self, conversation_id: str) -> None:
        """Approve the pending action: execute EXACTLY it (the loop's `confirm`), then
        resume the loop for the next steps.

        Lazy-composes (`_loop_for`) like `request_plan` below: after a server restart
        `_loops` is empty, and the old `.get()` guard SILENTLY dropped the frame — the
        user clicked Approve and nothing happened (state is event-sourced, so the
        recomposed loop sees the same pending gate)."""
        loop = self._loop_for(conversation_id)
        await loop.confirm()
        self.kick(conversation_id)  # continue plan→act→observe past the gate

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        """Deny the pending action: record the denial (no execution), then resume.
        Lazy-composes — see `confirm` (the post-restart silent-drop hole)."""
        loop = self._loop_for(conversation_id)
        await loop.reject(reason)
        self.kick(conversation_id)

    async def approve_plan(self, conversation_id: str) -> None:
        """Approve the pending plan: flip the loop into execution mode (full tools)
        and run it. The per-action BlastRadiusConfirm gate still governs the build.
        Lazy-composes — see `confirm` (the post-restart silent-drop hole)."""
        loop = self._loop_for(conversation_id)
        await loop.approve_plan()
        self.kick(conversation_id)  # start building the approved plan

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        """(Re-)enter plan mode with the user's instruction — the first plan AND the
        re-plan after a build, so focused diff-style changes are planned and re-approved
        instead of free-form steered.

        Composes the loop lazily (`_loop_for`, same as `kick`) — a fresh
        conversation or one whose loop died with a server restart has no entry in
        `_loops`, and the old `.get()` guard silently dropped the frame: the
        composer showed "sending…" forever while the server did nothing."""
        loop = self._loop_for(conversation_id)
        await loop.enter_planning(text)
        self.kick(conversation_id)  # produce the (revised) plan

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        """Resume from AWAITING_USER_DECISION by selecting the agent's proposed
        alternative path. The loop synthesizes an ActionEvent from the option's
        ToolCall and executes it directly, then resumes.
        Lazy-composes — see `confirm` (the post-restart silent-drop hole)."""
        loop = self._loop_for(conversation_id)
        await loop.pick_alternative(option_id)

    async def cancel(self, conversation_id: str) -> None:
        """Cooperative stop (distinct from the hard kill): the loop winds down. For
        Deep Research (engine, not an AgentLoop) this ALSO sets the cancel flag the
        engine polls — without it, Stop was a no-op (the engine ran to completion)."""
        self._cancel_flags.setdefault(conversation_id, asyncio.Event()).set()
        loop = self._loops.get(conversation_id)
        if loop is not None:
            await loop.cancel()

    async def resume(self, conversation_id: str) -> None:
        """Continue a stopped (PAUSED) Deep Research run. Clears the cancel flag and
        re-kicks; `_maybe_run_deep_research` detects the PAUSED state and re-runs the
        execution to completion (the new full report supersedes the partial)."""
        self._cancel_flags.pop(conversation_id, None)
        self.kick(conversation_id)

    def _condense_trailing_degeneracy(
        self, events: list
    ) -> CondensationEvent | None:
        """Pure-on-the-event-list detector for trailing degenerate segments.
        No model call (call-site: resume_conversation).
        
        DC-05c Design:
        1. Detection: scan tail backwards to first real Action (non-bookkeeping)
           or USER Message. Count agent messages, duplicate knowledge, plan revisions.
        2. Degenerate iff: segment >= 6 AND zero real actions AND (>=3 agent messages
           OR any knowledge fact >= 3 times).
        3. Pinning: the FIRST instance of any duplicated knowledge fact stays
           OUTSIDE the span.
        """
        # Scan backwards to define the segment boundary
        # "stopping at the first real ActionEvent (non-bookkeeping tool) or USER MessageEvent"
        segment_events = []
        for e in reversed(events):
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                break
            if isinstance(e, ActionEvent):
                # A real (non-bookkeeping) action ends the degenerate segment.
                # Same set the engine's noop counter exempts — single source
                # of truth, NOT a local copy.
                if e.tool_call.tool_name not in _BOOKKEEPING_TOOLS:
                    break
            segment_events.append(e)

        if len(segment_events) < 6:
            return None

        segment_events.reverse()

        # count degeneracy signals
        agent_prose_count = 0
        knowledge_counts: dict[tuple[str, str], int] = {}  # (scope, hash) -> count
        plan_revision_count = 0

        for e in segment_events:
            if isinstance(e, MessageEvent) and e.message.role == "assistant":
                agent_prose_count += 1
            elif isinstance(e, KnowledgeEvent):
                snippet_hash = hashlib.sha256(e.snippet.strip().encode()).hexdigest()
                key = (e.scope, snippet_hash)
                knowledge_counts[key] = knowledge_counts.get(key, 0) + 1
            elif isinstance(e, PlanEvent):
                plan_revision_count += 1

        is_degenerate = agent_prose_count >= 3 or any(
            count >= 3 for count in knowledge_counts.values()
        )

        if not is_degenerate:
            return None

        # Pinning exemption: FIRST instance stays OUTSIDE.
        # Find all knowledge facts that appeared BEFORE this segment.
        seen_knowledge: set[tuple[str, str]] = set()
        first_event_seq = segment_events[0].seq
        for e in events:
            if e.seq == first_event_seq:
                break
            if isinstance(e, KnowledgeEvent):
                h = hashlib.sha256(e.snippet.strip().encode()).hexdigest()
                seen_knowledge.add((e.scope, h))

        # Move the span start forward past any "first instances" of knowledge in the segment.
        # We only need to clear the start to respect "stays OUTSIDE" for the first half.
        # Interleaved first-instances in the middle are protected by View.of pinning.
        span_start_idx = 0
        while span_start_idx < len(segment_events):
            e = segment_events[span_start_idx]
            if isinstance(e, KnowledgeEvent):
                h = hashlib.sha256(e.snippet.strip().encode()).hexdigest()
                key = (e.scope, h)
                if key not in seen_knowledge:
                    seen_knowledge.add(key)
                    span_start_idx += 1
                    continue
            break

        final_span = segment_events[span_start_idx:]
        if not final_span:
            return None

        start_seq = final_span[0].seq
        end_seq = final_span[-1].seq

        if start_seq is None or end_seq is None:
            return None

        # Summary counts (for the tombstone text)
        m_dupes = sum(1 for e in final_span if isinstance(e, KnowledgeEvent))
        k_plans = sum(1 for e in final_span if isinstance(e, PlanEvent))

        summary = (
            f"[Condensed {len(final_span)} degenerate turns: the agent repeated itself "
            f"without calling any tools ({m_dupes} duplicate knowledge entries, "
            f"{k_plans} plan revisions). No work was performed in this span. "
            "Do not imitate this pattern — proceed by calling tools.]"
        )

        return CondensationEvent(
            forgotten_start_seq=start_seq,
            forgotten_end_seq=end_seq,
            summary=summary,
            summary_role="user",
            reason="hard_reset",
        )

    async def _reconstruct_resume_context(
        self, conversation_id: str, events: list
    ) -> list:
        """Build the events to append before a resume status flip (DC-05b / DEFECT-4).

        Returns a list that may contain:
          - 0 or more synthetic ObservationEvents for dangling (unresolved) actions
          - exactly 1 ENVIRONMENT MessageEvent: sandbox reality + file list + sessions
            + the first undone plan step as the next actionable instruction

        Receives the event list explicitly so it is unit-testable against an archived
        log without a live runtime — do NOT load events from the store here.
        """
        result: list = []

        # 1. Synthesize terminal observations for dangling actions.
        # An action is "dangling" if no ObservationEvent or AgentErrorEvent references
        # its id — the server was killed while the tool was in-flight.
        resolved: set[str] = set()
        for e in events:
            if isinstance(e, ObservationEvent) and e.action_id:
                resolved.add(e.action_id)
            elif isinstance(e, AgentErrorEvent) and e.action_id:
                resolved.add(e.action_id)

        for e in events:
            if isinstance(e, ActionEvent) and e.id not in resolved:
                result.append(
                    ObservationEvent(
                        source=EventSource.ENVIRONMENT,
                        action_id=e.id,
                        tool_result=ToolResult(
                            call_id=e.tool_call.call_id,
                            tool_name=e.tool_call.tool_name,
                            success=False,
                            content=(
                                "<system-reminder>This action was interrupted by a server"
                                " restart — its outcome is UNKNOWN. Re-verify its effect"
                                " before assuming it completed.</system-reminder>"
                            ),
                        ),
                    )
                )

        # 2. Environment reality block.
        # Starts with "Resumed by user." so existing checks that test for that
        # literal substring continue to pass.
        parts: list[str] = [
            "Resumed by user. Current environment reality after interruption:"
        ]

        # The sandbox sentence must match reality: a PAUSED landed by an in-loop
        # valve (dc-05a actionless/noop breakers) leaves the executor — and its
        # sandbox — alive; only restart/suspend paths reclaim it. Lying about a
        # reclaim would push the model into pointless re-verification.
        if conversation_id in self._executors:
            parts.append(
                "- Your sandbox is still running — existing workspace files and"
                " shell sessions are intact."
            )
        else:
            parts.append(
                "- The previous sandbox was reclaimed. A fresh sandbox is created on"
                " your next action and your saved workspace files are restored into"
                " it automatically."
            )

        # Workspace file listing from the project-store snapshot (up to 30 paths).
        store = self._project_store_now()
        file_paths: list[str] = []
        if store is not None and store.status() == StorageStatus.OK:
            try:
                workspace = store.path_for(conversation_id)
                raw_paths = list(store.iter_workspace(conversation_id))[:30]
                file_paths = [str(p.relative_to(workspace)) for p in raw_paths]
            except Exception:  # noqa: BLE001 — missing/corrupt store: fall through to empty
                pass

        if file_paths:
            listing = "\n  ".join(file_paths)
            parts.append(f"- Files that will be restored:\n  {listing}")
        else:
            parts.append("- No saved files — the workspace starts empty.")

        # DC-07: List uploads held server-side.
        upload_names = sorted(list(self.get_upload_names(conversation_id)))
        if upload_names:
            # If the sandbox is dead, they are "lost-and-recoverable" until the 
            # next action triggers recreation + re-materialization.
            listing = "\n  ".join(f"uploads/{n}" for n in upload_names[:30])
            status = (
                "intact"
                if conversation_id in self._executors
                else "held server-side and will be restored"
            )
            parts.append(f"- Uploaded files ({status}):\n  {listing}")

        # Session list (degrades gracefully — sessions_snapshot never raises).
        sessions, _ = await self.sessions_snapshot(conversation_id)
        if sessions:
            names = ", ".join(s.name for s in sessions)
            parts.append(f"- Shell sessions: {names}")
        else:
            parts.append("- No shell sessions are running.")

        # 3. Plan restatement: find the latest plan and the first step not yet done.
        latest_plan: PlanEvent | None = None
        for e in events:
            if isinstance(e, PlanEvent):
                if latest_plan is None or e.revision >= latest_plan.revision:
                    latest_plan = e

        if latest_plan is not None and latest_plan.steps:
            plan_seq = latest_plan.seq or 0
            done_steps: set[int] = set()
            for e in events:
                if not isinstance(e, ActionEvent) or e.tool_call is None:
                    continue
                if e.tool_call.tool_name != "plan_step":
                    continue
                if (e.seq or 0) < plan_seq:
                    continue
                try:
                    idx = int(e.tool_call.arguments.get("index"))  # type: ignore[arg-type]
                    state = str(e.tool_call.arguments.get("state"))
                except (TypeError, ValueError):
                    continue
                if state == "done":
                    done_steps.add(idx)

            first_undone: int | None = None
            for i in range(1, len(latest_plan.steps) + 1):
                if i not in done_steps:
                    first_undone = i
                    break

            if first_undone is not None:
                step_title = latest_plan.steps[first_undone - 1].title
                parts.append(
                    f"Next actionable step ({first_undone}): '{step_title}'."
                    " Do not re-plan, do not summarize, and do not ask the user"
                    " anything — everything you need is in this message. Execute"
                    " this step now using tools."
                )

        result.append(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content="\n".join(parts)),
            )
        )
        return result

    async def resume_conversation(self, conversation_id: str) -> dict:
        """Mode-agnostic, event-log-driven resume. Legal from PAUSED and IDLE-with-
        unfinished-plan (an approved PlanEvent exists but FINISHED was never reached).

        Returns {"ok": True, "status": "RUNNING"} on success or {"ok": False, "reason": …}
        for illegal transitions — the HTTP route converts non-ok to 409.

        Deep Research conversations dispatch to the EXISTING DR resume behavior unchanged
        (mode check). Build/Research: flip PAUSED/IDLE → RUNNING in the event log so
        loop.run() doesn't early-return on PAUSED, then kick via the standard task-spawn
        path (same as send-message). kick() is idempotent — no second loop if one is live.
        """
        state = await self._store.get_state(conversation_id)
        status = state.execution_status

        if status == ConversationStatus.RUNNING:
            return {"ok": False, "reason": "already_running"}
        if status == ConversationStatus.FINISHED:
            return {"ok": False, "reason": "conversation_finished"}
        if status == ConversationStatus.ERROR:
            return {"ok": False, "reason": "conversation_error"}

        events = await self._store.get_events(conversation_id)

        # DC-05c: condense trailing degeneracy before reconstruction
        tombstone = self._condense_trailing_degeneracy(events)
        if tombstone is not None:
            await self._store.append(conversation_id, tombstone)
            events = await self._store.get_events(conversation_id)

        legal = status == ConversationStatus.PAUSED
        if not legal and status == ConversationStatus.IDLE:
            legal = _has_unfinished_plan(events)

        if not legal:
            return {"ok": False, "reason": f"illegal_state_{status.value}"}

        # Reconstruct resume context: synthesized observations + reality block.
        # All new events are appended BEFORE the RUNNING status flip so View.of
        # sees resolved action→observation pairs and the model re-orients correctly.
        new_events = await self._reconstruct_resume_context(conversation_id, events)
        for event in new_events:
            await self._store.append(conversation_id, event)

        # Clear any lingering cancel flag from a prior Stop.
        self._cancel_flags.pop(conversation_id, None)

        surface = self._surface_of(conversation_id)
        if surface == "deep_research":
            # DR: _maybe_run_deep_research detects PAUSED and re-runs from the partial
            # ReportEvent checkpoint — dispatch unchanged, do not touch DR internals.
            pass
        else:
            # Build/Research: flip to RUNNING so loop.run() doesn't early-return on PAUSED.
            # The loop emits another RUNNING at the top of run() — idempotent.
            await self._store.append(
                conversation_id,
                StatusEvent(status=ConversationStatus.RUNNING, detail="resumed"),
            )

        self.kick(conversation_id)
        return {"ok": True, "status": "RUNNING"}

    async def kill(self, conversation_id: str) -> None:
        """The KILL SWITCH (BoD §13.6) — the ultimate stop above the three security
        layers. Halts a RUNNING loop promptly (cancel the task mid-step), revokes the
        agent's capabilities + tears down the sandbox session (executor.kill), and
        records a terminal status so the UI reflects the stop."""
        # 1. stop the running loop task promptly — do NOT wait for the current step.
        task = self._tasks.pop(conversation_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # 2. revoke capabilities + destroy the sandbox (the executor's kill, §6.4),
        #    then DROP the executor + loop from the caches. Critical: a killed executor
        #    is permanently `_killed=True` and returns "executor killed; instance
        #    revoked" for every call — if it stayed cached, RESUMING the conversation
        #    would reuse the dead executor and every tool call would fail forever (the
        #    exact unrecoverable loop a build hit). Popping them forces `_loop_for` to
        #    rebuild a FRESH executor + sandbox on the next run.
        executor = self._executors.pop(conversation_id, None)
        if executor is not None:
            await executor.kill()
        pending = self._pending_sessions.pop(conversation_id, None)
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.destroy()
        self._loops.pop(conversation_id, None)
        # 3. record the stop so subscribers see it (no STOPPED status in the enum; IDLE
        #    + a 'killed' detail is the contract's terminal-for-now shape).
        await self._store.append(
            conversation_id,
            StatusEvent(
                source=EventSource.SYSTEM, status=ConversationStatus.IDLE, detail="killed"
            ),
        )

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for executor in self._executors.values():
            with contextlib.suppress(Exception):
                await executor.kill()
        for session in self._pending_sessions.values():
            with contextlib.suppress(Exception):
                await session.destroy()

    # ---- RP-08: scheduled tasks ---------------------------------------------

    def _schedule_manager(self) -> Any:
        """Lazy accessor for the ScheduleManager (avoids circular import at
        module load).  The manager is created on first access (or already held
        after _start_schedule_manager is called at lifespan start)."""
        if not hasattr(self, "_sched_manager"):
            from .schedule import ScheduleManager
            self._sched_manager = ScheduleManager(self._store, self)
        return self._sched_manager

    async def _schedule_manager_loop(self) -> None:
        """Thin trampoline: lifespan task → ScheduleManager.run().  Mirrors the
        _idle_sweep_loop pattern so the app lifespan owns the Task."""
        await self._schedule_manager().run()

    def create_schedule(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        rrule: str,
        description: str,
        depth: str | None = None,
        model_override: str | None = None,
    ) -> dict:
        """Create a new schedule and return it as a JSON-safe dict.
        Raises ValueError for invalid cron expressions (caller surfaces to user)."""
        sched = self._schedule_manager().create_schedule(
            conversation_id=conversation_id,
            owner_id=owner_id,
            rrule=rrule,
            description=description,
            depth=depth,
            model_override=model_override,
        )
        return sched.model_dump(mode="json")

    def list_schedules(
        self, *, owner_id: str, conversation_id: str | None = None
    ) -> list[dict]:
        """List schedules, optionally filtered to one conversation."""
        return [
            s.model_dump(mode="json")
            for s in self._schedule_manager().list_schedules(
                owner_id=owner_id, conversation_id=conversation_id
            )
        ]

    def delete_schedule(self, schedule_id: str, *, owner_id: str) -> bool:
        """Delete a schedule. OWNER-SCOPED. Returns True if a row was removed."""
        return self._schedule_manager().delete_schedule(schedule_id, owner_id=owner_id)

    def preview_schedule_runs(self, rrule: str, n: int = 3) -> list[str]:
        """Preview next N run times for a cron expression (ISO-8601 strings).
        Returns [] for invalid expressions."""
        return [
            dt.isoformat()
            for dt in self._schedule_manager().preview_next_runs(rrule, n)
        ]

    # -- activity dashboard ----------------------------------------------------

    def running_conversation_ids(self) -> set[str]:
        """The conversation ids with a LIVE (not-yet-done) run task in THIS process —
        the ground truth of "what's executing right now" (cached status can lag a
        crash). Done tasks are filtered, so a finished-but-uncleaned entry never
        counts. Not owner-scoped; the endpoint intersects with the owner's summaries."""
        return {cid for cid, task in self._tasks.items() if not task.done()}

    def list_recent_schedule_runs(self, *, owner_id: str, limit: int = 50) -> list[dict]:
        """Owner-scoped recent scheduled-run history for the activity dashboard
        (delegates to the store; newest first, with schedule description + title)."""
        fn = getattr(self._store, "list_recent_schedule_runs", None)
        if fn is None:  # a store without the audit table (defensive)
            return []
        return fn(owner_id, limit)
