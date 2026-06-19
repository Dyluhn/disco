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

Backend note: the live `diffusers`+`torch` Stable-Diffusion wire is DEFERRED
(neither package is installed in this env; importing them would force a
multi-GB model download). The default backend is `_PILProceduralBackend`
(keyless, local, no network), which is sufficient to prove the
binary-write + deliverable + magic-bytes contract end-to-end.
"""

from __future__ import annotations

import hashlib
import io

import httpx
import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin import ImageGenTool, build_default_registry
from disco.tools.builtin.image_gen import (
    _PNG_MAGIC,
    ImageGenArgs,
    _ComfyUIBackend,
    _OpenAIImageBackend,
    _PILProceduralBackend,
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


def _ctx(sandbox: _FakeSandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="conv-d9",
    )


# ---- backend sanity (no tool plumbing) -------------------------------------


def test_pil_procedural_backend_produces_valid_png():
    """The default backend returns a PNG whose first 8 bytes match the
    RFC 2083 signature \\x89PNG\\r\\n\\x1a\\n. A test that just decodes
    the bytes with PIL proves the same thing structurally — both must pass
    for the deliverable to be considered non-corrupt."""
    backend = _PILProceduralBackend()
    raw = backend.generate(
        prompt="a sunset over the sea",
        width=32,
        height=32,
        seed=42,
        fmt="png",
    )
    assert isinstance(raw, bytes), "backend must return raw bytes, not str"
    assert raw.startswith(_PNG_MAGIC), (
        f"first 8 bytes should be the PNG signature, got {raw[:8]!r}"
    )
    # PIL can decode it: the bytes are a real image, not a renamed/empty
    # file with a coincidentally-matching prefix.
    img = Image.open(io.BytesIO(raw))
    img.load()  # forces a full decode — would raise on a corrupt PNG
    assert img.format == "PNG"
    assert img.size == (32, 32)


def test_pil_procedural_backend_deterministic_per_seed():
    """Same prompt + same seed → byte-identical PNG. This is the property
    the round-trip test relies on (it compares the bytes on disk to the
    bytes the backend produced, so determinism proves no silent
    re-encoding happened in the write path)."""
    backend = _PILProceduralBackend()
    a = backend.generate(prompt="x", width=16, height=16, seed=7, fmt="png")
    b = backend.generate(prompt="x", width=16, height=16, seed=7, fmt="png")
    assert a == b
    # Different seed → different bytes (the pattern is visibly distinct).
    c = backend.generate(prompt="x", width=16, height=16, seed=8, fmt="png")
    assert a != c


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
    backend = _PILProceduralBackend()
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
    assert out.structured["backend"] == "pil-procedural"
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
    reports the LIVE backend, not the procedural constructor default."""

    class _FakeLive:
        name = "fake-live"
        is_remote = True

        def generate(self, *, prompt: str, width: int, height: int, seed: int, fmt: str) -> bytes:
            # Return a real, decodable PNG so the tool's magic-bytes + decode gate passes.
            return _PILProceduralBackend().generate(
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
    out = await ImageGenTool().run(
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
    out = await ImageGenTool().run(
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
    out = await ImageGenTool().run(
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


# ---- registration + scoping ------------------------------------------------


def test_image_generate_in_default_registry_and_agent_scope():
    """image_generate is registered by build_default_registry and offered
    in agent_scope (the surface that does work), absent from research_scope
    (the read-only surface)."""
    reg = build_default_registry()
    assert "image_generate" in reg.names()

    agt = agent_scope()
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
    # Filesystem capability only — the procedural backend needs no
    # network. The deferred diffusers tier would want NETWORK too.
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


def test_select_image_backend_returns_procedural_by_default(monkeypatch):
    """When no provider is configured (procedural default), the factory
    returns the keyless PIL procedural backend."""
    from disco.tools.builtin.image_gen import _PILProceduralBackend

    # Mock ConfigStore to return procedural provider
    class _MockConfig:
        image_gen = type('obj', (object,), {'provider': 'procedural', 'base_url': '', 'api_key_env': ''})()

    class _MockStore:
        def load(self):
            return _MockConfig()

    monkeypatch.setattr('disco.tools.builtin.image_gen.ConfigStore', lambda: _MockStore())

    backend = select_image_backend()
    assert isinstance(backend, _PILProceduralBackend)
    assert backend.name == "pil-procedural"
    assert backend.is_remote is False


def test_select_image_backend_falls_back_to_procedural_when_openai_has_no_key(monkeypatch):
    """When openai provider is configured but no API key is available,
    the factory falls back to procedural."""
    from disco.tools.builtin.image_gen import _PILProceduralBackend

    class _MockConfig:
        image_gen = type('obj', (object,), {
            'provider': 'openai',
            'base_url': 'https://api.openai.com/v1',
            'api_key_env': 'OPENAI_API_KEY'
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

    backend = select_image_backend()
    assert isinstance(backend, _PILProceduralBackend)


def test_select_image_backend_falls_back_when_comfyui_has_no_url(monkeypatch):
    """When comfyui provider is configured but no base_url is set,
    the factory falls back to procedural."""
    from disco.tools.builtin.image_gen import _PILProceduralBackend

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

    backend = select_image_backend()
    assert isinstance(backend, _PILProceduralBackend)


def test_select_image_backend_returns_openai_with_key(monkeypatch):
    """When openai provider is configured with a valid API key,
    the factory returns the OpenAI-compatible backend."""

    class _MockConfig:
        image_gen = type('obj', (object,), {
            'provider': 'openai',
            'base_url': 'https://api.openai.com/v1',
            'api_key_env': 'OPENAI_API_KEY'
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
            'api_key_env': ''
        })()

    class _MockStore:
        def load(self):
            return _MockConfig()

    monkeypatch.setattr('disco.tools.builtin.image_gen.ConfigStore', lambda: _MockStore())

    backend = select_image_backend()
    assert isinstance(backend, _ComfyUIBackend)
    assert backend.name == "comfyui"
    assert backend.is_remote is True


def test_select_image_backend_unknown_provider_falls_back(monkeypatch):
    """When an unknown provider is configured, the factory falls back
    to procedural."""
    from disco.tools.builtin.image_gen import _PILProceduralBackend

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

    backend = select_image_backend()
    assert isinstance(backend, _PILProceduralBackend)


# ---- OpenAI-compatible backend tests ----------------------------------------


def test_openai_backend_builds_correct_request_shape():
    """The OpenAI-compatible backend builds a proper /v1/images/generations
    request with the correct payload shape."""
    import base64
    from unittest.mock import MagicMock, patch

    # Create a minimal valid PNG (1x1 transparent)
    png_data = base64.b64encode(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')

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
        backend = _ComfyUIBackend(base_url='http://localhost:8188')

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

        # Verify polling happened
        assert mock_client.get.call_count >= 2

        # Verify we got image data
        assert result == b'\x89PNG\r\n\x1a\n' + b'fake png data'
