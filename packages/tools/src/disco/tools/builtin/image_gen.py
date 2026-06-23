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
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from typing import Protocol

import httpx
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.secrets import SecretStore
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

# W-50: the operator-facing "no real image backend is configured" message, shared by
# every caller (the tool's NOT-CONFIGURED outcome, the slides degrade path, probes).
_NOT_CONFIGURED_MSG = (
    "Image generation isn't configured — set ComfyUI (base URL), an OpenAI-compatible "
    "API (base URL + key), or OpenRouter (store the OpenRouter key) in "
    "Settings → Image generation."
)


class ImageGenNotConfigured(RuntimeError):
    """Raised by `select_image_backend()` when the selected image-gen tier is not
    fully configured (W-50). Replaces the old silent fallback to a procedural
    placeholder: callers handle it explicitly (tool → NOT-CONFIGURED outcome;
    slides → image-less degrade) instead of shipping fake art."""

    def __init__(self, message: str = _NOT_CONFIGURED_MSG) -> None:
        super().__init__(message)


# ---- binary formats ---------------------------------------------------------

# PNG signature: \x89 P N G \r \n \x1a \n (RFC 2083). Eight bytes that
# distinguish a real PNG from a renamed/empty file; the same check the
# acceptance test does.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# JPEG SOI marker: 0xFF 0xD8 (JFIF). Not exhaustive (a real decoder parses the
# segments), but a fast "is this plausibly a JPEG?" sniff for the test.
_JPEG_MAGIC = b"\xff\xd8\xff"

_SUPPORTED_FORMATS: frozenset[str] = frozenset({"png", "jpeg"})


# Hard caps on an untrusted provider image blob (remote APIs return arbitrary bytes).
# A 40 MB encoded ceiling and a 4096x4096 pixel ceiling bound both transport size and
# decode/re-encode memory, so a decompression bomb can't OOM the orchestrator.
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_MAX_IMAGE_PIXELS = 4096 * 4096


def _normalize_to_png_or_jpeg(raw: bytes) -> bytes:
    """Return `raw` unchanged if it's already PNG/JPEG, else decode it (e.g. WEBP,
    which ImageRouter returns by default) and re-encode as PNG. Keeps the deliverable
    contract (PNG/JPEG only) for any provider without a provider-specific request field.

    Hardened against a hostile/oversized provider response: rejects blobs over
    `_MAX_IMAGE_BYTES` and images over `_MAX_IMAGE_PIXELS` BEFORE the expensive
    convert/decode (PIL reads the header dimensions without decompressing pixels).
    Genuinely-undecodable bytes are returned unchanged so the caller's magic-bytes guard
    rejects them with its clear error rather than masking the failure."""
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"provider image is too large ({len(raw)} bytes > {_MAX_IMAGE_BYTES}) — refusing"
        )
    if raw.startswith(_PNG_MAGIC) or raw.startswith(_JPEG_MAGIC):
        return raw
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(raw))  # lazy: reads header (size) without decoding
    except Exception:  # noqa: BLE001 — undecodable → let the magic-bytes guard reject it
        return raw
    w, h = img.size
    if w * h > _MAX_IMAGE_PIXELS:
        raise ValueError(
            f"provider image dimensions too large ({w}x{h} > {_MAX_IMAGE_PIXELS}px) — refusing"
        )
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()

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


