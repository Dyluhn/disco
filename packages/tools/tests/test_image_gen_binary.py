"""D9 — image_generate tool: binary-safe write + byte-identical round-trip.

Acceptance (per the D9 brief):
  * The tool writes a valid (non-corrupt) image file — magic-bytes check
    (e.g. PNG \\x89PNG…) — and emits a deliverable.
  * The bytes round-trip byte-identical: what the backend hands the tool
    is byte-for-byte what lands in the sandbox.
  * The binary-safe write path is exercised end-to-end through the sandbox
    write API (the same `ctx.sandbox.write_file(path, bytes)` path used by
    audio_overview and slides), with NO text-mode round-trip.
  * F3's read-before-write guard is preserved: the image-gen tool does not
    go through `FileWriteTool` and never consults the F3 tracker.
  * Image bytes NEVER appear in the ToolOutcome text content (the brief's
    "leak into the text context" guardrail).

Backend note: in-process `diffusers`+`torch` Stable-Diffusion was SCRAPPED
(it would pin multi-GB of weights to the app process and break the 8 GB ship
target; ComfyUI gives the local-GPU path out-of-process). W-50 removed the
bundled `_PILProceduralBackend` placeholder, so these contract tests inject a
tiny `_FakePNGBackend` (real, deterministic PNG bytes) to exercise the
binary-write + deliverable + magic-bytes path without an external service.
"""

from __future__ import annotations

import hashlib
import io

import httpx
import pytest
from disco.core.llm import ModelExecutionPolicy
from disco.tools.anatomy import ToolContext
from disco.tools.builtin import ImageGenTool, build_default_registry
from disco.tools.builtin.image_gen import (
    _PNG_MAGIC,
    ImageGenArgs,
    ImageGenNotConfigured,
    _ComfyUIBackend,
    _OpenAIImageBackend,
    _prompt_seed,
    select_image_backend,
)
from disco.tools.registry import agent_scope, research_scope
from disco.tools.secrets import CapabilityBroker
from PIL import Image

# ---- fakes ------------------------------------------------------------------


