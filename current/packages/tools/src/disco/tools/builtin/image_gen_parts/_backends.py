"""Concrete image-gen backend implementations + the binary-format helpers they
share: ``_OpenAIImageBackend``, ``_OpenRouterImageBackend``, ``_ComfyUIBackend``,
plus the PNG/JPEG magic-byte constants and ``_normalize_to_png_or_jpeg`` /
``_raise_image_status`` used by all three.

Extracted from ``image_gen.py`` to reduce module complexity; the public facade
(``image_gen.py``) re-imports every name here unchanged, so every existing
import path (production code AND tests, several of which construct these
backend classes directly or patch ``httpx.Client`` on the facade module) keeps
working exactly as before. This module intentionally does NOT import back from
``image_gen.py`` — the backends are self-contained and only need the third-party
libraries (``httpx``, ``PIL``) they already depended on.
"""

from __future__ import annotations

import io

import httpx

# ---- binary formats ----------------------------------------------------------

# PNG signature: \x89 P N G \r \n \x1a \n (RFC 2083). Eight bytes that
# distinguish a real PNG from a renamed/empty file; the same check the
# acceptance test does.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# JPEG SOI marker: 0xFF 0xD8 (JFIF). Not exhaustive (a real decoder parses the
# segments), but a fast "is this plausibly a JPEG?" sniff for the test.
_JPEG_MAGIC = b"\xff\xd8\xff"

# Hard caps on an untrusted provider image blob (remote APIs return arbitrary bytes).
# A 40 MB encoded ceiling and a 4096x4096 pixel ceiling bound both transport size and
# decode/re-encode memory, so a decompression bomb can't OOM the orchestrator.
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_MAX_IMAGE_PIXELS = 4096 * 4096


def _raise_image_status(response: httpx.Response, provider: str) -> None:
    status = getattr(response, "status_code", 200)
    if not isinstance(status, int):
        response.raise_for_status()
        return
    if status >= 400:
        raise RuntimeError(f"{provider} image endpoint returned HTTP {status}")


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
        }
        # Pin the model when configured (e.g. "gpt-image-1", "dall-e-3"); empty → the
        # provider's default. Optional because many OpenAI-compatible servers expose one model.
        if self._model:
            payload["model"] = self._model
        # response_format is a DALL·E 2/3 (and most OpenAI-compatible-server) field —
        # gpt-image-1 REJECTS it outright (400 unknown_parameter) and returns b64_json
        # unconditionally regardless, so it's never sent for that model. The parse below
        # already reads `b64_json` off the response either way, so gpt-image-1 still works.
        if self._model != "gpt-image-1":
            payload["response_format"] = "b64_json"

        with httpx.Client(timeout=60.0, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                self._endpoint(),
                json=payload,
                headers=headers,
            )
            _raise_image_status(response, self.name)
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

        with httpx.Client(timeout=120.0, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            _raise_image_status(response, self.name)
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

        with httpx.Client(timeout=120.0, trust_env=False, follow_redirects=False) as client:
            # Submit the prompt
            prompt_response = client.post(
                f"{self._base_url}/prompt",
                json={"prompt": workflow},
            )
            _raise_image_status(prompt_response, self.name)
            prompt_data = prompt_response.json()
            prompt_id = prompt_data["prompt_id"]

            # Poll for completion
            import time

            for _ in range(120):  # 2 minutes max
                time.sleep(1)
                history_response = client.get(f"{self._base_url}/history/{prompt_id}")
                _raise_image_status(history_response, self.name)
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
                        _raise_image_status(img_response, self.name)
                        return img_response.content

                    # Generation completed but no images yet - wait a bit more
                    if history[prompt_id].get("status"):
                        status = history[prompt_id]["status"]
                        if status.get("completed"):
                            break

            raise TimeoutError("ComfyUI generation timed out")
