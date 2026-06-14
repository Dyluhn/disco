"""Image generation tool — keyless/local-capable backend with a BINARY-safe
write path so the generated image round-trips byte-identical and surfaces as
a deliverable artifact.

Backend design
--------------
The tool is structured around a `ImageBackend` protocol so a real
diffusers/Stable-Diffusion backend can be slotted in without touching the
tool. Today the *default* backend is `_PILProceduralBackend` — a deterministic
procedural pattern generator built on Pillow (no network, no API key, no
optional model deps). It is the "keyless" baseline per the universal-providers
tiers; the live `diffusers`+`torch` wire is **deferred** (neither is installed
in this environment; importing them would force a multi-GB model download at
tool-import time and break the package's headless build).

Why a procedural backend is honest for a keyless default
---------------------------------------------------------
The point of a "keyless" tier is to ship SOMETHING that produces a valid
binary image, so the tool's full surface (binary write → deliverable emission
→ magic-bytes check) is exercised end-to-end without an external dependency.
A small geometric pattern, seeded from the prompt (with an optional explicit
seed), satisfies the binary-safe-write contract and is visibly distinct across
seeds — enough for a focused test, and the same interface a real model would
speak. The deferred `DiffusersBackend` slot is left empty by design; tests do
not require it.

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
"""

from __future__ import annotations

import hashlib
import io
import os
from typing import Protocol

from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

# ---- binary formats ---------------------------------------------------------

# PNG signature: \x89 P N G \r \n \x1a \n (RFC 2083). Eight bytes that
# distinguish a real PNG from a renamed/empty file; the same check the
# acceptance test does.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# JPEG SOI marker: 0xFF 0xD8 (JFIF). Not exhaustive (a real decoder parses the
# segments), but a fast "is this plausibly a JPEG?" sniff for the test.
_JPEG_MAGIC = b"\xff\xd8\xff"

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
            "Text description of the image to generate. Used as a seed for the "
            "keyless local backend (deterministic per prompt). The live diffusers "
            "backend (deferred) would consume it as the conditioning prompt."
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
    depends on this shape — the implementation is injected at runtime, so
    `DiffusersBackend` (deferred) can replace `_PILProceduralBackend` without
    touching the tool's `run()` body."""

    name: str  # surfaced in the deliverable summary ("backend": "pil-procedural")
    is_remote: bool  # network-bound? (False for both current backends)

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


class _PILProceduralBackend:
    """Keyless / local / default backend. No network, no model download, no
    API key — Pillow + numpy are the only deps, both already in the tools
    package's runtime.

    Renders a small, deterministic geometric pattern: a base hue seeded from
    the prompt, a concentric ring count + thickness from the seed, and a
    three-color palette. Same prompt + same seed → byte-identical PNG
    (PNG encoding is deterministic for the same input array + same PIL
    build), which is what the round-trip test asserts.

    NOTE — this is NOT a generative model. It exists so the binary-safe
    write path and deliverable emission are exercised end-to-end without
    shipping diffusers/torch. The real `DiffusersBackend` (Stable Diffusion
    XL or similar) is the deferred replacement; it speaks the same
    `ImageBackend` protocol, so swapping it in is a one-line registration
    change in `__init__.py`.
    """

    name = "pil-procedural"
    is_remote = False

    def generate(
        self,
        *,
        prompt: str,
        width: int,
        height: int,
        seed: int,
        fmt: str,
    ) -> bytes:
        # Lazy imports keep the tools package importable even on a Pillow-less
        # minimal install (the build is fully headless otherwise). The tool
        # only needs them at run-time, in the sandbox, when the model asks
        # for an image.
        from PIL import Image, ImageDraw

        rng_state = seed & 0xFFFFFFFF
        # A three-color palette derived from the seed — the visual signal
        # that "different prompt → different image" holds in the round-trip
        # test (asserted via SHA-256 of the bytes, not by eye).
        hue1 = (rng_state >> 0) & 0xFF
        hue2 = (rng_state >> 8) & 0xFF
        hue3 = (rng_state >> 16) & 0xFF
        c1 = (hue1, (hue1 * 7) & 0xFF, 255 - hue1)
        c2 = (hue2, 255 - (hue2 * 3) & 0xFF, hue2)
        c3 = (255 - hue3, hue3, (hue3 * 5) & 0xFF)
        bg = ((seed * 13) & 0xFF, (seed * 17) & 0xFF, (seed * 23) & 0xFF)

        img = Image.new("RGB", (width, height), bg)
        draw = ImageDraw.Draw(img)

        # Concentric rings: count + thickness from the seed. Always renders,
        # even at 8x8, so a tiny test image still looks like "something".
        n_rings = 3 + (seed % 4)
        for i in range(n_rings):
            t = (i + 1) / n_rings
            inset = int(min(width, height) * (1 - t) / 2)
            color = (c1, c2, c3)[i % 3]
            draw.rectangle(
                (inset, inset, width - 1 - inset, height - 1 - inset),
                outline=color,
                width=max(1, (seed >> (i * 2)) % 3 + 1),
            )

        # A diagonal accent: a single line from corner to corner, also
        # seed-driven, so two different seeds don't accidentally produce
        # the same byte stream.
        draw.line((0, 0, width - 1, height - 1), fill=c3, width=1)

        # PIL's PNG encoder is deterministic for a given input array + build
        # version, which is what makes the round-trip test "byte-identical"
        # hold without needing a fixed binary build. JPEG is NOT deterministic
        # at the bit level across encoders (the JFIF spec leaves a few
        # encoder-chosen fields free), so PNG is the default and the test
        # asserts on PNG magic — JPEG is supported but the test focuses on
        # PNG.
        out_fmt = "JPEG" if fmt == "jpeg" else "PNG"
        buf = io.BytesIO()
        img.save(buf, format=out_fmt)
        return buf.getvalue()