class _FakeSandbox:
    """In-memory sandbox that records every write (path, bytes) for the
    round-trip assertion. Mirrors the FakeSandboxInstance in conftest.py,
    but slimmed to just the surface the image-gen tool touches."""

    def __init__(self, existing: dict[str, bytes] | None = None) -> None:
        self._fs: dict[str, bytes] = dict(existing or {})
        self.writes: list[tuple[str, bytes]] = []

    async def read_file(self, path: str) -> bytes:
        if path not in self._fs:
            raise FileNotFoundError(f"no such file: {path}")
        return self._fs[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self._fs[path] = data
        self.writes.append((path, data))

    async def list_dir(self, path: str) -> list[str]:
        return sorted(self._fs)

    async def file_exists(self, path: str) -> bool:
        return path in self._fs


def _ctx(sandbox: _FakeSandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="conv-d9",
    )


class _FakePNGBackend:
    """A real-bytes stand-in for the deleted procedural backend (W-50). Renders a
    deterministic small image (same prompt+dims+seed+fmt → byte-identical output, a
    different seed/prompt → different bytes) so the binary-write, round-trip, and
    W-51 disambiguation contracts are exercised end-to-end without a network service."""

    name = "fake-png"
    is_remote = True

    def generate(self, *, prompt: str, width: int, height: int, seed: int, fmt: str) -> bytes:
        digest = hashlib.sha256(f"{prompt}|{seed}".encode()).digest()
        img = Image.new("RGB", (width, height), (digest[0], digest[1], digest[2]))
        img.putpixel((0, 0), (digest[3], digest[4], digest[5]))
        buf = io.BytesIO()
        img.save(buf, format="JPEG" if fmt == "jpeg" else "PNG")
        return buf.getvalue()


# ---- backend sanity (no tool plumbing) -------------------------------------


def test_fake_png_backend_produces_valid_png():
    """The fake test backend returns a PNG whose first 8 bytes match the RFC 2083
    signature, and PIL can fully decode it — proving the contract tests below run
    against real, non-corrupt image bytes."""
    backend = _FakePNGBackend()
    raw = backend.generate(prompt="a sunset over the sea", width=32, height=32, seed=42, fmt="png")
    assert isinstance(raw, bytes), "backend must return raw bytes, not str"
    assert raw.startswith(_PNG_MAGIC), f"first 8 bytes should be the PNG signature, got {raw[:8]!r}"
    img = Image.open(io.BytesIO(raw))
    img.load()  # forces a full decode — would raise on a corrupt PNG
    assert img.format == "PNG"
    assert img.size == (32, 32)
    # Deterministic per (prompt, seed); different seed → different bytes.
    again = backend.generate(prompt="a sunset", width=32, height=32, seed=42, fmt="png")
    repeat = backend.generate(prompt="a sunset", width=32, height=32, seed=42, fmt="png")
    assert again == repeat
    other = backend.generate(prompt="a sunset", width=32, height=32, seed=43, fmt="png")
    assert again != other


def test_prompt_seed_is_stable():
    """Same prompt → same seed, across calls. The seed derivation uses
    SHA-256 of the prompt, so it's deterministic across Python versions
    and platforms (no random(), no time())."""
    assert _prompt_seed("hello world") == _prompt_seed("hello world")
    assert _prompt_seed("hello world") != _prompt_seed("goodbye world")


# ---- the headline acceptance: byte-identical round-trip ------------------


@pytest.mark.asyncio
async def test_image_generate_writes_byte_identical_png_to_sandbox():
    """Headline D9 test: the bytes the backend hands the tool land in the
    sandbox byte-for-byte (no text-mode round-trip, no encoding step). The
    on-disk file also starts with the PNG signature, so the file is a real
    decodable image — the deliverable contract."""
    backend = _FakePNGBackend()
    sbx = _FakeSandbox()
    tool = ImageGenTool(backend=backend)

    out = await tool.run(
        ImageGenArgs(
            prompt="abstract geometric pattern",
            filename="cover",
            width=24,
            height=24,
            format="png",
        ),
        _ctx(sbx),
    )

    # 1. success + deliverable surfaced
    assert out.success is True
    assert out.error is None
    assert out.artifacts == ["cover.png"], "image must be emitted as a deliverable"
    assert out.structured is not None
    assert out.structured["path"] == "cover.png"
    assert out.structured["format"] == "png"
    assert out.structured["width"] == 24
    assert out.structured["height"] == 24
    assert out.structured["backend"] == "fake-png"
    assert out.structured["bytes"] == len(sbx._fs["cover.png"])

    # 2. magic-bytes check: the on-disk file is a real PNG
    on_disk = sbx._fs["cover.png"]
    assert on_disk.startswith(_PNG_MAGIC), (
        f"on-disk file lacks PNG signature (got {on_disk[:8]!r}) — "
        f"the write path corrupted the binary or returned an empty file"
    )
    # Magic hex is surfaced in structured for the deliverable panel.
    assert out.structured["magic_hex"] == on_disk[:8].hex()

    # 3. byte-identical round-trip: backend output == sandbox file
    expected = backend.generate(
        prompt="abstract geometric pattern",
        width=24,
        height=24,
        seed=out.structured["seed"],
        fmt="png",
    )
    assert on_disk == expected, (
        "the bytes the backend produced must be byte-for-byte what lands "
        "in the sandbox — a text-mode encode or an intermediate copy would "
        "fail this assertion"
    )
    # Extra: SHA-256 of the file == SHA-256 of the backend output. Belt and
    # suspenders for the round-trip claim, in case a future PIL build
    # happens to re-encode with an identical-looking but bit-shifted stream.
    assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(expected).hexdigest()

    # 4. the write was a single call to sandbox.write_file with raw bytes
    assert sbx.writes == [("cover.png", on_disk)]
    assert isinstance(sbx.writes[0][1], bytes), (
        "the write must hand raw bytes to the sandbox — not a str, not a "
        "bytearray wrapped through a text encode"
    )

    # 5. PIL can fully decode the on-disk file (the "valid" half of the
    #    acceptance: not just a magic-bytes match, but a real decodable
    #    image end-to-end).
    img = Image.open(io.BytesIO(on_disk))
    img.load()  # forces full decode — raises on a corrupt/truncated PNG
    assert img.format == "PNG"
    assert img.size == (24, 24)


@pytest.mark.asyncio
async def test_imagegen_tool_resolves_backend_per_call(monkeypatch):
    """With no injected backend (the production path), ImageGenTool re-reads the
    configured provider via select_image_backend() on EVERY run() — so a provider
    saved in Settings is honored on the next call — and the deliverable metadata
    reports the LIVE backend that run() actually used."""

    class _FakeLive:
        name = "fake-live"
        is_remote = True

        def generate(self, *, prompt: str, width: int, height: int, seed: int, fmt: str) -> bytes:
            # Return a real, decodable PNG so the tool's magic-bytes + decode gate passes.
            return _FakePNGBackend().generate(
                prompt=prompt, width=width, height=height, seed=seed, fmt=fmt
            )

    import disco.tools.builtin.image_gen as ig

    monkeypatch.setattr(ig, "select_image_backend", lambda: _FakeLive())
    sbx = _FakeSandbox()
    out = await ImageGenTool().run(  # NO injected backend → must re-select per call
        ImageGenArgs(prompt="a tree", filename="t", width=16, height=16, format="png"),
        _ctx(sbx),
    )
    assert out.success is True
    assert out.structured is not None
    assert out.structured["backend"] == "fake-live", (
        "ImageGenTool().run() must report the per-call selected backend, not the default"
    )
    # Every reachable backend is a real, connected generator now (W-50).
    assert out.structured["placeholder"] is False
    assert out.structured["backend_connected"] is True
    assert "PROCEDURAL PLACEHOLDER" not in out.content


@pytest.mark.asyncio
async def test_run_returns_not_configured_when_no_backend(monkeypatch) -> None:
    """W-50: with no injected backend and no configured tier, select_image_backend()
    raises ImageGenNotConfigured — the tool must return a NOT-CONFIGURED failure with a
    Settings pointer (never a silent placeholder), and write nothing to the sandbox."""
    import disco.tools.builtin.image_gen as ig

    def _raise() -> object:
        raise ImageGenNotConfigured()

    monkeypatch.setattr(ig, "select_image_backend", _raise)
    sbx = _FakeSandbox()
    out = await ImageGenTool().run(  # no injected backend → resolves via the factory
        ImageGenArgs(prompt="a photo of a cat", filename="cat", format="png"),
        _ctx(sbx),
    )
    assert out.success is False
    assert out.error == "image_gen_not_configured"
    assert "isn't configured" in out.content
    assert "Settings" in out.content
    assert sbx.writes == [], "a not-configured run must not write any deliverable"


@pytest.mark.asyncio
async def test_image_generate_does_not_leak_bytes_into_text_content():
    """Brief: 'Leak the image bytes into the text context' is on the
    must-not-do list. The image bytes must NEVER appear in `content` (the
    model-visible text), nor in `structured` as the full payload — only
    metadata (size, dimensions, format, seed, magic-hex prefix) does.

    A naive implementation that interpolated the bytes into the success
    message would bloat the model's context and risk a downstream
    text-mode transport mangling the binary. This test pins the contract."""
    sbx = _FakeSandbox()
    out = await ImageGenTool(backend=_FakePNGBackend()).run(
        ImageGenArgs(prompt="a binary-safe test", filename="safe", format="png"),
        _ctx(sbx),
    )
    assert out.success is True
    on_disk = sbx._fs["safe.png"]
    assert on_disk.startswith(_PNG_MAGIC)

    # The full image bytes MUST NOT appear in `content` (we can't search
    # for all 300+ bytes verbatim, but a 32-byte contiguous chunk from the
    # middle of the file is a sufficient smoke check — any naive
    # interpolation of the bytes into the message would surface at least
    # one such chunk).
    middle_chunk = on_disk[10:42]
    assert middle_chunk not in out.content.encode("utf-8"), (
        "image bytes leaked into ToolOutcome.content (text context)"
    )
    # structured only carries the 8-byte magic_hex — never the full file.
    structured = out.structured or {}
    assert "magic_hex" in structured
    assert len(structured["magic_hex"]) == 16  # 8 bytes → 16 hex chars
    # And no `data` / `bytes_b64` / similar full-payload field sneaked in.
    for forbidden in ("data", "bytes_b64", "image_b64", "raw", "payload"):
        assert forbidden not in structured, (
            f"structured must NOT carry a full-payload field {forbidden!r}"
        )


@pytest.mark.asyncio
async def test_image_generate_jpeg_path_also_works():
    """The JPEG path uses the same binary-safe write; only the format and
    the magic-bytes sniff differ. A focused check that the JPEG branch
    also writes valid bytes and emits a deliverable."""
    sbx = _FakeSandbox()
    out = await ImageGenTool(backend=_FakePNGBackend()).run(
        ImageGenArgs(
            prompt="jpeg test",
            filename="cover-jpg",
            width=16,
            height=16,
            format="jpeg",
        ),
        _ctx(sbx),
    )
    assert out.success is True
    assert out.artifacts == ["cover-jpg.jpg"]
    jpeg_bytes = sbx._fs["cover-jpg.jpg"]
    # JPEG SOI marker: 0xFF 0xD8 (any JPEG starts with this).
    assert jpeg_bytes[:2] == b"\xff\xd8", (
        f"on-disk JPEG lacks the SOI marker (got {jpeg_bytes[:4]!r})"
    )
    img = Image.open(io.BytesIO(jpeg_bytes))
    img.load()
    assert img.format == "JPEG"
    assert img.size == (16, 16)


@pytest.mark.asyncio
async def test_image_generate_preserves_f3_read_before_write_guard():
    """F3 must remain untouched by the new tool. The image-gen path writes
    through `ctx.sandbox.write_file` directly — it never goes through
    `FileWriteTool`, never consults the F3 tracker, never warns. The
    test asserts that AFTER running image_generate, the F3 tracker
    state for the conversation is empty (no read/written/warned entries)."""
    from disco.tools.builtin import files as _files
    from disco.tools.builtin.files import FileWriteTool, reset_read_tracker

    reset_read_tracker()
    # Pre-existing file the image-gen tool does NOT touch — the F3 guard
    # is a no-op for image_generate (it never sees the file), and the
    # tracker must reflect that.
    sbx = _FakeSandbox({"important.txt": b"do not clobber"})
    out = await ImageGenTool(backend=_FakePNGBackend()).run(
        ImageGenArgs(prompt="isolated run", filename="art", format="png"),
        _ctx(sbx),
    )
    assert out.success is True

    # The tracker is empty — image_generate does not record reads/writes
    # in it. (The assist gate's `_read_state` only knows about the
    # file_read / file_write conversation, and image_generate is a third
    # path that bypasses both.)
    state = _files._read_state.get("conv-d9")
    assert state is None or all(
        not v for v in state.values()
    ), f"image_generate leaked F3 tracker state: {state}"

    # Crucially: the existing `important.txt` was NOT read by image_generate
    # (it doesn't go through FileReadTool), and a subsequent assist-ON
    # file_write to it must STILL hit the F3 guard. This is the precise
    # guarantee F3 provides, and the new tool must not have eroded it.
    from disco.tools.anatomy import ToolContext
    from disco.tools.builtin.files import FileWriteArgs

    assist_ctx = ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="conv-d9",
        assist=True,
    )
    fw = await FileWriteTool().run(
        FileWriteArgs(path="important.txt", content="new content\n"),
        assist_ctx,
    )
    assert fw.success is False
    assert fw.error == "read_before_write", (
        "F3 guard must still fire on file_write after image_generate ran"
    )
    assert sbx._fs["important.txt"] == b"do not clobber"


