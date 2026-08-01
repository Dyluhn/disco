"""Image generation tool — keyless/local-capable backend with a BINARY-safe
write path so the generated image round-trips byte-identical and surfaces as
a deliverable artifact.

Backend design
--------------
The tool is structured around an `ImageBackend` protocol with real, configured
backends only — the universal self-host/paid pattern:
- `_ComfyUIBackend`: Self-hosted ComfyUI graph API (real diffusion on your own
  GPU box; keyless, needs a base_url).
- `_OpenAIImageBackend`: OpenAI-compatible /v1/images/generations API (paid).
- `_OpenRouterImageBackend`: OpenRouter chat-completions image models (paid).

Real diffusion runs out-of-process (ComfyUI or a paid API), NOT in-process: an
in-process `diffusers`+`torch` backend was deliberately SCRAPPED — it would pin
multi-GB of weights to the app process and break the 8 GB keyless ship target,
and ComfyUI already gives a local-GPU path without that cost.

W-50 — NO bundled procedural tier
---------------------------------
The old `_PILProceduralBackend` (a keyless Pillow pattern generator) was REMOVED.
It was a false affordance: it always "succeeded" with an abstract pattern, so the
tool looked wired even when no real generator was connected, and `select_image_backend`
silently fell back to it on any missing config. Now `select_image_backend()` raises
`ImageGenNotConfigured` when the selected tier isn't fully configured (no ComfyUI
base_url, no stored OpenAI/OpenRouter key, or an unknown provider). Callers handle
that explicitly: the `image_generate` tool returns a NOT-CONFIGURED failure, and the
slides pipeline DEGRADES to image-less slides (never crashes).

Binary safety
-------------
The image bytes flow: backend → `bytes` object → `ctx.sandbox.write_file(path,
bytes)` (the same path as `audio_overview` and `slides`). There is NO
text-mode or `str.encode()` step anywhere in the chain — PNG/JPEG bytes above
0x7F would silently corrupt on a UTF-8 encode (PIL already hands us raw
container bytes via BytesIO). The on-disk bytes are byte-identical to the
backend's output.

F3 guard is NOT touched
-----------------------
`FileWriteTool` is the only place the F3 read-before-write tracker is
consulted. The image-gen tool writes through `ctx.sandbox.write_file` directly,
so the assist gate never sees it — its presence/absence is invisible to the
tracker. This is intentional: the F3 guard is about the model clobbering an
existing file it never read, which is a different failure mode (silent data
loss on a text file the model could have read first) than a brand-new
deliverable file the tool just synthesized.

No bytes in text context
------------------------
`ToolOutcome.content` is a short human-readable summary ("Image written:
1024 bytes to image.png, 64x64 PNG"). The actual image bytes live ONLY in the
sandbox filesystem and are referenced by the artifact path. The model never
sees the raw bytes — only metadata (size, dimensions, format, prompt, seed).

Secret handling
---------------
The OpenAI-compatible backend fetches the API key from the encrypted secret
store via the api_key_env name. Keys are NEVER logged or echoed — only the
presence/absence of the key is surfaced in error messages.

Internal layout
----------------
The three concrete backend implementations (`_OpenAIImageBackend`,
`_OpenRouterImageBackend`, `_ComfyUIBackend`) plus the binary-format helpers
they share (`_normalize_to_png_or_jpeg`, `_raise_image_status`, the PNG/JPEG
magic-byte constants) live in the private `image_gen_parts` subpackage — see
`image_gen_parts/_backends.py`. This module remains the sole public
compatibility/export facade: it re-imports every one of those names unchanged,
so every existing import path (production code and tests) keeps working.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from typing import Protocol

from disco.core.effects import EffectCapability
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.secret_refs import resolve_provider_secret, secret_ref_allowed_for_origin
from disco.core.llm.secrets import SecretStore
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from .image_gen_parts._backends import (
    _COMFY_DEFAULT_DIMENSION as _COMFY_DEFAULT_DIMENSION,
)
from .image_gen_parts._backends import (
    _COMFY_MIN_DIMENSION as _COMFY_MIN_DIMENSION,
)
from .image_gen_parts._backends import (
    _DEFAULT_COMFY_NEGATIVE as _DEFAULT_COMFY_NEGATIVE,
)
from .image_gen_parts._backends import (
    _JPEG_MAGIC as _JPEG_MAGIC,
)
from .image_gen_parts._backends import (
    _MAX_IMAGE_BYTES as _MAX_IMAGE_BYTES,
)
from .image_gen_parts._backends import (
    _MAX_IMAGE_PIXELS as _MAX_IMAGE_PIXELS,
)
from .image_gen_parts._backends import (
    _PNG_MAGIC as _PNG_MAGIC,
)
from .image_gen_parts._backends import (
    _comfy_snap_dim as _comfy_snap_dim,
)
from .image_gen_parts._backends import (
    _ComfyUIBackend as _ComfyUIBackend,
)
from .image_gen_parts._backends import (
    _normalize_to_png_or_jpeg as _normalize_to_png_or_jpeg,
)
from .image_gen_parts._backends import (
    _OpenAIImageBackend as _OpenAIImageBackend,
)
from .image_gen_parts._backends import (
    _OpenRouterImageBackend as _OpenRouterImageBackend,
)
from .image_gen_parts._backends import (
    _raise_image_status as _raise_image_status,
)

# --- compatibility re-exports (PKG-10-MEDIA) --------------------------------
# Names this module exposed BEFORE the parts split. Consumers and the test
# suite reach several of them through this module object (including via
# monkeypatch), so the facade must keep exposing every one. Restored
# programmatically by diffing this module's top-level names against its
# parent commit; PKG-13-FACADES owns their eventual deletion.
from .image_gen_parts._backends import (
    httpx as httpx,
)

_SVG_FALLBACK_HINT = (
    "Draw a bespoke inline SVG in the committed palette instead; do not leave the "
    "slot empty or hotlink external images."
)

# W-50: the operator-facing "no real image backend is configured" message, shared by
# every caller (the tool's NOT-CONFIGURED outcome, the slides degrade path, probes).
_NOT_CONFIGURED_MSG = (
    "Image generation isn't configured — set ComfyUI (base URL), an OpenAI-compatible "
    "API (base URL + key), or OpenRouter (store the OpenRouter key) in "
    f"Settings → Image generation. {_SVG_FALLBACK_HINT}"
)


class ImageGenNotConfigured(RuntimeError):
    """Raised by `select_image_backend()` when the selected image-gen tier is not
    fully configured (W-50). Replaces the old silent fallback to a procedural
    placeholder: callers handle it explicitly (tool → NOT-CONFIGURED outcome;
    slides → image-less degrade) instead of shipping fake art."""

    def __init__(self, message: str = _NOT_CONFIGURED_MSG) -> None:
        super().__init__(message)


def _with_svg_fallback_hint(message: str) -> str:
    return f"{message} {_SVG_FALLBACK_HINT}"


# ---- binary formats ---------------------------------------------------------
# `_PNG_MAGIC` / `_JPEG_MAGIC` / `_MAX_IMAGE_BYTES` / `_MAX_IMAGE_PIXELS` /
# `_normalize_to_png_or_jpeg` / `_raise_image_status` live in
# `image_gen_parts/_backends.py` (imported above) — this is where the format
# CONTRACT for the tool itself (which formats it accepts) is declared.

_SUPPORTED_FORMATS: frozenset[str] = frozenset({"png", "jpeg"})


# A small, bounded size cap. The procedural backend renders pixel-by-pixel
# (cheap) and any real model backend will respect it. Keeps a misbehaving
# call from rendering a 16384x16384 texture and OOMing the box.
_MAX_DIMENSION = 2048
_MIN_DIMENSION = 8


# ---- args model -------------------------------------------------------------


class ImageGenArgs(BaseModel):
    """Arguments for the `image_generate` tool."""

    prompt: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Text description of the image to generate — the conditioning prompt "
            "for the configured ComfyUI / OpenAI / OpenRouter backend."
        ),
    )
    filename: str = Field(
        default="image",
        description=(
            "Base filename WITHOUT extension (e.g. 'cover-art'). The tool "
            "appends the format-appropriate extension (.png or .jpg)."
        ),
    )
    width: int = Field(
        default=64,
        ge=_MIN_DIMENSION,
        le=_MAX_DIMENSION,
        description=f"Image width in pixels (clamped to [{_MIN_DIMENSION}, {_MAX_DIMENSION}]).",
    )
    height: int = Field(
        default=64,
        ge=_MIN_DIMENSION,
        le=_MAX_DIMENSION,
        description=f"Image height in pixels (clamped to [{_MIN_DIMENSION}, {_MAX_DIMENSION}]).",
    )
    seed: int | None = Field(
        default=None,
        description=(
            "Optional explicit seed. Omit (or pass null) to derive a seed "
            "deterministically from the prompt — same prompt + same seed = "
            "same image, every time."
        ),
    )
    format: str = Field(
        default="png",
        description="Output format: 'png' (lossless) or 'jpeg' (smaller). Default: 'png'.",
    )


# ---- backend protocol + default implementation -----------------------------


class ImageBackend(Protocol):
    """The interface a real image-gen backend must speak. The tool only
    depends on this shape — the implementation is injected at runtime, so any
    new provider backend can be slotted in without touching the tool's `run()` body."""

    name: str  # surfaced in the deliverable summary ("backend": "comfyui" / "openai-…")
    is_remote: bool  # network-bound? (True for every real backend)

    def generate(
        self,
        *,
        prompt: str,
        width: int,
        height: int,
        seed: int,
        fmt: str,
    ) -> bytes:
        """Return the encoded image bytes (PNG or JPEG container). Must
        return raw `bytes` — never `str`, never a file handle. The tool
        writes them straight to the sandbox; any text-mode conversion here
        would silently corrupt bytes >= 0x80."""
        ...