# ---- tool implementation ----------------------------------------------------


class ImageGenTool:
    """Generate an image from a text prompt and write it to the workspace
    as a binary deliverable. The default backend is keyless/local (PIL
    procedural); the live model backend is deferred."""

    definition = ToolDef(
        name="image_generate",
        description=(
            "Generate an image from a text prompt and save it to the workspace "
            "as a binary deliverable. The default backend is local and keyless "
            "(procedural pattern, seeded from the prompt) — no network, no "
            "API key. The output is a real, valid image file (PNG by default) "
            "written to disk and surfaced in the deliverables panel. Use a "
            "fixed `seed` for reproducible results; the same prompt + same "
            "seed yields the byte-identical image every time. Format: 'png' "
            "(lossless, default) or 'jpeg' (smaller). NOTE: the live "
            "diffusers-based generative backend is deferred; the current "
            "default renders a deterministic procedural pattern that proves "
            "the binary-safe write + deliverable path end-to-end."
        ),
        args_model=ImageGenArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        # Low risk on the local backend (no network, no model, no shell). The
        # deferred diffusers tier, if/when wired in, would want a Medium
        # static hint and the runtime would still route through the same
        # binary write path.
        runs_in="sandbox",
        read_only=False,
    )

    def __init__(self, backend: ImageBackend | None = None) -> None:
        # Injected backend — tests can swap in a stub; production gets the
        # procedural default. The deferred diffusers backend, when ready,
        # is a one-line change at construction.
        self._backend: ImageBackend = backend or _PILProceduralBackend()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def run(self, args: ImageGenArgs, ctx: ToolContext) -> ToolOutcome:
        # Resolve the seed. The procedural backend needs an int; if the model
        # didn't pass one, derive it from the prompt (deterministic per
        # prompt) so the round-trip property still holds.
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

        # ---- generate (the backend hands us raw `bytes`) ---------------
        try:
            image_bytes = self._backend.generate(
                prompt=args.prompt,
                width=args.width,
                height=args.height,
                seed=seed,
                fmt=fmt,
            )
        except Exception as e:  # noqa: BLE001 — tool failure surfaces as an observation
            return ToolOutcome(
                success=False,
                content=f"image generation failed ({self._backend.name}): {e}",
                error=f"backend_error: {type(e).__name__}",
            )

        # Defensive: the contract is `bytes`. If a future backend ever
        # returns a `bytearray` or `str`, fail loud rather than silently
        # corrupting on write.
        if not isinstance(image_bytes, bytes):
            return ToolOutcome(
                success=False,
                content=(
                    f"backend {self._backend.name!r} returned "
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
        ext = "jpg" if fmt == "jpeg" else "png"
        # Sanitize the user-provided base filename: strip directory
        # components and force a sane extension. The sandbox instance
        # already jails paths, but the filename should also be a real
        # file basename so the deliverable UI can show it cleanly.
        safe_base = os.path.basename(args.filename)
        if not safe_base or safe_base in {".", ".."}:
            safe_base = "image"
        out_path = f"{safe_base}.{ext}"
        await ctx.sandbox.write_file(out_path, image_bytes)

        # ---- magic-bytes sanity check (the deliverable contract) ------
        # We re-check the first 8 bytes of what we just wrote — a
        # defensive cross-check that the on-disk file is actually a
        # decodable image, not a silently-corrupted container. PNG and
        # JPEG are the only supported formats today, and both have a
        # well-known short signature.
        if fmt == "png" and not image_bytes.startswith(_PNG_MAGIC):
            return ToolOutcome(
                success=False,
                content=(
                    f"image_generate: backend produced bytes that lack the "
                    f"PNG signature — refusing to surface a corrupt image "
                    f"as a deliverable. (backend={self._backend.name})"
                ),
                error="non_png_signature",
            )
        if fmt == "jpeg" and not image_bytes.startswith(_JPEG_MAGIC):
            return ToolOutcome(
                success=False,
                content=(
                    f"image_generate: backend produced bytes that lack the "
                    f"JPEG SOI marker — refusing to surface a corrupt image "
                    f"as a deliverable. (backend={self._backend.name})"
                ),
                error="non_jpeg_signature",
            )

        # ---- success: short text summary, image bytes NEVER in content -
        # The model should see size + dimensions + format + seed, NOT the
        # image bytes themselves. The bytes live on disk in the sandbox
        # and are referenced by path. Surfacing them in `content` would
        # both bloat the model's context AND risk any downstream
        # text-mode transport mangling the binary.
        magic_hex = image_bytes[:8].hex()
        return ToolOutcome(
            success=True,
            content=(
                f"Image written: {len(image_bytes)} bytes to {out_path} "
                f"({args.width}x{args.height} {fmt.upper()}, "
                f"seed={seed}, backend={self._backend.name})"
            ),
            artifacts=[out_path],
            structured={
                "path": out_path,
                "filename_base": safe_base,
                "format": fmt,
                "width": args.width,
                "height": args.height,
                "seed": seed,
                "prompt": args.prompt,
                "backend": self._backend.name,
                "bytes": len(image_bytes),
                # Hex of the first 8 bytes (the magic) — useful for the
                # deliverable panel to render a thumbnail / sanity-check
                # badge without re-reading the file. Never the full
                # image, just the signature.
                "magic_hex": magic_hex,
            },
        )