# ---- W-51: generated images must not overwrite each other ------------------


@pytest.mark.asyncio
async def test_w51_default_filename_does_not_overwrite_previous_image():
    """W-51 root: ImageGenArgs.filename defaults to 'image', so two generations
    with the default name would both write 'image.png' and the second clobbers the
    first. The fix probes the sandbox and disambiguates: first → image.png, second →
    image-1.png. Both files survive on disk."""
    sbx = _FakeSandbox()
    tool = ImageGenTool(backend=_FakePNGBackend())

    first = await tool.run(ImageGenArgs(prompt="alpha", format="png"), _ctx(sbx))
    second = await tool.run(ImageGenArgs(prompt="beta", format="png"), _ctx(sbx))

    assert first.success is True and second.success is True
    # The two deliverables have DIFFERENT names — no overwrite.
    assert first.structured is not None and second.structured is not None
    assert first.structured["path"] == "image.png"
    assert second.structured["path"] == "image-1.png"
    # Both files are present on disk (the first was not clobbered).
    assert "image.png" in sbx._fs
    assert "image-1.png" in sbx._fs
    # A third generation continues the sequence.
    third = await tool.run(ImageGenArgs(prompt="gamma", format="png"), _ctx(sbx))
    assert third.structured is not None
    assert third.structured["path"] == "image-2.png"
    assert "image-2.png" in sbx._fs


@pytest.mark.asyncio
async def test_w51_explicit_unique_filename_is_unaffected():
    """A caller that supplies a distinct base each time gets exactly that name —
    the disambiguation only kicks in on an actual collision."""
    sbx = _FakeSandbox()
    tool = ImageGenTool(backend=_FakePNGBackend())

    a = await tool.run(ImageGenArgs(prompt="x", filename="cover", format="png"), _ctx(sbx))
    b = await tool.run(ImageGenArgs(prompt="y", filename="hero", format="png"), _ctx(sbx))

    assert a.structured is not None and b.structured is not None
    assert a.structured["path"] == "cover.png"
    assert b.structured["path"] == "hero.png"
    # But re-using an explicit base DOES disambiguate (overwrite is never possible).
    c = await tool.run(ImageGenArgs(prompt="z", filename="cover", format="png"), _ctx(sbx))
    assert c.structured is not None
    assert c.structured["path"] == "cover-1.png"


