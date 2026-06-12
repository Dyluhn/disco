"""Router configuration — llm-router-contract.md §7 (v1.2 deterministic).

All `[VERIFY]` specifics (model ids, context sizes, prices, family tags) live
HERE — one config surface (BoD §19), editable without touching any caller. This
is the single file you revise when models or prices change (R10).

v1.2 LOBOTOMY: model selection is now ABSOLUTE and config-driven. A role resolves
to exactly one model by direct lookup — `default_model` for the driver, an
explicit `assignments[role]` for every other role, optionally overridden per
conversation by the model pill. There is no overflow policy, no difficulty
assessment, no capability-based escalation: the mapping IS the source of truth
and the router obeys it without deviation (see `model_for`). The intelligent-
routing scaffolding (`RoleRouting`, threshold fields) is retained DORMANT for the
documented revival path.

The starting assignments are encoded as a `default_config()` factory with
**placeholder** model ids — deliberately NOT the operator's real homelab model
ids or OpenRouter strings, which are [VERIFY] and filled in at wiring time.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from .types import ModelRole, Requirement


class ModelEntry(BaseModel):
    model_id: str  # provider's id string [VERIFY]
    provider: str  # the endpoint key (one OpenAIProvider per distinct backend) [VERIFY]
    context_window: int  # [VERIFY]
    capabilities: frozenset[Requirement] = frozenset()
    quantization: str | None = None  # e.g. "Q4_K_M"; informational provenance
    # per-million-token prices for cost accounting; 0 for local. [VERIFY]
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0
    # [EXTENSION] §8 requires a model family for prompt selection but §7's
    # ModelEntry omitted the field. Optional here: if None, the family is
    # derived from model_id (prompts.derive_family). Set it to pin a [VERIFY]
    # family tag explicitly.
    family: str | None = None
    # [VERIFY] live wiring: the OpenAI-compatible base URL for this model's backend
    # (None = no live adapter, e.g. the NLI cross-encoder or a dormant overflow
    # slot). `api_key_env` names an env var holding the key, if the server needs one.
    base_url: str | None = None
    api_key_env: str | None = None


class RoleRouting(BaseModel):
    """[DORMANT — v1.2] The intelligent-routing per-role bundle. No longer read
    by the deterministic router (a role now maps to ONE model via
    `RouterConfig.assignments`/`default_model`). Retained as the revival
    scaffolding referenced by the dormant policy and the v1.2 contract appendix."""

    primary: str  # ModelEntry key for the local default
    overflow: str | None = None  # ModelEntry key for the OpenRouter path
    overflow_eligible: bool = False  # may this role overflow at all? (§5.1)
    overflow_ladder: list[str] = Field(default_factory=list)  # stronger models (§5.3)


class SandboxSettings(BaseModel):
    """[settings] The active sandbox backend + its (non-secret) connection details.

    Plain config held in `core` (so it persists in the SAME shared ConfigStore as the
    model catalogue — no parallel config path); the agent-server maps it to the concrete
    `SandboxBackend`. Remote connections are KEYLESS over Tailscale SSH — there are no
    secrets here, only host/socket/runtime detail.
    """

    # seconds a non-RUNNING sandbox may sit idle before suspend
    idle_ttl_s: int = 1800
    # which backend is active. "process" (dev, host) | "gvisor" (strong, remote) |
    # "local" (container, same host) | "podman" (remote; a STUB in this environment).
    backend: str = "local"
    # gVisor / Docker host endpoint — a local socket OR Docker-over-SSH (ssh://user@host).
    docker_socket: str = "unix:///var/run/docker.sock"
    # Podman native remote (rootless socket over Tailscale SSH).
    podman_url: str = "http+ssh://sandbox@100.73.110.47/run/user/1000/podman/podman.sock"
    # the OCI runtime: runsc (gVisor), runc/crun (local/podman).
    runtime: str = "runc"
    image: str = "pmx-sandbox:base"
    # host dir bind-mounted to the container workspace (gVisor); local uses a named volume.
    workspace_root: str = "/opt/sandbox/workspaces"


class ProjectStorageSettings(BaseModel):
    """[settings] Where Build projects persist on the APP HOST — the user-chosen
    directory under which each project's manifest + workspace tree lives.

    Plain config held in `core` (same shared ConfigStore as the model catalogue);
    the agent-server reads it to snapshot/rehydrate Build workspaces. Empty by
    default — "not configured" — so an unset path is detectable rather than
    accidentally falling back to a hidden default. Validation is done at the
    SET path (the settings PUT endpoint) not at construction; load-time
    construction must not throw."""

    # absolute directory on the app host where projects persist. Empty == unset.
    projects_root: str = ""


class EncodersSettings(BaseModel):
    """[settings] Where the non-generative encoders (embeddings / rerank / NLI)
    run. `remote=False` (default) = BUNDLED in-process (ONNX/CPU via fastembed —
    self-contained, no encoder server). `remote=True` = the external LAN endpoints
    (TEI / OpenAI-embeddings / NLI-sidecar). The three endpoint URLs are PERSISTED
    here (UI-editable when Remote is selected); an empty string falls back to the
    PMX_*_URL env default, so an unconfigured remote still resolves.
    These are NOT LLM-router roles — they don't follow the model assignments."""

    remote: bool = False
    reranker_url: str = ""  # empty → PMX_RERANKER_URL env default
    embedder_url: str = ""  # empty → PMX_EMBEDDER_URL env default
    nli_url: str = ""  # empty → PMX_NLI_URL env default