def _prompt_seed(prompt: str) -> int:
    """Derive a stable 32-bit seed from a free-form prompt. SHA-256 is overkill
    for a seed but is already in stdlib, deterministic across Python versions,
    and gives the model a clean "same prompt → same image" property for tests
    and for the agent to iterate on a prompt visually."""
    h = hashlib.sha256(prompt.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big", signed=False)


def select_image_backend() -> ImageBackend:
    """Factory function to select the image generation backend based on settings.

    Reads the provider configuration from ConfigStore and returns the appropriate
    real backend. RAISES `ImageGenNotConfigured` (W-50 — no silent procedural
    fallback) when the selected tier is not fully configured:
    - openai: no API key available (encrypted store or env)
    - openrouter: no OpenRouter key stored
    - comfyui: no base URL set
    - an unknown provider

    Callers handle the exception explicitly (tool → NOT-CONFIGURED outcome; slides
    → image-less degrade), so the UI never advertises fake placeholder art.
    """
    store = ConfigStore()
    config = store.load()
    settings = config.image_gen
    provider = settings.provider

    # For openai, we need a key. base_url is the ORIGIN only — _OpenAIImageBackend.generate
    # appends "/v1/images/generations", so the default must NOT include /v1 (else /v1/v1/…).
    # Tolerate a user pasting a trailing /v1 or slash by stripping it.
    if provider == "openai":
        base_url = (settings.base_url or "https://api.openai.com").rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[: -len("/v1")]
        api_key_env = settings.api_key_env

        if not store.approvals.origin_approved(base_url, "image:openai", api_key_env):
            raise ImageGenNotConfigured(
                "Image generation endpoint is not operator-approved. Save the Image "
                "generation settings to approve this exact origin."
            )
        # Look up the secret from SecretStore by secret-ref id only.
        if api_key_env:
            if not secret_ref_allowed_for_origin(api_key_env, base_url):
                raise ImageGenNotConfigured(
                    "Image generation secret_ref is not allowed for this endpoint origin."
                )
            api_key = resolve_provider_secret(api_key_env, SecretStore())
            if api_key:
                return _OpenAIImageBackend(base_url, api_key, model=settings.model)
        # No key available — NOT configured (no silent placeholder fallback).
        raise ImageGenNotConfigured()

    # For openrouter image gen, the key is the OpenRouter key (reserved "openrouter"
    # SecretStore slot, the same one the LLM router uses), NOT a user-named api_key_env.
    if provider == "openrouter":
        openrouter_base = "https://openrouter.ai/api/v1"
        if not store.approvals.origin_approved(openrouter_base, "image:openrouter", "openrouter"):
            raise ImageGenNotConfigured(
                "OpenRouter image generation origin is not operator-approved."
            )
        api_key = resolve_provider_secret("openrouter", SecretStore())
        # An empty image model is NOT configured: OpenRouter has no usable provider
        # default (every image model is paid + model-specific), and Settings already
        # shows this tier as "unavailable" until a model id is set — so the factory must
        # enforce the same, never silently fall back to a hardcoded model the user never
        # chose (a false affordance: Settings says off, the tool would run anyway).
        if api_key and settings.model.strip():
            # SECURITY: pin the OpenRouter origin. The UI exposes NO endpoint field for
            # this tier, so any persisted base_url can only be stale config left over from
            # another provider — and we must never send the OpenRouter Bearer key to an
            # unintended host. Ignore settings.base_url entirely.
            return _OpenRouterImageBackend(openrouter_base, api_key, model=settings.model)
        # No OpenRouter key stored, or no image model id set — NOT configured.
        raise ImageGenNotConfigured()

    # For comfyui, we need a base_url
    if provider == "comfyui":
        base_url = settings.base_url
        if base_url:
            if not store.approvals.origin_approved(base_url, "image:comfyui", ""):
                raise ImageGenNotConfigured(
                    "ComfyUI endpoint is not operator-approved. Save the Image "
                    "generation settings to approve this exact origin."
                )
            return _ComfyUIBackend(
                base_url,
                model=settings.model,
                workflow_json=settings.workflow_json,
            )
        # No URL configured — NOT configured.
        raise ImageGenNotConfigured()

    # Unknown / unset provider — NOT configured.
    raise ImageGenNotConfigured()


# ---- run() helpers (validation/formatting sub-steps, kept in this facade since
# they exist purely to keep `ImageGenTool.run` itself small and low-branching) --


def _sanitize_base_filename(filename: str) -> str:
    """Sanitize the user-provided base filename: strip directory components and
    fall back to a safe default. The sandbox jails paths, but the filename should
    also be a real file basename so the deliverable UI can show it cleanly."""
    safe_base = os.path.basename(filename)
    if not safe_base or safe_base in {".", ".."}:
        safe_base = "image"
    return safe_base


async def _resolve_output_path(sandbox, safe_base: str, ext: str, seed: int) -> str:
    """W-51 — collision-free naming. The default base is "image", so a build that
    generates several images would otherwise write "image.png" every time and
    silently overwrite the prior deliverable. Probe the sandbox's OWN namespace
    (file_exists resolves inside the box; see boundaries.py:92) and append a
    numeric suffix until the name is free: image.png, image-1.png, image-2.png…
    Bounded so a pathological workspace can never spin forever; if every probe is
    taken we fall back to the per-image seed (and, last resort, a timestamp), both
    of which are effectively unique."""
    out_path = f"{safe_base}.{ext}"
    if not await sandbox.file_exists(out_path):
        return out_path
    for n in range(1, 1001):
        candidate = f"{safe_base}-{n}.{ext}"
        if not await sandbox.file_exists(candidate):
            return candidate
    seed_candidate = f"{safe_base}-{seed}.{ext}"
    if not await sandbox.file_exists(seed_candidate):
        return seed_candidate
    return f"{safe_base}-{int(time.time() * 1000)}.{ext}"


def _detect_actual_format(image_bytes: bytes) -> str | None:
    """Detect the ACTUAL format from the produced bytes — the remote tiers (OpenAI,
    ComfyUI) ignore the requested `fmt` and emit PNG regardless, so trust the bytes,
    not the request. Returns None when the bytes are neither a PNG nor a JPEG."""
    if image_bytes.startswith(_PNG_MAGIC):
        return "png"
    if image_bytes.startswith(_JPEG_MAGIC):
        return "jpeg"
    return None


def _actual_image_dimensions(
    image_bytes: bytes, fallback_width: int, fallback_height: int
) -> tuple[int, int]:
    """Report the ACTUAL produced dimensions, not the requested ones: some backends
    can't honor arbitrary sizes (the OpenAI tier snaps to 1024x1024), so reporting
    the requested width/height would be false metadata. Falls back to the requested
    dimensions if the bytes don't decode (should not happen post magic-bytes check,
    but this must never raise)."""
    from PIL import Image as _PILImage

    try:
        return _PILImage.open(io.BytesIO(image_bytes)).size
    except Exception:  # noqa: BLE001 — fall back to requested dims if decode fails
        return fallback_width, fallback_height


# ---- tool implementation ----------------------------------------------------


class ImageGenTool:
    """Generate an image from a text prompt and write it to the workspace as a
    binary deliverable. Real diffusion comes from the ComfyUI (self-host), OpenAI-
    compatible (paid), or OpenRouter (paid) tier configured in Settings. When none
    is configured the tool returns a NOT-CONFIGURED failure (W-50 — there is no
    bundled placeholder backend)."""

    definition = ToolDef(
        name="image_generate",
        description=(
            "Generate an image from a text prompt and save it to the workspace "
            "as a binary deliverable (PNG by default), surfaced in the "
            "deliverables panel. Requires a configured image backend — ComfyUI "
            "(self-host), an OpenAI-compatible API (paid), or OpenRouter (paid) — "
            "set in Settings → Image generation; if none is configured the tool "
            "fails with a NOT-CONFIGURED message (there is no placeholder fallback). "
            "Use a fixed `seed` for reproducible results. Format: 'png' (lossless, "
            "default) or 'jpeg' (smaller)."
        ),
        args_model=ImageGenArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        # Low risk on the local backend (no network, no model, no shell). The
        # remote tiers (ComfyUI/OpenAI) are network-bound but route through the
        # same binary write path.
        runs_in="sandbox",
        read_only=False,
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    def __init__(self, backend: ImageBackend | None = None) -> None:
        # An EXPLICITLY injected backend (tests) pins the tool to it. In production no
        # backend is injected (registry passes none), so each run() re-reads the saved
        # provider via select_image_backend() — config changes are honored on the NEXT
        # call without a restart, matching the Settings contract (and the TTS tier, which
        # likewise re-reads config per call rather than snapshotting at registry build).
        self._injected = backend

    def execution_scope(self, args: ImageGenArgs) -> str:
        return "sandbox" if self._injected is not None else "in_process"

    def _resolve_backend(self) -> ImageBackend:
        # May raise ImageGenNotConfigured (W-50) when no real tier is configured —
        # callers (run(), backend_name) handle that explicitly.
        return self._injected if self._injected is not None else select_image_backend()

    @property
    def backend_name(self) -> str:
        # Report the LIVE backend (re-resolved from config), not a constructor default,
        # so this stays consistent with what run() actually uses per call. When no tier
        # is configured, say so rather than naming a backend that won't run.
        try:
            return self._resolve_backend().name
        except ImageGenNotConfigured:
            return "not-configured"

    async def run(self, args: ImageGenArgs, ctx: ToolContext) -> ToolOutcome:
        # Resolve the seed. The backend needs an int; if the model didn't pass one,
        # derive it from the prompt (deterministic per prompt) so the round-trip
        # property still holds.
        seed = args.seed if args.seed is not None else _prompt_seed(args.prompt)
        fmt = args.format.lower()
        if fmt not in _SUPPORTED_FORMATS:
            return ToolOutcome(
                success=False,
                content=(
                    f"unsupported format {args.format!r}; supported: {sorted(_SUPPORTED_FORMATS)}"
                ),
                error="unsupported_format",
            )

        # Re-resolve the backend per call so a provider change saved in Settings is
        # honored on the NEXT image_generate (no restart, no stale per-conversation cache).
        # W-50: when no real tier is configured, fail loudly with a Settings pointer —
        # never a silent procedural placeholder.
        try:
            backend = self._resolve_backend()
        except ImageGenNotConfigured as e:
            return ToolOutcome(
                success=False,
                content=str(e),
                error="image_gen_not_configured",
            )

        # ---- generate (the backend hands us raw `bytes`) ---------------
        try:
            image_bytes = backend.generate(
                prompt=args.prompt,
                width=args.width,
                height=args.height,
                seed=seed,
                fmt=fmt,
            )
        except Exception as e:  # noqa: BLE001 — tool failure surfaces as an observation
            return ToolOutcome(
                success=False,
                content=_with_svg_fallback_hint(f"image generation failed ({backend.name}): {e}"),
                error=f"backend_error: {type(e).__name__}",
            )

        # Defensive: the contract is `bytes`. If a future backend ever
        # returns a `bytearray` or `str`, fail loud rather than silently
        # corrupting on write.
        if not isinstance(image_bytes, bytes):
            return ToolOutcome(
                success=False,
                content=_with_svg_fallback_hint(
                    f"backend {backend.name!r} returned "
                    f"{type(image_bytes).__name__}, expected bytes. The "
                    f"binary-write path requires raw bytes — text-mode "
                    f"would corrupt >=0x80 bytes."
                ),
                error="backend_returned_non_bytes",
            )

        # ---- binary-safe write through the sandbox --------------------
        # CRITICAL: this is the F3 / text-mode junction. We pass `bytes`
        # directly to `write_file` (which is typed `bytes`). DO NOT call
        # `.encode("utf-8")` here — PNG/JPEG bytes above 0x7F are common
        # (LZW-compressed pixel data, IDAT chunks, DCT coefficients in
        # JPEG) and a text encode round-trip would silently corrupt them.
        # This is exactly the kind of bug the task brief calls out.
        assert ctx.sandbox is not None, (
            "image_generate requires a sandbox instance for the binary file write"
        )
        # Detect the ACTUAL format from the produced bytes — the remote tiers (OpenAI,
        # ComfyUI) ignore the requested `fmt` and emit PNG regardless, so trust the bytes,
        # not the request. This keeps the extension, the magic-bytes check, the on-disk
        # file, and the reported format all consistent with what was really produced
        # (so `format="jpeg"` against a PNG-only provider yields an honest .png, not a
        # rejected request). PNG and JPEG are the only supported deliverable formats.
        actual_fmt = _detect_actual_format(image_bytes)
        if actual_fmt is None:
            return ToolOutcome(
                success=False,
                content=_with_svg_fallback_hint(
                    f"image_generate: backend produced bytes that are neither a PNG nor "
                    f"a JPEG — refusing to surface a corrupt image as a deliverable. "
                    f"(backend={backend.name})"
                ),
                error="unrecognized_image_format",
            )
        ext = "jpg" if actual_fmt == "jpeg" else "png"
        safe_base = _sanitize_base_filename(args.filename)
        out_path = await _resolve_output_path(ctx.sandbox, safe_base, ext, seed)
        await ctx.sandbox.write_file(out_path, image_bytes)

        # ---- success: short text summary, image bytes NEVER in content -
        # The model should see size + dimensions + format + seed, NOT the
        # image bytes themselves. The bytes live on disk in the sandbox
        # and are referenced by path. Surfacing them in `content` would
        # both bloat the model's context AND risk any downstream
        # text-mode transport mangling the binary.
        magic_hex = image_bytes[:8].hex()
        actual_w, actual_h = _actual_image_dimensions(image_bytes, args.width, args.height)

        # W-50: every reachable backend here is a REAL configured generator (the keyless
        # procedural placeholder was removed; an unconfigured tier raises before this
        # point). So a produced image is always a real generation — no placeholder note.
        return ToolOutcome(
            success=True,
            content=(
                f"Image written: {len(image_bytes)} bytes to {out_path} "
                f"({actual_w}x{actual_h} {actual_fmt.upper()}, "
                f"seed={seed}, backend={backend.name})"
            ),
            artifacts=[out_path],
            structured={
                "path": out_path,
                # Reflect the FINAL written stem (post W-51 disambiguation), not the
                # requested base — so a consumer that rebuilds a name from filename_base
                # lands on the file that was actually written.
                "filename_base": out_path[: -(len(ext) + 1)],
                "format": actual_fmt,
                "width": actual_w,
                "height": actual_h,
                "seed": seed,
                "prompt": args.prompt,
                "backend": backend.name,
                # Every reachable backend is a real, connected generator now.
                "placeholder": False,
                "backend_connected": True,
                "bytes": len(image_bytes),
                # Hex of the first 8 bytes (the magic) — useful for the
                # deliverable panel to render a thumbnail / sanity-check
                # badge without re-reading the file. Never the full
                # image, just the signature.
                "magic_hex": magic_hex,
            },
        )
