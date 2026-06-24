"""Wire DTOs for the app-server settings surface (BoD §5.2).

Extracted from config_state.py (god-file decomposition, Wave 1). Pure Pydantic
data models with no state and no logic — the wire contract the frontend's data
layer consumes (they mirror the frontend's `src/types/models.ts` +
`src/types/config.ts`). The stateful `ConfigState` orchestrator and the pure
`RouterConfig`↔DTO mappers live alongside this module.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ModelDTO(BaseModel):
    id: str
    label: str
    provider: str  # derived view: "local" (free) | "openrouter" (paid)
    price_in_per_m: float
    price_out_per_m: float
    # W-05: pay model — "metered" | "subscription" | "free". None → the frontend
    # derives it from price/provider for back-compat (price 0 → free, else metered).
    pricing_mode: Literal["metered", "subscription", "free"] | None = None
    capabilities: list[str]
    note: str | None = None
    # raw editable fields (so the edit form prefills the real config, not a view):
    model_id: str
    base_url: str | None = None
    api_key_env: str | None = None
    context_window: int
    quantization: str | None = None


class ModelUpsert(BaseModel):
    """Create/edit a catalogue model. `id` is the catalogue key (immutable on
    edit). The endpoint key is derived (= id for new models), so the user only
    thinks in terms of a model + its endpoint, never an internal provider key."""

    id: str
    model_id: str
    base_url: str | None = None
    api_key_env: str | None = None
    context_window: int = 8192
    quantization: str | None = None
    capabilities: list[str] = []
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0
    # W-05: how the user pays — threaded so an edited/added subscription model keeps
    # its mode. None → derive (back-compat); not surfaced as free for "subscription".
    pricing_mode: Literal["metered", "subscription", "free"] | None = None


class OpenRouterModelDTO(BaseModel):
    """One model from the live OpenRouter catalogue, normalized to the fields the
    Add flow needs (the slug `id` becomes the model's model_id; prices are /Mtok)."""

    id: str  # slug, e.g. "anthropic/claude-3.5-sonnet"
    name: str
    context_length: int
    price_in_per_m: float
    price_out_per_m: float
    capabilities: list[str]
    # True when the model can OUTPUT images (architecture.output_modalities ∋ "image")
    # — lets the image-gen picker filter the catalogue to image-generation models.
    image_output: bool = False
    # USD per MILLION image-output tokens — the real image-generation cost. The
    # /models catalogue reports 0 for the dedicated generators (FLUX/Recraft/…); this
    # is enriched from each model's /endpoints `image_output` rate (×1e6). 0 = unknown.
    image_price_per_m: float = 0.0


class OpenRouterKeyStatus(BaseModel):
    configured: bool  # an encrypted key is stored
    locked: bool  # stored but not decryptable (PMX_SECRET_KEY missing/wrong)
    can_store: bool  # PMX_SECRET_KEY present, so a new key can be encrypted + saved


class OpenRouterKeyBody(BaseModel):
    key: str


class SecretBody(BaseModel):
    """Set-request body for a generic named provider secret."""

    value: str


class SecretStatus(BaseModel):
    """Status of one named secret. `value` is NEVER returned — only whether one
    is stored, plus the store's lock/can-store state (shared with OpenRouter)."""

    name: str
    configured: bool  # an encrypted value is stored under this name
    locked: bool  # a value is stored but can't be decrypted (no/wrong app secret)
    can_store: bool  # the app secret is present, so a value can be encrypted + saved


class SecretsListDTO(BaseModel):
    """All currently-stored secret names (never the values) + store capability.
    `locked_names` are the SPECIFIC stored keys that can't be decrypted right now
    (wrong/missing app secret, or a corrupted token) — so the UI can name exactly
    which keys the operator must restore or re-enter. `locked` = any are locked."""

    names: list[str]
    locked_names: list[str] = []
    locked: bool
    can_store: bool


class ProbeResult(BaseModel):
    """The outcome of a live "test" probe (provider key / data source / TTS /
    image / MCP). A real network call decides this — never a fake green. An
    EXPECTED failure (bad key, host down, misconfigured) returns ``ok=False``
    with a machine-readable ``status`` + human ``detail`` at HTTP 200, so the UI
    renders the truth without a 500. ``status`` vocabulary:
      ``ok`` — the provider really answered.
      ``unauthorized`` — reached, but the credential was rejected (401/403).
      ``unreachable`` — connect/timeout/DNS failure; the host didn't answer.
      ``misconfigured`` — nothing to probe (no endpoint/key/server resolved).
      ``disabled`` — the feature is turned off in Settings.
      ``procedural-fallback`` — a real tier is selected but the run fell back to
        the keyless procedural/bundled placeholder (image-gen #76).
      ``error`` — the provider answered with an unexpected/non-2xx status.
    Optional extras carry probe-specific facts (the model count, tool count,
    synthesized byte length) so the chip can show a concrete number."""

    ok: bool
    status: str
    detail: str = ""
    # probe-specific extras (None when not applicable to that probe)
    provider: str | None = None
    tool_count: int | None = None
    byte_count: int | None = None
    procedural: bool | None = None


class AssignmentsDTO(BaseModel):
    default_model: str
    roles: dict[str, str]  # {rag_answerer, query_rewriter, summarizer, nli_verifier} -> model id


class AssignmentsPatch(BaseModel):
    default_model: str | None = None
    roles: dict[str, str] | None = None


class SandboxConnectionDTO(BaseModel):
    """One backend's saved connection block — the wire mirror of core's
    SandboxConnection. Carried in :class:`SandboxConfigDTO.connections` so the UI can
    RESTORE a backend's last-known setup on switch instead of blanking it."""

    docker_socket: str = ""
    podman_url: str = ""
    runtime: str = ""
    image: str = ""
    workspace_root: str = ""


class SandboxConfigDTO(BaseModel):
    """The active sandbox backend + its (non-secret) connection — the wire mirror of
    core's SandboxSettings. Persisted in the shared ConfigStore; the agent-server maps
    it to the live backend. No secrets (remote connections are keyless Tailscale SSH).

    ``connections`` exposes EVERY backend's last-saved block (keyed by backend id) so the
    Settings UI restores a backend's own connection when you flip to it — the persistence
    fix for the switch-blanks-the-other-backend outage. It's read-only context for the
    client; the server is authoritative and merges it on save."""

    backend: str  # "process" | "gvisor" | "local" | "podman"
    docker_socket: str
    podman_url: str
    runtime: str
    image: str
    workspace_root: str
    connections: dict[str, SandboxConnectionDTO] = {}


class SandboxHealthDTO(BaseModel):
    """Reachability of the ACTIVE (persisted) sandbox backend — the cheap, side-effect-
    free health signal the app shell surfaces as a banner BEFORE a run is started. Uses
    the SAME probe (`healthcheck()`) the run path hits, so the banner and the real run
    agree. ``reachable`` False carries the typed, host-naming ``detail`` (e.g. "gvisor
    sandbox host ssh://sandbox@ unreachable: …")."""

    reachable: bool
    backend: str
    detail: str = ""


class EncodersConfigDTO(BaseModel):
    """Where the non-generative encoders (embeddings / rerank / NLI) run. `remote=False`
    (default) = BUNDLED in-process (ONNX/CPU); `remote=True` = the external LAN
    endpoints. The three endpoint URLs are UI-editable when Remote is selected and
    persisted; an empty one falls back to the PMX_*_URL env default. The wire mirror
    of core's EncodersSettings — the agent-server honors it on the next research run.
    NOT an LLM-router role assignment."""

    remote: bool
    reranker_url: str = ""
    embedder_url: str = ""
    nli_url: str = ""


class TtsConfigDTO(BaseModel):
    """Audio-overview TTS (RP-09) — the universal THREE-tier provider DTO (same shape
    as search/extraction). `enabled=False` turns the feature off AND lets the
    agent-server unload the Kokoro model. `provider`:
      - `bundled` (default) — in-process Kokoro (ONNX/CPU, keyless).
      - `speaches` — a self-hosted OpenAI-compatible `/v1/audio/speech` endpoint
        (`base_url`; empty → SPEACHES_URL env default), keyless.
      - `openai` — a paid OpenAI-compatible vendor (`base_url` + `api_key_env` naming
        the secret/env var, never the key; `model` e.g. "tts-1").
    Wire mirror of core's TtsSettings — the agent-server honors it on the next overview.
    NOT an LLM-router role assignment.

    No `loaded` indicator: the Kokoro model is resident in the AGENT-server process,
    which this (app) server cannot introspect — a field here would always read False
    and mislead. The UI shows a static "loads ~0.5 GB on first use" note instead."""

    enabled: bool = True
    provider: Literal["bundled", "speaches", "openai"] = "bundled"
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""
    voice_a: str = "af_heart"
    voice_b: str = "af_bella"


class ImageGenConfigDTO(BaseModel):
    """Image generation provider DTO — real, configured backends only (W-50: the old
    bundled `procedural` Pillow tier was removed; it was a false affordance that
    always "succeeded" with abstract patterns). Until a tier is fully configured,
    image generation is NOT CONFIGURED and the tool fails loudly:
      - `comfyui` — self-hosted ComfyUI graph API (`base_url`; required), keyless
        (assumes local/network-accessible). Uses /prompt + /history poll.
      - `openai` — paid OpenAI-compatible `/v1/images/generations` endpoint
        (`base_url` + `api_key_env` naming the secret/env var, never the key
        itself), e.g. DALL-E 3.
      - `openrouter` (default) — image models via OpenRouter chat-completions using
        the shared OpenRouter key; inactive until that key is stored.
    Wire mirror of core's ImageGenSettings — the agent-server honors it on the
    next image-gen call. NOT an LLM-router role assignment."""

    provider: Literal["comfyui", "openai", "openrouter"] = "openrouter"
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""  # openai: image model id; comfyui: checkpoint filename; empty → default
    # comfyui ONLY: optional ComfyUI "Save (API Format)" graph overriding the built-in
    # SDXL default. Empty → built-in default. Tokens: %prompt% %negative% %seed% %width%
    # %height% %ckpt%. Lets FLUX / SD3 / custom shapes work without code changes.
    workflow_json: str = ""


class DataSourcesConfigDTO(BaseModel):
    """The universal web-data providers (§B). Each slot has three tiers; the bundled
    defaults (`ddgs` / `local`) need no key. `api_key_env` is the NAME of the env var
    holding a paid key (never the key itself). The wire mirror of core's
    SearchSettings + ExtractionSettings."""

    search_provider: Literal["ddgs", "searxng", "tavily", "brave"] = "ddgs"
    search_base_url: str = ""
    search_api_key_env: str = ""
    extraction_provider: Literal["local", "crawl4ai", "firecrawl"] = "local"
    extraction_base_url: str = ""
    extraction_api_key_env: str = ""


class ProjectStorageConfigDTO(BaseModel):
    """Where Build projects persist on the app host — the user-chosen directory.

    Wire mirror of core's `ProjectStorageSettings`. The `status` field is derived
    on the GET path so the UI knows immediately whether the saved path is valid
    (ok / unset / not_found / not_a_directory / not_writable); on PUT, the path
    is validated server-side and a 400 with a typed reason is returned for any
    non-OK status.

    `effective_root` is the REAL directory in use — auto-created when
    `projects_root` is empty (the zero-config default) and equal to
    `projects_root` when the user has set one. This lets the UI show WHERE
    builds will save (even with the input left blank) without losing the
    explicit-vs-default distinction carried by `projects_root` itself."""

    projects_root: str = ""  # raw configured value; "" means "use the auto default"
    status: str = "unset"  # informational; populated by the GET path
    effective_root: str = ""  # the real, auto-created directory currently in use


class SkillDTO(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool
    body: str = ""  # the markdown instructions handed to the agent
    # Which surfaces the skill applies to (["build"], ["agent"], or both). Empty =
    # all surfaces (back-compat). The agent-server filters by this per conversation.
    surfaces: list[str] = []


class SkillPatch(BaseModel):
    """Partial update for an existing skill. Any field omitted is left unchanged."""

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    body: str | None = None
    surfaces: list[str] | None = None


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    body: str = ""
    enabled: bool = True
    surfaces: list[str] = []


class McpServerConfigDTO(BaseModel):
    """POST/PATCH body for creating or updating an MCP server."""

    name: str
    url: str
    transport: str = "stdio"  # "stdio" | "streamable_http"
    enabled: bool = True
    allowed_tools: list[str] | None = None
    risk_tier: str = "medium"


class McpServerApproveDTO(BaseModel):
    """POST body for approve/re-approve — carries the new description hash."""

    description_hash: str


class McpConnectionDTO(BaseModel):
    id: str
    name: str
    url: str
    status: str  # "connected" | "disconnected" | "error"
    transport: str | None = None  # "stdio" | "streamable_http"
    risk_tier: str | None = None
    description_hash: str | None = None  # SHA-256 of approved tool descriptions (the OLD hash)
    # E6 (#10): the AUTHORITATIVE new_hash the live agent-server pool just
    # computed at startup when it detected drift against the stored approval.
    # Surfaces on the ApprovalDiff so the user sees the REAL fingerprint of
    # the changed tool set — not a stub of the stored hash. None when the
    # server is in sync (no drift) or has never been approved (first connect).
    new_description_hash: str | None = None
    approved_at: str | None = None  # ISO-8601
    enabled: bool | None = None


class BuildKernelConfigDTO(BaseModel):
    """Build kernel selector — the wire mirror of core's RouterConfig.build_kernel
    (Disco Pi Build Kernel Campaign A2). `kind` is the persisted choice; the
    read-only `experimental_enabled` reports whether the agent-server's experimental
    flag is on, so the Settings UI can offer/hide the `pi_experimental` option and
    not present a false affordance. PUT sends `kind` only."""

    kind: Literal["disco", "pi_experimental"] = "disco"
    experimental_enabled: bool = False


class LiveBrowserConfigDTO(BaseModel):
    """Live browser (noVNC) toggle — the wire mirror of core's LiveBrowserSettings.
    Off by default; enabling shows a 'Live' toggle on the Agent canvas browser pane.
    The Xvfb + x11vnc + websockify stack starts lazily on first click; idle cost ~0.
    VNC is loopback-bound inside the sandbox. gVisor needs D7 egress allowlist update.

    P5 live jail acceptance is HARDWARE-DEFERRED (sandbox VM destroyed). Verify on
    a real sandbox backend before shipping to production."""

    enabled: bool = False