class TtsSettings(BaseModel):
    """[settings] Audio-overview TTS (RP-09). `enabled=False` turns the feature off
    AND lets the agent-server unload the model to free RAM. `remote=False` (default)
    = BUNDLED in-process Kokoro (ONNX/CPU, weights download on first use — same
    doctrine as EncodersSettings; lazy-loaded, so enabled-but-unused costs no RAM).
    `remote=True` = an external Speaches `/v1/audio/speech` endpoint (`speaches_url`;
    empty → the SPEACHES_URL env default). Voices are the ratified af_heart/af_bella."""

    enabled: bool = True
    remote: bool = False
    speaches_url: str = ""  # empty → SPEACHES_URL env default (remote tier only)
    voice_a: str = "af_heart"
    voice_b: str = "af_bella"


class SearchSettings(BaseModel):
    """[settings] Web DISCOVERY provider. The THREE tiers of the universal design:
    (a) self-host `searxng` (base_url), (b) a paid API `tavily`/`brave` (BYO key
    in secrets via api_key_env), and (c) the BUNDLED `ddgs` — DuckDuckGo scraping,
    in-process, no key, no container — the FIRST-RUN DEFAULT so a fresh install
    searches the moment it's downloaded."""

    provider: Literal["ddgs", "searxng", "tavily", "brave"] = "ddgs"
    base_url: str = ""  # for searxng (self-host); empty → PMX_SEARXNG_URL env
    api_key_env: str = ""  # secrets key name for tavily/brave (never the key itself)


class ExtractionSettings(BaseModel):
    """[settings] URL → clean content provider. Same three tiers: (a) self-host
    `crawl4ai` (base_url), (b) paid `firecrawl` (BYO key), (c) the BUNDLED `local`
    — in-process httpx fetch + stdlib readability→markdown, no service — the
    FIRST-RUN DEFAULT so extraction works offline-of-services out of the box."""

    provider: Literal["local", "crawl4ai", "firecrawl"] = "local"
    base_url: str = ""  # for crawl4ai (self-host) / firecrawl base; empty → default
    api_key_env: str = ""  # secrets key name for firecrawl (never the key itself)


class McpSettings(BaseModel):
    """[settings] MCP (Model Context Protocol) client settings — RP-05.
    The explicit McpServerConfig typed dict lives in tools/mcp; this is the
    top-level toggle + server map the Settings surface reads/writes. Off by
    default so a fresh install is unchanged."""

    enabled: bool = False  # off by default — opt-in
    servers: dict[str, dict] = Field(default_factory=dict)  # name -> {transport, command, url, ...}
    max_active_schemas: int = 20  # cap; beyond this, tool_search is exposed