@pytest.mark.asyncio
async def test_w51_returned_path_artifacts_and_content_reflect_final_name():
    """The disambiguated name must propagate to EVERY reference the caller sees:
    out_path (content), artifacts, structured.path and structured.filename_base —
    so the returned path matches the bytes that were actually written."""
    # Pre-seed the collision so the first run already has to disambiguate.
    sbx = _FakeSandbox({"image.png": b"\x89PNG\r\n\x1a\n-existing"})
    out = await ImageGenTool(backend=_FakePNGBackend()).run(
        ImageGenArgs(prompt="fresh", format="png"), _ctx(sbx)
    )
    assert out.success is True
    assert out.structured is not None
    final = out.structured["path"]
    assert final == "image-1.png"
    assert out.artifacts == [final], "artifacts must reference the final disambiguated name"
    assert out.structured["filename_base"] == "image-1"
    assert final in out.content, "content summary must name the file actually written"
    # The pre-existing file was NOT overwritten.
    assert sbx._fs["image.png"] == b"\x89PNG\r\n\x1a\n-existing"
    # The bytes the caller is pointed at are the ones that landed on disk.
    assert out.structured["bytes"] == len(sbx._fs[final])


@pytest.mark.asyncio
async def test_w51_bounded_loop_terminates_and_falls_back_when_saturated():
    """The numeric probe is bounded (1..1000). If every numbered slot is taken the
    loop must still terminate and fall back to the seed (then a timestamp) suffix —
    it can never spin forever."""

    class _SaturatedSandbox(_FakeSandbox):
        """Reports image.png and image-1.png … image-1000.png as ALL taken, so the
        bounded numeric loop exhausts and the seed fallback must fire. Everything
        else (including the seed-suffixed name) is free."""

        def __init__(self) -> None:
            super().__init__()
            self.taken: set[str] = {"image.png"} | {f"image-{n}.png" for n in range(1, 1001)}

        async def file_exists(self, path: str) -> bool:
            return path in self.taken

    sbx = _SaturatedSandbox()
    out = await ImageGenTool(backend=_FakePNGBackend()).run(
        ImageGenArgs(prompt="saturate", seed=4242, format="png"), _ctx(sbx)
    )
    assert out.success is True
    assert out.structured is not None
    # Numeric slots exhausted → seed fallback (seed=4242 was forced).
    assert out.structured["path"] == "image-4242.png"
    assert "image-4242.png" in sbx._fs
    # And none of the 1000 numbered slots were overwritten.
    assert sbx.writes == [("image-4242.png", sbx._fs["image-4242.png"])]


# ---- registration + scoping ------------------------------------------------


def test_image_generate_in_default_registry_and_agent_scope():
    """image_generate is registered by build_default_registry and offered
    in agent_scope (the surface that does work), absent from research_scope
    (the read-only surface)."""
    reg = build_default_registry()
    assert "image_generate" in reg.names()

    agt = agent_scope(model_policy=ModelExecutionPolicy.standard())
    assert "image_generate" in agt.allowed_tools
    tool = reg.get("image_generate", scope=agt)
    assert tool is not None
    assert tool.definition.name == "image_generate"

    res = research_scope()
    assert "image_generate" not in res.allowed_tools
    assert reg.get("image_generate", scope=res) is None


def test_image_generate_tool_def_is_sandbox_and_mutating():
    """The tool runs in the sandbox (file write) and is NOT read-only —
    so it's correctly withheld from the planner (planner-safety contract,
    same as file_write / audio_overview / slides_generate)."""
    d = ImageGenTool().definition
    assert d.name == "image_generate"
    assert d.runs_in == "sandbox"
    assert d.read_only is False
    # Filesystem capability only — the remote tiers (ComfyUI/OpenAI/OpenRouter) are
    # network-bound but route through the same binary write path.
    assert "filesystem" in {c.value for c in d.needs}


def test_image_generate_rejects_non_bytes_backend_output():
    """Defensive: if a future (buggy) backend returns a `bytearray` or
    `str` instead of `bytes`, the tool must fail loud rather than silently
    corrupt the image in the write path. The text-mode encode round-trip
    is the exact bug D9 is designed to prevent, so this guard is a
    belt-and-suspenders on the contract."""
    import asyncio as _asyncio

    class _BadBackend:
        name = "broken"
        is_remote = False
        def generate(self, **_):
            return "not bytes"  # str — would silently corrupt under utf-8 encode

    async def _runner():
        sbx = _FakeSandbox()
        out = await _run_with(_BadBackend(), sbx)
        assert out.success is False
        assert out.error == "backend_returned_non_bytes"
        # Nothing was written to the sandbox.
        assert sbx.writes == []

    _asyncio.run(_runner())


# ---- helpers ----------------------------------------------------------------


async def _run_with(backend, sbx):
    return await ImageGenTool(backend=backend).run(
        ImageGenArgs(prompt="x", filename="x", format="png"),
        _ctx(sbx),
    )


# ---- backend selection + factory --------------------------------------------


def test_select_image_backend_raises_when_openai_has_no_key(monkeypatch):
    """W-50: openai provider configured but no API key → NOT configured (raise),
    never a silent procedural placeholder."""

    class _MockConfig:
        image_gen = type('obj', (object,), {
            'provider': 'openai',
            'base_url': 'https://api.openai.com/v1',
            'api_key_env': 'OPENAI_API_KEY',
            'model': ''
        })()

    class _MockStore:
        def load(self):
            return _MockConfig()

    # Mock SecretStore to return no secret
    class _MockSecrets:
        def get_secret(self, name):
            return None

    monkeypatch.setattr('disco.tools.builtin.image_gen.ConfigStore', lambda: _MockStore())
    monkeypatch.setattr('disco.tools.builtin.image_gen.SecretStore', lambda: _MockSecrets())

    with pytest.raises(ImageGenNotConfigured, match="isn't configured"):
        select_image_backend()