class _OpenAIImageBackend:
    """OpenAI-compatible images API backend (OpenAI, ImageRouter, Together, Azure …).

    Posts to `{base_url}/v1/images/generations` for a bare origin, OR to `base_url`
    verbatim when it already names the full endpoint path (e.g. ImageRouter's
    `https://api.imagerouter.io/v1/openai/images/generations`, which has an extra
    `/openai/` segment that the default suffix can't express). Uses `api_key_env`
    to look up the secret from the encrypted store. Never logs or echoes the key —
    only presence/absence is surfaced in error messages.

    Whatever container the provider returns (PNG, JPEG, or — like ImageRouter's
    default — WEBP) is normalized to PNG bytes so the deliverable contract
    (PNG/JPEG only) holds for every provider without sending a provider-specific
    `output_format` field that a different provider might 400 on.

    This is a PAID backend: it requires an API key to be configured via Settings.
    Without a key, image generation is NOT CONFIGURED (the factory raises).
    """

    name = "openai-compatible"
    is_remote = True

    def __init__(self, base_url: str, api_key: str | None, *, model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def _endpoint(self) -> str:
        # A configured base_url whose PATH already ends at the images route (incl.
        # ImageRouter's `/v1/openai/images/generations`) is used verbatim; a bare origin
        # gets the standard OpenAI suffix appended. Match on the parsed path suffix, not
        # a raw substring, so a query string like `?next=/images/generations` can't
        # masquerade as a full endpoint.
        from urllib.parse import urlsplit

        if urlsplit(self._base_url).path.rstrip("/").endswith("/images/generations"):
            return self._base_url
        return f"{self._base_url}/v1/images/generations"

    def generate(
        self,
        *,
        prompt: str,
        width: int,
        height: int,
        seed: int,
        fmt: str,
    ) -> bytes:
        # Map our format to the API's response_format
        # Both PNG and JPEG use b64_json for direct byte return
        response_format = "b64_json"

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # OpenAI Images only accepts a fixed set of sizes (not arbitrary WxH), and those
        # sets DIFFER by model — DALL·E 3 does 1792x1024 / 1024x1792 while gpt-image-1 does
        # 1536x1024 / 1024x1536. The ONLY size common to DALL·E 2/3 and gpt-image-1 is
        # 1024x1024, so use it unconditionally: a request never 400s on the configured
        # model. (The requested width/height are still reported in the deliverable summary.)
        payload: dict[str, object] = {
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "response_format": response_format,
        }
        # Pin the model when configured (e.g. "gpt-image-1", "dall-e-3"); empty → the
        # provider's default. Optional because many OpenAI-compatible servers expose one model.
        if self._model:
            payload["model"] = self._model

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                self._endpoint(),
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        # The response is {"data": [{"b64_json": "...", ...}]}
        b64_data = data["data"][0].get("b64_json")
        if b64_data:
            import base64

            raw = base64.b64decode(b64_data)
            return _normalize_to_png_or_jpeg(raw)

        # We request response_format="b64_json" so a compliant provider returns inline
        # bytes (above). We deliberately do NOT fetch a provider-returned `url`: doing so
        # would GET an attacker-influenced URL from the orchestrator host (SSRF), and a
        # DNS-rebinding host defeats any pre-flight IP check. Fail loud instead.
        if data["data"][0].get("url"):
            raise ValueError(
                "OpenAI images API returned a URL instead of inline b64_json. Disco "
                "does not fetch provider URLs (SSRF risk). Use a provider that honors "
                "response_format=b64_json (OpenAI gpt-image-1 / DALL·E do)."
            )
        raise ValueError("OpenAI images API returned no image data")


class _OpenRouterImageBackend:
    """OpenRouter image generation via the /chat/completions endpoint.

    OpenRouter does NOT expose an OpenAI `/v1/images/generations` route — image
    models are driven through chat completions with `modalities: ["image","text"]`,
    and the result comes back as a base64 data URL at
    `choices[0].message.images[0].image_url.url`. This is a distinct wire shape from
    `_OpenAIImageBackend`, hence its own backend.

    PAID: every OpenRouter image model is paid (no free tier), so the account needs
    credits. Uses the OpenRouter key (the reserved "openrouter" SecretStore slot /
    DISCO_OPENROUTER_API_KEY) — the same key the LLM router uses. Returned bytes are
    normalized to PNG. A provider-returned http(s) URL (not an inline data URL) is
    REFUSED, never fetched (SSRF), matching the OpenAI backend's stance.
    """

    name = "openrouter-image"
    is_remote = True

    def __init__(self, base_url: str, api_key: str | None, *, model: str = "") -> None:
        self._base_url = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        self._api_key = api_key
        # Default to the cheapest current image model so it works once credits exist.
        self._model = model or "google/gemini-2.5-flash-image"

    def generate(
        self,
        *,
        prompt: str,
        width: int,
        height: int,
        seed: int,
        fmt: str,
    ) -> bytes:
        import base64

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # OpenRouter attribution headers (recommended, not required).
        headers["HTTP-Referer"] = "https://disco.local"
        headers["X-Title"] = "Disco"

        payload: dict[str, object] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
        }

        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        try:
            images = data["choices"][0]["message"].get("images") or []
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"OpenRouter response had no choices/message: {e}") from e
        if not images:
            raise ValueError(
                "OpenRouter returned no image — the model may not support image output, "
                "or the account is out of credits (every OpenRouter image model is paid)."
            )

        url = images[0].get("image_url", {}).get("url", "")
        if url.startswith("data:"):
            # data:image/png;base64,<...> — split off the b64 payload and decode.
            try:
                b64 = url.split(",", 1)[1]
            except IndexError as e:
                raise ValueError("OpenRouter data URL was malformed (no comma)") from e
            return _normalize_to_png_or_jpeg(base64.b64decode(b64))

        # A remote http(s) URL is attacker-influenced: GETting it from the orchestrator
        # host is SSRF (DNS-rebinding defeats a pre-flight IP check). Refuse, never fetch.
        if url.startswith("http"):
            raise ValueError(
                "OpenRouter returned a remote image URL instead of an inline data URL. "
                "Disco does not fetch provider URLs (SSRF risk)."
            )
        raise ValueError("OpenRouter image had no usable image_url data")