class RouterConfig(BaseModel):
    models: dict[str, ModelEntry]  # key -> entry (the assignable catalogue)
    # the active sandbox backend + connection (settings-driven; agent-server maps it).
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    # the user-chosen Build-project persistence root (empty = unset).
    projects: ProjectStorageSettings = Field(default_factory=ProjectStorageSettings)
    # where the bundled-vs-remote encoders run (settings-driven; agent-server honors it).
    encoders: EncodersSettings = Field(default_factory=EncodersSettings)
    # audio-overview TTS: bundled in-process Kokoro vs remote Speaches, + the toggle.
    tts: TtsSettings = Field(default_factory=TtsSettings)
    # universal data providers — bundled (ddgs / local) by default so a fresh
    # install works with no keys; upgradeable to self-host or paid in Settings.
    search: SearchSettings = Field(default_factory=SearchSettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)
    # MCP (Model Context Protocol) — external tool servers (RP-05).
    # Off by default; the pool is built at agent-server start when enabled.
    mcp: McpSettings = Field(default_factory=McpSettings)
    # DF-08: catalogue key of a vision-capable model to escalate image-bearing
    # requests to when the primary model lacks VISION. None → vision guard stays
    # hard (raise NoEligibleModel). Swappable to any vision model in the catalogue
    # (e.g. "or-gemma-4-31b-free" for free tier, "driver-overflow" for Sonnet).
    vision_escalation_model: str | None = None
    # v1.2 deterministic assignment — the source of truth (R10):
    default_model: str  # AGENT_DRIVER's model + fallback for any unassigned role
    assignments: dict[ModelRole, str] = Field(default_factory=dict)  # explicit per-role
    # cost governance (§5.2) — PASSIVE spend backstop in v1.2: it observes spend
    # and refuses to exceed a HARD cap, but it no longer influences which model is
    # chosen (selection is `assignments`-driven, full stop).
    soft_budget_usd_per_conversation: float | None = None
    hard_budget_usd_per_conversation: float | None = None
    soft_budget_usd_global_daily: float | None = None
    hard_budget_usd_global_daily: float | None = None

    # === INTELLIGENT ROUTING (DORMANT) — see policy.py / routing.py ============
    # The intelligent router consumed per-role `RoleRouting` (primary + overflow +
    # eligibility + ladder) PLUS the thresholds below to decide local-vs-frontier
    # escalation. v1.2 lobotomized selection to deterministic assignment, so these
    # inputs are no longer read. Kept commented for the documented revival path
    # (llm-router-contract.md v1.2 appendix). To revive: restore these fields,
    # re-enable the policy in policy.py, and switch routing.py's `_resolve` back to
    # the intelligent body preserved there.
    #     roles: dict[ModelRole, RoleRouting]
    #     local_retry_before_overflow: int = 1
    #     errors_before_overflow: int = 2
    #     confidence_floor: float = 0.5
    # === END DORMANT ==========================================================

    def model_for(self, role: ModelRole, *, override: str | None = None) -> str:
        """Deterministic role -> model-key resolution (v1.2). Precedence:
        per-conversation `override` (the main-screen model pill) > explicit
        settings `assignments[role]` > `default_model`. No policy, no capability
        escalation — the mapping is obeyed without deviation. Returns a KEY into
        `models`; `entry_for` resolves it (and fails loud if it is unknown)."""
        if override is not None:
            return override
        return self.assignments.get(role, self.default_model)

    def entry_for(self, key: str) -> ModelEntry:
        return self.models[key]