def test_select_image_backend_raises_when_comfyui_has_no_url(monkeypatch):
    """W-50: comfyui provider configured but no base_url → NOT configured (raise)."""

    class _MockConfig:
        image_gen = type('obj', (object,), {
            'provider': 'comfyui',
            'base_url': '',  # Empty URL
            'api_key_env': ''
        })()

    class _MockStore:
        def load(self):
            return _MockConfig()

    monkeypatch.setattr('disco.tools.builtin.image_gen.ConfigStore', lambda: _MockStore())

    with pytest.raises(ImageGenNotConfigured):
        select_image_backend()


def test_select_image_backend_returns_openai_with_key(monkeypatch):
    """When openai provider is configured with a valid API key,
    the factory returns the OpenAI-compatible backend."""

    class _MockConfig:
        image_gen = type('obj', (object,), {
            'provider': 'openai',
            'base_url': 'https://api.openai.com/v1',
            'api_key_env': 'OPENAI_API_KEY',
            'model': ''
        })()

    class _MockStore:
        def load(self):
            return _MockConfig()

    class _MockSecrets:
        def get_secret(self, name):
            return "test-api-key-12345"

    monkeypatch.setattr('disco.tools.builtin.image_gen.ConfigStore', lambda: _MockStore())
    monkeypatch.setattr('disco.tools.builtin.image_gen.SecretStore', lambda: _MockSecrets())

    backend = select_image_backend()
    assert isinstance(backend, _OpenAIImageBackend)
    assert backend.name == "openai-compatible"
    assert backend.is_remote is True


def test_select_image_backend_returns_comfyui_with_url(monkeypatch):
    """When comfyui provider is configured with a base_url,
    the factory returns the ComfyUI backend."""

    class _MockConfig:
        image_gen = type('obj', (object,), {
            'provider': 'comfyui',
            'base_url': 'http://localhost:8188',
            'api_key_env': '',
            'model': 'sd_xl.safetensors',
            'workflow_json': '{"1": {"class_type": "X"}}'
        })()

    class _MockStore:
        def load(self):
            return _MockConfig()

    monkeypatch.setattr('disco.tools.builtin.image_gen.ConfigStore', lambda: _MockStore())

    backend = select_image_backend()
    assert isinstance(backend, _ComfyUIBackend)
    assert backend.name == "comfyui"
    assert backend.is_remote is True
    # The factory forwards BOTH the checkpoint and the custom workflow template.
    assert backend._ckpt == 'sd_xl.safetensors'
    assert backend._workflow_json == '{"1": {"class_type": "X"}}'


def test_select_image_backend_unknown_provider_raises(monkeypatch):
    """W-50: an unknown/unset provider → NOT configured (raise), no placeholder."""

    class _MockConfig:
        image_gen = type('obj', (object,), {
            'provider': 'unknown-provider',
            'base_url': '',
            'api_key_env': ''
        })()

    class _MockStore:
        def load(self):
            return _MockConfig()

    monkeypatch.setattr('disco.tools.builtin.image_gen.ConfigStore', lambda: _MockStore())

    with pytest.raises(ImageGenNotConfigured):
        select_image_backend()


# ---- OpenAI-compatible backend tests ----------------------------------------


def test_openai_backend_builds_correct_request_shape():
    """The OpenAI-compatible backend builds a proper /v1/images/generations
    request with the correct payload shape."""
    import base64
    from unittest.mock import MagicMock, patch

    # Create a minimal valid PNG (1x1 transparent)
    png_data = base64.b64encode(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')  # noqa: E501

    # Mock the httpx client
    mock_response = MagicMock()
    mock_response.json.return_value = {'data': [{'b64_json': png_data.decode()}]}
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_response

    with patch('disco.tools.builtin.image_gen.httpx.Client', return_value=mock_client):
        backend = _OpenAIImageBackend(
            base_url='https://api.openai.com/v1',
            api_key='test-key',
        )

        result = backend.generate(
            prompt='a sunset',
            width=512,
            height=512,
            seed=42,
            fmt='png',
        )

        # Verify the request was made with correct payload
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args

        assert '/v1/images/generations' in str(call_args)
        body = call_args.kwargs.get('json') or call_args[1].get('json')
        assert body['prompt'] == 'a sunset'
        # OpenAI only accepts fixed sizes; a square request snaps to 1024x1024
        # (arbitrary WxH like 512x512 would 400).
        assert body['size'] == '1024x1024'
        assert body['n'] == 1
        assert body['response_format'] == 'b64_json'

        headers = call_args.kwargs.get('headers') or call_args[1].get('headers')
        assert 'Authorization' in headers
        assert headers['Authorization'] == 'Bearer test-key'

        # Verify we got the image back
        assert result.startswith(b'\x89PNG')


def test_openai_backend_raises_on_api_error():
    """The OpenAI-compatible backend raises on API errors."""
    from unittest.mock import MagicMock, patch

    from disco.tools.builtin.image_gen import _OpenAIImageBackend

    # Mock the httpx client to raise an error
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=MagicMock(),
        response=MagicMock(status_code=401),
    )

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_response

    with patch('disco.tools.builtin.image_gen.httpx.Client', return_value=mock_client):
        backend = _OpenAIImageBackend(
            base_url='https://api.openai.com/v1',
            api_key='invalid-key',
        )

        with pytest.raises(httpx.HTTPStatusError):
            backend.generate(
                prompt='a sunset',
                width=512,
                height=512,
                seed=42,
                fmt='png',
            )


# ---- ComfyUI backend tests --------------------------------------------------