# A light, universally-helpful SDXL/SD negative for the built-in default graph.
# (FLUX and other shapes that don't want a negative use the workflow_json override.)
_DEFAULT_COMFY_NEGATIVE = "lowres, blurry, jpeg artifacts, watermark, text, signature"

# Diffusion models render garbage below their native resolution. The image_generate
# tool defaults to 64x64 (fine for the procedural pattern, useless for SDXL), so any
# sub-512 request is snapped up to a sane square; everything is rounded to a multiple
# of 8 (SDXL/SD latent constraint). Reported dimensions come from the PRODUCED bytes
# (run() re-reads PIL size), so this snap never lies about the result.
_COMFY_MIN_DIMENSION = 512
_COMFY_DEFAULT_DIMENSION = 1024


def _comfy_snap_dim(value: int) -> int:
    v = value if value >= _COMFY_MIN_DIMENSION else _COMFY_DEFAULT_DIMENSION
    return max(_COMFY_MIN_DIMENSION, (v // 8) * 8)


class _ComfyUIBackend:
    """Self-hosted ComfyUI graph API backend.

    Connects to a local/network ComfyUI instance via its REST API. Submits a
    graph to `/prompt`, polls `/history/{id}` until the image is ready, then
    fetches it from `/view`. Keyless (assumes local/network access), but needs
    a running ComfyUI.

    The graph is DATA, not code. Real installs differ in shape:
    - SDXL / SD1.5 / Pony / Illustrious embed CLIP + VAE in the checkpoint
      (`CheckpointLoaderSimple` → MODEL[0], CLIP[1], VAE[2]).
    - FLUX / SD3 use a separate dual-CLIP loader + `UNETLoader` + a standalone VAE.
    So there is NO single hardcoded graph that works everywhere. This backend:
    - ships a correct **SDXL/SD default** (covers the overwhelming majority of
      local checkpoints, incl. Illustrious-XL), and
    - accepts a power-user **workflow_json template** (a ComfyUI "Save (API Format)"
      export) with `%prompt%`, `%negative%`, `%seed%`, `%width%`, `%height%`,
      `%ckpt%` tokens, which makes ANY model shape work without code changes.
    """

    name = "comfyui"
    is_remote = True

    def __init__(self, base_url: str, *, model: str = "", workflow_json: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        # The checkpoint filename for the DEFAULT graph. No bogus default: an
        # invented name (the old "flux1-dev.safetensors") is a false affordance —
        # it 404s on a box that doesn't have that exact file. Empty → fail loud
        # with a Settings pointer (unless a workflow_json template pins its own).
        self._ckpt = model.strip()
        self._workflow_json = workflow_json.strip()

    def _build_workflow(
        self, *, prompt: str, negative: str, width: int, height: int, seed: int
    ) -> dict:
        """Return the ComfyUI graph to submit: the user's template (token-substituted)
        when configured, else the built-in SDXL/SD default."""
        if self._workflow_json:
            # If the template references the checkpoint via %ckpt% but no Checkpoint is
            # set, fail HERE with the Settings pointer rather than substituting "" and
            # letting ComfyUI 404 on an empty model name deep in the graph.
            if "%ckpt%" in self._workflow_json and not self._ckpt:
                raise ValueError(
                    "Your custom workflow uses %ckpt% but no checkpoint is set "
                    "(Settings → Image generation → Checkpoint)."
                )
            return self._render_template(
                prompt=prompt, negative=negative, width=width, height=height, seed=seed
            )
        if not self._ckpt:
            raise ValueError(
                "ComfyUI provider needs a checkpoint filename that exists on your "
                "ComfyUI install (Settings → Image generation → Checkpoint), or a "
                "custom workflow (API format) that pins its own model."
            )
        # The PROVEN-WORKING SDXL/SD graph (live-verified on the R9700, 2026-06-19):
        # CLIP + VAE from the checkpoint, positive+negative encode, KSampler
        # euler_ancestral / cfg 6 / 26 steps / normal, VAE decode, save.
        return {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": self._ckpt},
                "_meta": {"title": "Load Checkpoint"},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["1", 1]},
                "_meta": {"title": "Positive"},
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative, "clip": ["1", 1]},
                "_meta": {"title": "Negative"},
            },
            "4": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
                "_meta": {"title": "Empty Latent Image"},
            },
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": 26,
                    "cfg": 6.0,
                    "sampler_name": "euler_ancestral",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["1", 0],
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "latent_image": ["4", 0],
                },
                "_meta": {"title": "KSampler"},
            },
            "6": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
                "_meta": {"title": "VAE Decode"},
            },
            "7": {
                "class_type": "SaveImage",
                "inputs": {"images": ["6", 0], "filename_prefix": "disco-image"},
                "_meta": {"title": "Save Image"},
            },
        }

    def _render_template(
        self, *, prompt: str, negative: str, width: int, height: int, seed: int
    ) -> dict:
        """Substitute the placeholder tokens in a user-supplied ComfyUI API-format
        graph and parse it. String tokens (`%prompt%`, `%negative%`, `%ckpt%`) are
        JSON-escaped and substituted INSIDE the template's existing quotes; numeric
        tokens (`%seed%`, `%width%`, `%height%`) are substituted where a bare number
        is expected. We substitute on the raw text (not a parsed tree) so a token can
        appear at any node, then json.loads the result and fail loud if it's malformed."""
        import json
        import re

        def esc(s: str) -> str:
            # json.dumps wraps in quotes + escapes; strip the outer quotes so the
            # escaped body lands inside the template's own "..." — a prompt with
            # quotes/newlines/backslashes can't break out of its string or inject nodes.
            return json.dumps(s)[1:-1]

        subs = {
            "prompt": esc(prompt),
            "negative": esc(negative),
            "ckpt": esc(self._ckpt),
            "seed": str(int(seed)),
            "width": str(int(width)),
            "height": str(int(height)),
        }
        # SINGLE pass: re.sub never re-scans its own replacements, so a prompt that
        # literally contains another token (e.g. the text "%seed%") is NOT re-substituted.
        # A naive chained .replace() would bleed the seed into such a prompt.
        rendered = re.sub(
            r"%(prompt|negative|ckpt|seed|width|height)%",
            lambda m: subs[m.group(1)],
            self._workflow_json,
        )
        try:
            graph = json.loads(rendered)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"ComfyUI custom workflow is not valid JSON after token substitution: {e}. "
                "Paste a ComfyUI 'Save (API Format)' export and use the %prompt%, %seed%, "
                "%width%, %height%, %negative%, %ckpt% tokens."
            ) from e
        if not isinstance(graph, dict) or not graph:
            raise ValueError(
                "ComfyUI custom workflow must be a non-empty JSON object of nodes "
                "(ComfyUI 'Save (API Format)' shape)."
            )
        return graph

    def generate(
        self,
        *,
        prompt: str,
        width: int,
        height: int,
        seed: int,
        fmt: str,
    ) -> bytes:
        # Snap sub-native dimensions up so a real diffusion model doesn't render
        # garbage from the tool's 64x64 procedural default.
        gen_w = _comfy_snap_dim(width)
        gen_h = _comfy_snap_dim(height)
        workflow = self._build_workflow(
            prompt=prompt,
            negative=_DEFAULT_COMFY_NEGATIVE,
            width=gen_w,
            height=gen_h,
            seed=seed,
        )

        with httpx.Client(timeout=120.0) as client:
            # Submit the prompt
            prompt_response = client.post(
                f"{self._base_url}/prompt",
                json={"prompt": workflow},
            )
            prompt_response.raise_for_status()
            prompt_data = prompt_response.json()
            prompt_id = prompt_data["prompt_id"]

            # Poll for completion
            import time

            for _ in range(120):  # 2 minutes max
                time.sleep(1)
                history_response = client.get(f"{self._base_url}/history/{prompt_id}")
                history_response.raise_for_status()
                history = history_response.json()

                if prompt_id in history:
                    # Check if outputs exist
                    outputs = history[prompt_id].get("outputs", {})
                    # Custom graphs can emit BOTH preview/temp images (PreviewImage,
                    # type="temp") and final saved images (SaveImage, type="output").
                    # Collect every produced image, then prefer a real saved "output"
                    # over a "temp" preview so we return the final render, not a thumbnail.
                    all_images = [
                        img
                        for node_output in outputs.values()
                        for img in node_output.get("images", [])
                    ]
                    if all_images:
                        image_info = next(
                            (i for i in all_images if i.get("type") == "output"),
                            all_images[0],
                        )
                        img_response = client.get(
                            f"{self._base_url}/view",
                            params={
                                "filename": image_info["filename"],
                                "subfolder": image_info.get("subfolder", ""),
                                "type": image_info.get("type", "output"),
                            },
                        )
                        img_response.raise_for_status()
                        return img_response.content

                    # Generation completed but no images yet - wait a bit more
                    if history[prompt_id].get("status"):
                        status = history[prompt_id]["status"]
                        if status.get("completed"):
                            break

            raise TimeoutError("ComfyUI generation timed out")


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
    config = ConfigStore().load()
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

        # Look up the secret: encrypted SecretStore first, then os.environ — the same
        # resolution order the TTS paid tier uses (report_audio.py), so an env-configured
        # key works and the UI's "secret/env-var name" affordance is honest.
        if api_key_env:
            api_key = SecretStore().get_secret(api_key_env) or os.environ.get(api_key_env)
            if api_key:
                return _OpenAIImageBackend(base_url, api_key, model=settings.model)
        # No key available — NOT configured (no silent placeholder fallback).
        raise ImageGenNotConfigured()

    # For openrouter image gen, the key is the OpenRouter key (reserved "openrouter"
    # SecretStore slot, the same one the LLM router uses), NOT a user-named api_key_env.
    if provider == "openrouter":
        from disco.core.llm.secrets import (
            OPENROUTER_API_KEY_ENV,
            OPENROUTER_API_KEY_ENV_LEGACY,
        )

        api_key = (
            SecretStore().get_openrouter_key()
            or os.environ.get(OPENROUTER_API_KEY_ENV)
            or os.environ.get(OPENROUTER_API_KEY_ENV_LEGACY)
        )
        if api_key:
            # SECURITY: pin the OpenRouter origin. The UI exposes NO endpoint field for
            # this tier, so any persisted base_url can only be stale config left over from
            # another provider — and we must never send the OpenRouter Bearer key to an
            # unintended host. Ignore settings.base_url entirely.
            return _OpenRouterImageBackend(
                "https://openrouter.ai/api/v1", api_key, model=settings.model
            )
        # No OpenRouter key stored — NOT configured.
        raise ImageGenNotConfigured()

    # For comfyui, we need a base_url
    if provider == "comfyui":
        base_url = settings.base_url
        if base_url:
            return _ComfyUIBackend(
                base_url,
                model=settings.model,
                workflow_json=settings.workflow_json,
            )
        # No URL configured — NOT configured.
        raise ImageGenNotConfigured()

    # Unknown / unset provider — NOT configured.
    raise ImageGenNotConfigured()


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
    )

    def __init__(self, backend: ImageBackend | None = None) -> None:
        # An EXPLICITLY injected backend (tests) pins the tool to it. In production no
        # backend is injected (registry passes none), so each run() re-reads the saved
        # provider via select_image_backend() — config changes are honored on the NEXT
        # call without a restart, matching the Settings contract (and the TTS tier, which
        # likewise re-reads config per call rather than snapshotting at registry build).
        self._injected = backend

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
                    f"unsupported format {args.format!r}; supported: "
                    f"{sorted(_SUPPORTED_FORMATS)}"
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
                content=f"image generation failed ({backend.name}): {e}",
                error=f"backend_error: {type(e).__name__}",
            )

        # Defensive: the contract is `bytes`. If a future backend ever
        # returns a `bytearray` or `str`, fail loud rather than silently
        # corrupting on write.
        if not isinstance(image_bytes, bytes):
            return ToolOutcome(
                success=False,
                content=(
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
        if image_bytes.startswith(_PNG_MAGIC):
            actual_fmt = "png"
        elif image_bytes.startswith(_JPEG_MAGIC):
            actual_fmt = "jpeg"
        else:
            return ToolOutcome(
                success=False,
                content=(
                    f"image_generate: backend produced bytes that are neither a PNG nor "
                    f"a JPEG — refusing to surface a corrupt image as a deliverable. "
                    f"(backend={backend.name})"
                ),
                error="unrecognized_image_format",
            )
        ext = "jpg" if actual_fmt == "jpeg" else "png"
        # Sanitize the user-provided base filename: strip directory components and force a
        # sane extension. The sandbox jails paths, but the filename should also be a real
        # file basename so the deliverable UI can show it cleanly.
        safe_base = os.path.basename(args.filename)
        if not safe_base or safe_base in {".", ".."}:
            safe_base = "image"
        # W-51 — collision-free naming. The default base is "image", so a build that
        # generates several images would otherwise write "image.png" every time and
        # silently overwrite the prior deliverable. Probe the sandbox's OWN namespace
        # (file_exists resolves inside the box; see boundaries.py:92) and append a
        # numeric suffix until the name is free: image.png, image-1.png, image-2.png…
        # Bounded so a pathological workspace can never spin forever; if every probe is
        # taken we fall back to the per-image seed (and, last resort, a timestamp), both
        # of which are effectively unique.
        out_path = f"{safe_base}.{ext}"
        if await ctx.sandbox.file_exists(out_path):
            chosen: str | None = None
            for n in range(1, 1001):
                candidate = f"{safe_base}-{n}.{ext}"
                if not await ctx.sandbox.file_exists(candidate):
                    chosen = candidate
                    break
            if chosen is None:
                seed_candidate = f"{safe_base}-{seed}.{ext}"
                if not await ctx.sandbox.file_exists(seed_candidate):
                    chosen = seed_candidate
                else:
                    chosen = f"{safe_base}-{int(time.time() * 1000)}.{ext}"
            out_path = chosen
        await ctx.sandbox.write_file(out_path, image_bytes)

        # ---- success: short text summary, image bytes NEVER in content -
        # The model should see size + dimensions + format + seed, NOT the
        # image bytes themselves. The bytes live on disk in the sandbox
        # and are referenced by path. Surfacing them in `content` would
        # both bloat the model's context AND risk any downstream
        # text-mode transport mangling the binary.
        magic_hex = image_bytes[:8].hex()
        # Report the ACTUAL produced dimensions, not the requested ones: some backends
        # can't honor arbitrary sizes (the OpenAI tier snaps to 1024x1024), so reporting
        # args.width/height would be false metadata and silently swallow the aspect ratio.
        from PIL import Image as _PILImage

        try:
            actual_w, actual_h = _PILImage.open(io.BytesIO(image_bytes)).size
        except Exception:  # noqa: BLE001 — fall back to requested dims if decode fails
            actual_w, actual_h = args.width, args.height

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