def default_config() -> RouterConfig:
    """The starting catalogue + assignments, wired to the real LAN endpoints
    ([VERIFY] — discovered at build: see api-endpoints.md / the live-wiring build).

    Each `provider` is the endpoint KEY (one OpenAIProvider per distinct backend);
    `base_url` is its OpenAI-compatible URL. Roles: AGENT_DRIVER + RAG_ANSWERER →
    Qwen 27B (.231); QUERY_REWRITER + SUMMARIZER → Gemma E2B (.81, needs a key in
    $PMX_GEMMA_API_KEY); NLI_VERIFIER → the cross-encoder sidecar (not a chat model,
    handled by the grounding NLIVerifier, no base_url). "driver-overflow" stays as a
    dormant, assignable OpenRouter slot (no key → fails loud if assigned).
    """
    _QWEN = "http://192.168.1.231:18080/v1"
    _GEMMA = "http://192.168.1.81:8087/v1"
    # [BP-00] Vision gate: the driver is vision-capable ONLY if enabled via env.
    driver_caps = {Requirement.TOOL_CALLING, Requirement.JSON_MODE, Requirement.LONG_CONTEXT}
    if os.environ.get("PMX_DRIVER_VISION") == "1":
        driver_caps.add(Requirement.VISION)

    models = {
        # AGENT_DRIVER + RAG_ANSWERER → Qwen 27B (reasoning, 128K ctx).
        "driver-local": ModelEntry(
            model_id="Qwen3.6-27B-UD-Q5_K_XL.gguf",
            provider="qwen",
            base_url=_QWEN,
            context_window=131_072,
            capabilities=frozenset(driver_caps),
            quantization="Q5_K_XL",
            family="qwen",
        ),
        "rag-local": ModelEntry(
            model_id="Qwen3.6-27B-UD-Q5_K_XL.gguf",
            provider="qwen",
            base_url=_QWEN,
            context_window=131_072,
            capabilities=frozenset(
                {Requirement.TOOL_CALLING, Requirement.JSON_MODE, Requirement.LONG_CONTEXT}
            ),
            quantization="Q5_K_XL",
            family="qwen",
        ),
        # QUERY_REWRITER + SUMMARIZER → Gemma E2B (cheap, fast, not a reasoning
        # model). The server needs a key in $PMX_GEMMA_API_KEY (read at wiring time
        # via api_key_env — never hardcoded).
        "rewriter-local": ModelEntry(
            model_id="gemma-4-e2b-mtp",
            provider="gemma",
            base_url=_GEMMA,
            api_key_env="PMX_GEMMA_API_KEY",
            context_window=32_768,
            capabilities=frozenset({Requirement.JSON_MODE}),
            family="gemma",
        ),
        "summarizer-local": ModelEntry(
            model_id="gemma-4-e2b-mtp",
            provider="gemma",
            base_url=_GEMMA,
            api_key_env="PMX_GEMMA_API_KEY",
            context_window=32_768,
            family="gemma",
        ),
        # NLI verifier — the cross-encoder sidecar, NOT a chat model (no base_url;
        # the grounding NLIVerifier calls it directly, §9.2).
        "nli-local": ModelEntry(
            model_id="bge-reranker-v2-m3",
            provider="local",
            context_window=512,
            family="deberta",
        ),
        # Dormant, assignable OpenRouter overflow (no key → fails loud if assigned).
        "driver-overflow": ModelEntry(
            model_id="anthropic/claude-3.5-sonnet",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="PMX_OPENROUTER_API_KEY",
            context_window=200_000,
            capabilities=frozenset(
                {
                    Requirement.TOOL_CALLING,
                    Requirement.JSON_MODE,
                    Requirement.LONG_CONTEXT,
                    Requirement.VISION,
                }
            ),
            family="anthropic",
            price_in_per_m=3.0,
            price_out_per_m=15.0,
        ),
        # DF-08: Vision escalation target — Gemini 3 Flash via OpenRouter.
        # Proven 4/4 in the vision bake-off. Slug google/gemini-3-flash-preview
        # is the real GA-track id; bare google/gemini-3-flash does NOT exist.
        # Documented fallback (one-line disco-config.json swap, no code
        # change): google/gemini-3.5-flash (newer, non-preview).
        "or-gemini-3-flash": ModelEntry(
            model_id="google/gemini-3-flash-preview",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="PMX_OPENROUTER_API_KEY",
            context_window=1_048_576,
            capabilities=frozenset(
                {
                    Requirement.TOOL_CALLING,
                    Requirement.LONG_CONTEXT,
                    Requirement.VISION,
                    Requirement.JSON_MODE,
                }
            ),
            price_in_per_m=0.075,
            price_out_per_m=0.30,
        ),
    }
    assignments = {
        # AGENT_DRIVER intentionally omitted: it resolves to `default_model`.
        ModelRole.RAG_ANSWERER: "rag-local",
        ModelRole.QUERY_REWRITER: "rewriter-local",
        ModelRole.SUMMARIZER: "summarizer-local",
        ModelRole.NLI_VERIFIER: "nli-local",
    }
    return RouterConfig(
        models=models,
        default_model="driver-local",
        assignments=assignments,
        vision_escalation_model="or-gemini-3-flash",
    )


def apply_runtime_capabilities(config: RouterConfig) -> RouterConfig:
    """[BP-00] Overlay deployment-runtime capabilities onto a loaded config.

    PMX_DRIVER_VISION describes the LIVE llama-server (is an mmproj loaded right
    now?), not a user catalogue preference — so it is applied over whatever the
    persisted file says, in BOTH directions, on every load. Without this, a
    config file persisted before the vision rollout silently pins the driver
    text-only (the routing guard then rejects its own driver's images); and a
    file that persisted VISION keeps advertising it after the deployment loses
    the mmproj. The env var is authoritative for `driver-local` only.
    """
    entry = config.models.get("driver-local")
    if entry is None:
        return config
    vision_on = os.environ.get("PMX_DRIVER_VISION") == "1"
    if vision_on == (Requirement.VISION in entry.capabilities):
        return config
    caps = set(entry.capabilities)
    if vision_on:
        caps.add(Requirement.VISION)
    else:
        caps.discard(Requirement.VISION)
    models = {
        **config.models,
        "driver-local": entry.model_copy(update={"capabilities": frozenset(caps)}),
    }
    return config.model_copy(update={"models": models})