def test_comfyui_backend_builds_workflow_and_polls():
    """The ComfyUI backend submits a prompt and polls for completion."""
    from unittest.mock import MagicMock, patch

    # Track call count for polling
    call_count = [0]

    def mock_get(url, **kwargs):
        call_count[0] += 1
        mock_resp = MagicMock()
        if '/prompt' in str(url):
            mock_resp.json.return_value = {'prompt_id': 'test-prompt-123'}
            mock_resp.raise_for_status = MagicMock()
        elif '/history/test-prompt-123' in str(url):
            if call_count[0] <= 2:
                # Not ready yet
                mock_resp.json.return_value = {}
            else:
                # Ready
                mock_resp.json.return_value = {
                    'test-prompt-123': {
                        'outputs': {
                            '9': {
                                'images': [
                                    {
                                        'filename': 'test.png',
                                        'subfolder': '',
                                        'type': 'output',
                                    }
                                ]
                            }
                        }
                    }
                }
            mock_resp.raise_for_status = MagicMock()
        elif '/view' in str(url):
            mock_resp.content = b'\x89PNG\r\n\x1a\n' + b'fake png data'
            mock_resp.raise_for_status = MagicMock()
        return mock_resp

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = lambda url, **kwargs: mock_get(url, **kwargs)
    mock_client.get.side_effect = lambda url, **kwargs: mock_get(url, **kwargs)

    with patch('disco.tools.builtin.image_gen.httpx.Client', return_value=mock_client):
        backend = _ComfyUIBackend(
            base_url='http://localhost:8188', model='sd_xl_base_1.0.safetensors'
        )

        result = backend.generate(
            prompt='a sunset',
            width=512,
            height=512,
            seed=42,
            fmt='png',
        )

        # Verify prompt was submitted
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert '/prompt' in str(call_args)

        # The submitted default graph is the SDXL/SD shape: CLIP comes from the
        # checkpoint (CheckpointLoaderSimple), NOT a separate FLUX T5 CLIPLoader.
        submitted = (call_args.kwargs.get('json') or call_args[1].get('json'))['prompt']
        class_types = {node['class_type'] for node in submitted.values()}
        assert 'CheckpointLoaderSimple' in class_types
        assert 'CLIPLoader' not in class_types  # the old FLUX-frankenstein node is gone
        assert 'VAEDecode' in class_types and 'SaveImage' in class_types
        # The configured checkpoint is wired into the loader.
        ckpt_nodes = [n for n in submitted.values() if n['class_type'] == 'CheckpointLoaderSimple']
        assert ckpt_nodes[0]['inputs']['ckpt_name'] == 'sd_xl_base_1.0.safetensors'

        # Verify polling happened
        assert mock_client.get.call_count >= 2

        # Verify we got image data
        assert result == b'\x89PNG\r\n\x1a\n' + b'fake png data'


def test_comfyui_default_graph_requires_a_checkpoint():
    """Without a checkpoint (and no custom workflow), the default graph can't name a
    model to load — fail loud with a Settings pointer rather than 404 on a bogus default."""
    backend = _ComfyUIBackend(base_url='http://localhost:8188', model='')
    with pytest.raises(ValueError, match="checkpoint"):
        backend.generate(prompt='x', width=1024, height=1024, seed=1, fmt='png')


def test_comfyui_snaps_subnative_dimensions_up():
    """The tool defaults to 64x64 (fine for the procedural pattern, garbage for SDXL).
    The ComfyUI backend snaps sub-512 requests up to 1024 and rounds to a multiple of 8."""
    from disco.tools.builtin.image_gen import _comfy_snap_dim

    assert _comfy_snap_dim(64) == 1024  # tool default → native square
    assert _comfy_snap_dim(500) == 1024  # below the 512 floor → snap up
    assert _comfy_snap_dim(512) == 512  # at the floor, kept
    assert _comfy_snap_dim(1024) == 1024
    assert _comfy_snap_dim(1023) == 1016  # rounded down to a multiple of 8
    assert _comfy_snap_dim(1536) == 1536


def test_comfyui_template_substitutes_tokens_and_is_injection_safe():
    """A custom workflow_json template substitutes the tokens with correct types and
    JSON-escapes string tokens so a hostile prompt can't break out of its string or
    inject/drop nodes."""
    template = (
        '{'
        '"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "%ckpt%"}},'
        '"2": {"class_type": "CLIPTextEncode", "inputs": {"text": "%prompt%", "clip": ["1", 1]}},'
        '"3": {"class_type": "CLIPTextEncode", "inputs": {"text": "%negative%", "clip": ["1", 1]}},'
        '"4": {"class_type": "EmptyLatentImage", "inputs": {"width": %width%, "height": %height%, "batch_size": 1}},'  # noqa: E501
        '"5": {"class_type": "KSampler", "inputs": {"seed": %seed%, "model": ["1", 0]}}'
        '}'
    )
    backend = _ComfyUIBackend(
        base_url='http://localhost:8188', model='my-ckpt.safetensors', workflow_json=template
    )

    # A hostile prompt full of JSON-breaking characters + an injected-node attempt.
    hostile = 'a cat", "EVIL": {"class_type": "X"}, "z": "\nline\\two'
    graph = backend._render_template(
        prompt=hostile, negative='lowres', width=768, height=1024, seed=12345
    )

    # Exactly the template's 5 nodes — no injected "EVIL"/"z" nodes leaked in.
    assert set(graph) == {'1', '2', '3', '4', '5'}
    # The prompt round-trips verbatim as a STRING value (not parsed as JSON structure).
    assert graph['2']['inputs']['text'] == hostile
    assert graph['3']['inputs']['text'] == 'lowres'
    assert graph['1']['inputs']['ckpt_name'] == 'my-ckpt.safetensors'
    # Numeric tokens are real ints, not quoted strings.
    assert graph['5']['inputs']['seed'] == 12345
    assert isinstance(graph['5']['inputs']['seed'], int)
    assert graph['4']['inputs']['width'] == 768
    assert isinstance(graph['4']['inputs']['width'], int)


def test_comfyui_template_prompt_containing_a_token_is_not_re_substituted():
    """Single-pass substitution: a prompt that literally contains another token (e.g.
    the text '%seed%') must survive verbatim — a naive chained .replace() would bleed
    the seed value into the prompt on a later pass."""
    template = (
        '{"2": {"class_type": "CLIPTextEncode", "inputs": {"text": "%prompt%"}},'
        '"5": {"class_type": "KSampler", "inputs": {"seed": %seed%}}}'
    )
    backend = _ComfyUIBackend(
        base_url='http://localhost:8188', model='c.safetensors', workflow_json=template
    )
    graph = backend._render_template(
        prompt="render seed %seed% and %width%px please",
        negative="",
        width=512,
        height=512,
        seed=98765,
    )
    assert graph['2']['inputs']['text'] == "render seed %seed% and %width%px please"
    assert graph['5']['inputs']['seed'] == 98765


def test_comfyui_template_using_ckpt_token_requires_a_checkpoint():
    """A custom workflow that references %ckpt% but has no Checkpoint set fails with the
    Settings pointer BEFORE submitting (not a confusing empty-model 404 inside ComfyUI)."""
    backend = _ComfyUIBackend(
        base_url='http://localhost:8188',
        model='',
        workflow_json='{"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "%ckpt%"}}}',  # noqa: E501
    )
    with pytest.raises(ValueError, match="Checkpoint"):
        backend.generate(prompt='x', width=1024, height=1024, seed=1, fmt='png')


def test_comfyui_prefers_output_image_over_temp_preview():
    """When a graph emits both a temp PREVIEW and a final OUTPUT image, the backend
    fetches the saved OUTPUT, not the thumbnail."""
    from unittest.mock import MagicMock, patch

    fetched = {}

    def mock_call(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if '/prompt' in str(url):
            resp.json.return_value = {'prompt_id': 'p1'}
        elif '/history/p1' in str(url):
            resp.json.return_value = {
                'p1': {
                    'outputs': {
                        # preview node first (temp), final saved node second (output)
                        '8': {'images': [{'filename': 'prev.png', 'subfolder': '', 'type': 'temp'}]},  # noqa: E501
                        '9': {'images': [{'filename': 'final.png', 'subfolder': '', 'type': 'output'}]},  # noqa: E501
                    }
                }
            }
        elif '/view' in str(url):
            fetched['params'] = kwargs.get('params')
            resp.content = b'\x89PNG\r\n\x1a\n' + b'final-bytes'
        return resp

    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.side_effect = mock_call
    client.get.side_effect = mock_call

    with patch('disco.tools.builtin.image_gen.httpx.Client', return_value=client):
        backend = _ComfyUIBackend(base_url='http://localhost:8188', model='m.safetensors')
        result = backend.generate(prompt='x', width=1024, height=1024, seed=1, fmt='png')

    assert fetched['params']['filename'] == 'final.png'
    assert fetched['params']['type'] == 'output'
    assert result == b'\x89PNG\r\n\x1a\n' + b'final-bytes'


def test_comfyui_template_malformed_json_raises_clearly():
    """A template that isn't valid JSON after substitution fails loud with guidance."""
    backend = _ComfyUIBackend(
        base_url='http://localhost:8188',
        model='c.safetensors',
        workflow_json='{ this is not json %seed%',
    )
    with pytest.raises(ValueError, match="API Format|valid JSON"):
        backend.generate(prompt='x', width=1024, height=1024, seed=1, fmt='png')


# ---- ImageRouter / OpenAI-compatible endpoint + format handling -------------


def _webp_bytes() -> bytes:
    import io as _io

    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (32, 24), (200, 120, 40)).save(buf, format="WEBP")
    return buf.getvalue()


def test_openai_backend_endpoint_full_url_vs_origin():
    """A bare origin gets /v1/images/generations appended; a base_url that already
    names the images route (ImageRouter's /v1/openai/...) is used verbatim."""
    from disco.tools.builtin.image_gen import _OpenAIImageBackend

    origin = _OpenAIImageBackend("https://api.openai.com", "k")
    assert origin._endpoint() == "https://api.openai.com/v1/images/generations"

    ir = _OpenAIImageBackend("https://api.imagerouter.io/v1/openai/images/generations", "k")
    assert ir._endpoint() == "https://api.imagerouter.io/v1/openai/images/generations"


def test_openai_backend_posts_to_imagerouter_path_and_normalizes_webp():
    """Against an ImageRouter-style full URL the backend POSTs to that exact path and
    normalizes the WEBP b64 it returns into a PNG deliverable (Disco's contract)."""
    import base64
    from unittest.mock import MagicMock, patch

    from disco.tools.builtin.image_gen import _PNG_MAGIC, _OpenAIImageBackend

    webp_b64 = base64.b64encode(_webp_bytes()).decode()
    captured = {}

    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [{"b64_json": webp_b64}]}
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    def _post(url, **kw):
        captured["url"] = url
        return mock_response

    mock_client.post.side_effect = _post

    with patch("disco.tools.builtin.image_gen.httpx.Client", return_value=mock_client):
        be = _OpenAIImageBackend(
            "https://api.imagerouter.io/v1/openai/images/generations", "k", model="test/test"
        )
        out = be.generate(prompt="a fox", width=1024, height=1024, seed=1, fmt="png")

    assert captured["url"] == "https://api.imagerouter.io/v1/openai/images/generations"
    # WEBP from the provider → PNG bytes out (the deliverable contract holds)
    assert out.startswith(_PNG_MAGIC), "webp response was not normalized to PNG"


# ---- OpenRouter image backend (chat-completions + modalities) ---------------


def test_openrouter_backend_parses_data_url_image():
    """OpenRouter returns the image as a base64 data URL at
    choices[0].message.images[0].image_url.url — the backend decodes + PNG-normalizes it,
    and POSTs to /chat/completions with modalities:[image,text]."""
    import base64
    import io as _io
    from unittest.mock import MagicMock, patch

    from disco.tools.builtin.image_gen import _PNG_MAGIC, _OpenRouterImageBackend
    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (8, 8), (20, 200, 90)).save(buf, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    captured = {}
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"images": [{"image_url": {"url": data_url}}]}}]
    }
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    def _post(url, json=None, headers=None):
        captured["url"] = url
        captured["body"] = json
        captured["auth"] = headers.get("Authorization")
        return resp

    client.post.side_effect = _post
    with patch("disco.tools.builtin.image_gen.httpx.Client", return_value=client):
        be = _OpenRouterImageBackend("https://openrouter.ai/api/v1", "sk-or", model="g/img")
        out = be.generate(prompt="a leaf", width=1024, height=1024, seed=1, fmt="png")

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["body"]["modalities"] == ["image", "text"]
    assert captured["body"]["model"] == "g/img"
    assert captured["auth"] == "Bearer sk-or"
    assert out.startswith(_PNG_MAGIC)


def test_openrouter_backend_refuses_remote_url_ssrf():
    """A remote http(s) image URL (not an inline data URL) is refused, never fetched."""
    from unittest.mock import MagicMock, patch

    from disco.tools.builtin.image_gen import _OpenRouterImageBackend

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"images": [{"image_url": {"url": "https://evil.example/x.png"}}]}}]
    }
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = resp
    with patch("disco.tools.builtin.image_gen.httpx.Client", return_value=client):
        be = _OpenRouterImageBackend("https://openrouter.ai/api/v1", "sk-or")
        with pytest.raises(ValueError, match="SSRF"):
            be.generate(prompt="x", width=512, height=512, seed=1, fmt="png")


def test_select_image_backend_openrouter_uses_reserved_slot(monkeypatch):
    """provider=openrouter resolves the OpenRouter key from the reserved 'openrouter'
    SecretStore slot and returns the OpenRouter image backend."""
    from disco.tools.builtin.image_gen import _OpenRouterImageBackend

    class _Cfg:
        image_gen = type("o", (object,), {
            "provider": "openrouter", "base_url": "", "api_key_env": "",
            "model": "google/gemini-2.5-flash-image", "workflow_json": "",
        })()

    monkeypatch.setattr("disco.tools.builtin.image_gen.ConfigStore", lambda: type("S", (), {"load": lambda s: _Cfg()})())  # noqa: E501

    class _Secret:
        def get_openrouter_key(self):
            return "sk-or-reserved"
    monkeypatch.setattr("disco.tools.builtin.image_gen.SecretStore", lambda: _Secret())

    be = select_image_backend()
    assert isinstance(be, _OpenRouterImageBackend)
    assert be.name == "openrouter-image"
    assert be._model == "google/gemini-2.5-flash-image"
    assert be._api_key == "sk-or-reserved"


def test_select_image_backend_openrouter_empty_model_raises(monkeypatch):
    """W-50 P1: provider=openrouter with a stored key but an EMPTY image model is NOT
    configured — the factory must raise ImageGenNotConfigured, not silently fall back to
    a hardcoded default model the user never chose. Settings already shows this tier as
    unavailable until a model id is set; the factory must enforce the same."""
    class _Cfg:
        image_gen = type("o", (object,), {
            "provider": "openrouter", "base_url": "", "api_key_env": "",
            "model": "", "workflow_json": "",
        })()

    monkeypatch.setattr("disco.tools.builtin.image_gen.ConfigStore", lambda: type("S", (), {"load": lambda s: _Cfg()})())  # noqa: E501
    monkeypatch.setattr("disco.tools.builtin.image_gen.SecretStore",
                        lambda: type("K", (), {"get_openrouter_key": lambda s: "sk-or"})())

    with pytest.raises(ImageGenNotConfigured):
        select_image_backend()


def test_select_image_backend_openrouter_whitespace_model_raises(monkeypatch):
    """A model id of only whitespace is just as unconfigured as empty — must raise."""
    class _Cfg:
        image_gen = type("o", (object,), {
            "provider": "openrouter", "base_url": "", "api_key_env": "",
            "model": "   ", "workflow_json": "",
        })()

    monkeypatch.setattr("disco.tools.builtin.image_gen.ConfigStore", lambda: type("S", (), {"load": lambda s: _Cfg()})())  # noqa: E501
    monkeypatch.setattr("disco.tools.builtin.image_gen.SecretStore",
                        lambda: type("K", (), {"get_openrouter_key": lambda s: "sk-or"})())

    with pytest.raises(ImageGenNotConfigured):
        select_image_backend()


def test_select_openrouter_ignores_stale_base_url(monkeypatch):
    """SECURITY: a stale base_url left from another provider must NOT be used for
    OpenRouter (it would send the OpenRouter Bearer key to the wrong host)."""
    from disco.tools.builtin.image_gen import _OpenRouterImageBackend

    class _Cfg:
        image_gen = type("o", (object,), {
            "provider": "openrouter", "base_url": "https://evil.example/v1",
            "api_key_env": "", "model": "g/img", "workflow_json": "",
        })()

    monkeypatch.setattr("disco.tools.builtin.image_gen.ConfigStore",
                        lambda: type("S", (), {"load": lambda s: _Cfg()})())
    monkeypatch.setattr("disco.tools.builtin.image_gen.SecretStore",
                        lambda: type("K", (), {"get_openrouter_key": lambda s: "sk-or"})())
    be = select_image_backend()
    assert isinstance(be, _OpenRouterImageBackend)
    assert be._base_url == "https://openrouter.ai/api/v1", "stale base_url was NOT ignored"


def test_normalize_rejects_oversized_blob():
    from disco.tools.builtin.image_gen import _MAX_IMAGE_BYTES, _normalize_to_png_or_jpeg

    with pytest.raises(ValueError, match="too large"):
        _normalize_to_png_or_jpeg(b"\x00" * (_MAX_IMAGE_BYTES + 1))


def test_openai_endpoint_ignores_query_string_false_positive():
    """A base_url with a query string mentioning the images path is NOT treated as a
    full endpoint — the standard suffix is appended to the (parsed) origin path."""
    from disco.tools.builtin.image_gen import _OpenAIImageBackend

    be = _OpenAIImageBackend("https://proxy.example/api?next=/images/generations", "k")
    assert be._endpoint() == "https://proxy.example/api?next=/images/generations/v1/images/generations"  # noqa: E501
