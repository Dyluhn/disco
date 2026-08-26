"""Tests for the V1–V7 vision capability system (§2).

Covers:
 - vision_table.table_vision (V3 static table)
 - ModelEntry.vision pin (V1)
 - apply_runtime_capabilities probe order (V4)
 - probe_vision / probe_all_vision helpers (V2, stubbed)
 - prompts.DriverPrompts vision-aware mandates (V7 / W6)
 - W4: ANCHORED_EDIT on capable models
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from disco.core.llm.config import (
    ModelEntry,
    RouterConfig,
    apply_runtime_capabilities,
    default_config,
)
from disco.core.llm.prompts import DriverPrompts
from disco.core.llm.types import ModelRole, OperatingMode, Requirement
from disco.core.llm.vision_table import resolve_vision_status, table_vision

# ===========================================================================
# V3 — table_vision static rules
# ===========================================================================


class TestTableVision:
    """Pure unit tests for the static vision-capability table."""

    # Anthropic family rule (all Claude >= v3 are vision-capable)
    def test_claude_in_id_is_vision(self):
        assert table_vision("claude-3-opus-20240229") is True

    def test_claude_via_openrouter_slug_is_vision(self):
        assert table_vision("anthropic/claude-3-5-sonnet") is True

    def test_family_anthropic_overrides_unknown_id(self):
        assert table_vision("opaque-cloud-model", family="anthropic") is True

    def test_anthropic_slash_prefix_is_vision(self):
        assert table_vision("anthropic/claude-3-haiku-20240307") is True

    # Open / multilingual vision-language models (the "-VL" convention + MLLMs).
    def test_qwen_vl_is_vision(self):
        assert table_vision("qwen/qwen-2.5-vl-72b-instruct") is True
        assert table_vision("qwen/qwen3-vl-235b") is True

    def test_internvl_llava_glmv_are_vision(self):
        assert table_vision("opengvlab/internvl-2.5") is True
        assert table_vision("liuhaotian/llava-1.6") is True
        assert table_vision("z-ai/glm-4.6v") is True
        assert table_vision("thudm/glm-4v") is True

    def test_non_vision_qwen_coder_is_not_vision(self):
        # The "-vl" rule must NOT match a text-only Qwen coder model.
        assert table_vision("qwen/qwen-2.5-coder-32b") is False

    # OpenAI known-vision patterns
    def test_gpt_4o_is_vision(self):
        assert table_vision("gpt-4o") is True

    def test_gpt_4o_mini_is_vision(self):
        assert table_vision("gpt-4o-mini") is True

    def test_gpt_41_is_vision(self):
        assert table_vision("gpt-4.1") is True

    def test_gpt_41_mini_is_vision(self):
        assert table_vision("gpt-4.1-mini") is True

    def test_gpt_45_is_vision(self):
        assert table_vision("gpt-4.5-preview") is True

    def test_gpt_5_is_vision(self):
        assert table_vision("gpt-5") is True

    def test_gpt_5_via_openrouter_is_vision(self):
        assert table_vision("openai/gpt-5") is True

    # O-series (o3, o4)
    def test_o3_base_is_vision(self):
        assert table_vision("o3") is True

    def test_o3_mini_is_vision(self):
        assert table_vision("o3-mini") is True

    def test_o4_mini_is_vision(self):
        assert table_vision("o4-mini") is True

    def test_o3_via_openrouter_slug(self):
        assert table_vision("openai/o3") is True

    # Gemini rule (all gemini-* are multimodal)
    def test_gemini_flash_is_vision(self):
        assert table_vision("gemini-3-flash-preview") is True

    def test_gemini_openrouter_slug(self):
        assert table_vision("google/gemini-3-flash-preview") is True

    # Non-vision patterns (exclusions)
    def test_text_embedding_is_not_vision(self):
        assert table_vision("text-embedding-3-large") is False

    def test_embedding_in_id_is_not_vision(self):
        assert table_vision("some-embedding-model") is False

    def test_whisper_is_not_vision(self):
        assert table_vision("whisper-1") is False

    def test_tts_is_not_vision(self):
        assert table_vision("tts-1") is False

    def test_tts_hd_is_not_vision(self):
        assert table_vision("tts-1-hd") is False

    def test_rerank_is_not_vision(self):
        assert table_vision("bge-reranker-v2-m3") is False

    # Unknown → fail-safe False
    def test_unknown_model_is_not_vision(self):
        assert table_vision("mystery-7b") is False

    def test_unknown_local_slug_is_not_vision(self):
        assert table_vision("Qwen3.6-27B-UD-Q5_K_XL.gguf", "qwen") is False

    def test_unknown_with_unknown_family_is_not_vision(self):
        assert table_vision("some-model-4b", family="llama") is False

    def test_unknown_is_distinct_from_text_only(self):
        assert resolve_vision_status(model_id="mystery-7b") == "unknown"
        assert resolve_vision_status(model_id="text-embedding-3-large") == "text-only"


# ===========================================================================
# V1 — ModelEntry.vision pin
# ===========================================================================


def _make_config(
    *,
    vision_pin: bool | None = None,
    has_vision_cap: bool = False,
    model_id: str = "test-model",
) -> RouterConfig:
    """Helper: build a minimal RouterConfig with one model entry."""
    caps: set[Requirement] = set()
    if has_vision_cap:
        caps.add(Requirement.VISION)
    entry = ModelEntry(
        model_id=model_id,
        provider="test",
        base_url="http://localhost:8080/v1",
        context_window=4096,
        capabilities=frozenset(caps),
        vision=vision_pin,
    )
    return RouterConfig(models={"test-model": entry}, default_model="test-model")


class TestVisionPin:
    """V1: the per-model vision override pin."""

    def test_pin_false_prevents_vision_even_if_probe_says_yes(self):
        """A pinned vision=False entry must NOT gain VISION even with a probe result
        of True.  The pin is the hardest gate."""
        config = _make_config(vision_pin=False, has_vision_cap=False)
        result = apply_runtime_capabilities(config, probe_results={"test-model": True})
        assert Requirement.VISION not in result.models["test-model"].capabilities

    def test_pin_true_forces_vision_without_probe(self):
        """vision=True forces VISION even if the probe returned None."""
        config = _make_config(vision_pin=True, has_vision_cap=False)
        result = apply_runtime_capabilities(config, probe_results={"test-model": None})
        assert Requirement.VISION in result.models["test-model"].capabilities

    def test_pin_true_forces_vision_without_any_probe_results(self):
        """vision=True forces VISION even with no probe_results supplied."""
        config = _make_config(vision_pin=True, has_vision_cap=False)
        result = apply_runtime_capabilities(config)
        assert Requirement.VISION in result.models["test-model"].capabilities

    def test_pin_none_falls_through_to_probe(self):
        """vision=None defers to the probe result."""
        config = _make_config(vision_pin=None, model_id="mystery-7b")
        result = apply_runtime_capabilities(config, probe_results={"test-model": True})
        assert Requirement.VISION in result.models["test-model"].capabilities

    def test_pin_none_and_no_probe_falls_through_to_table(self):
        """vision=None, no probe → uses the static table.  gpt-4o → True."""
        config = _make_config(vision_pin=None, model_id="gpt-4o")
        result = apply_runtime_capabilities(config)
        assert Requirement.VISION in result.models["test-model"].capabilities


# ===========================================================================
# V4 — apply_runtime_capabilities probe order
# ===========================================================================


class TestApplyRuntimeCapabilities:
    """V4: the full probe order across all entries."""

    def test_probe_true_adds_vision(self):
        """A probe result of True grants VISION to an entry that had none."""
        config = _make_config(vision_pin=None, has_vision_cap=False, model_id="mystery-7b")
        result = apply_runtime_capabilities(config, probe_results={"test-model": True})
        assert Requirement.VISION in result.models["test-model"].capabilities

    def test_probe_false_removes_vision(self):
        """A probe result of False removes VISION from an entry that had it."""
        config = _make_config(vision_pin=None, has_vision_cap=True, model_id="mystery-7b")
        result = apply_runtime_capabilities(config, probe_results={"test-model": False})
        assert Requirement.VISION not in result.models["test-model"].capabilities

    def test_resolution_precedence_is_pin_probe_provider_static_unknown(self):
        assert resolve_vision_status(
            model_id="mystery-7b",
            explicit=False,
            live_probe=True,
            provider_declared=True,
        ) == "text-only"
        assert resolve_vision_status(
            model_id="mystery-7b", live_probe=False, provider_declared=True
        ) == "text-only"
        assert resolve_vision_status(
            model_id="mystery-7b", provider_declared=True
        ) == "vision"
        assert resolve_vision_status(model_id="gpt-4o") == "vision"
        assert resolve_vision_status(model_id="mystery-7b") == "unknown"

    def test_provider_declared_vision_survives_unknown_static_table(self):
        config = _make_config(model_id="mystery-7b")
        entry = config.models["test-model"].model_copy(update={"vision_declared": True})
        result = apply_runtime_capabilities(
            config.model_copy(update={"models": {"test-model": entry}})
        )
        assert Requirement.VISION in result.models["test-model"].capabilities

    def test_live_negative_probe_wins_over_provider_and_reports_text_only(self):
        config = _make_config(model_id="gpt-4o")
        entry = config.models["test-model"].model_copy(update={"vision_declared": True})
        result = apply_runtime_capabilities(
            config.model_copy(update={"models": {"test-model": entry}}),
            probe_results={"test-model": False},
        )
        assert Requirement.VISION not in result.models["test-model"].capabilities
        assert result.models["test-model"].vision_probe is False

    def test_probe_none_falls_back_to_table(self):
        """Probe result of None falls through to the static table."""
        # gpt-4o → table says True
        config = _make_config(vision_pin=None, has_vision_cap=False, model_id="gpt-4o")
        result = apply_runtime_capabilities(config, probe_results={"test-model": None})
        assert Requirement.VISION in result.models["test-model"].capabilities

    def test_unknown_model_with_none_probe_is_not_vision(self):
        """Unknown model + probe None → table says False → no VISION."""
        config = _make_config(vision_pin=None, has_vision_cap=False, model_id="mystery-7b")
        result = apply_runtime_capabilities(config, probe_results={"test-model": None})
        assert Requirement.VISION not in result.models["test-model"].capabilities

    def test_claude_model_gets_vision_via_table_with_no_probe(self):
        """A Claude entry (Anthropic family) gets VISION from the table even
        without a probe, since the table rule is authoritative for Anthropic."""
        entry = ModelEntry(
            model_id="anthropic/claude-3-5-sonnet",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            context_window=200_000,
            family="anthropic",
        )
        config = RouterConfig(models={"claude": entry}, default_model="claude")
        result = apply_runtime_capabilities(config)
        assert Requirement.VISION in result.models["claude"].capabilities

    def test_openai_embedding_model_never_gets_vision(self):
        """Embedding models must never get VISION from any source."""
        entry = ModelEntry(
            model_id="text-embedding-3-large",
            provider="openai",
            base_url="http://api.openai.com/v1",
            context_window=8192,
        )
        config = RouterConfig(models={"embedder": entry}, default_model="embedder")
        # Even if the probe somehow returned True, the table returns False for
        # embeddings — but the probe is run BEFORE the table so it would win.
        # Test the "no probe" path to ensure the table prevents false positives.
        result = apply_runtime_capabilities(config)
        assert Requirement.VISION not in result.models["embedder"].capabilities

    def test_entries_with_no_base_url_are_skipped(self):
        """NLI cross-encoder and other non-backend entries must be unchanged."""
        entry = ModelEntry(
            model_id="bge-reranker-v2-m3",
            provider="local",
            context_window=512,
            base_url=None,
        )
        config = RouterConfig(models={"nli": entry}, default_model="nli")
        # Provide a probe result that WOULD grant vision if the entry were processed
        result = apply_runtime_capabilities(config, probe_results={"nli": True})
        # base_url is None → entry untouched
        assert Requirement.VISION not in result.models["nli"].capabilities

    def test_no_change_returns_same_config(self):
        """When no capability needs changing, the returned config is the same object."""
        config = _make_config(vision_pin=None, has_vision_cap=False, model_id="mystery-7b")
        result = apply_runtime_capabilities(config)
        assert result is config  # no change → same object

    def test_driver_vision_env_back_compat(self, monkeypatch):
        """DISCO_DRIVER_VISION=1 still enables vision on driver-local (back-compat)."""
        monkeypatch.setenv("PMX_DRIVER_VISION", "1")
        config = default_config()
        result = apply_runtime_capabilities(config)
        assert Requirement.VISION in result.models["driver-local"].capabilities

    def test_driver_vision_env_off_strips_vision(self, monkeypatch):
        """DISCO_DRIVER_VISION=0 strips vision from driver-local (back-compat)."""
        monkeypatch.setenv("PMX_DRIVER_VISION", "0")
        config = default_config()
        result = apply_runtime_capabilities(config)
        assert Requirement.VISION not in result.models["driver-local"].capabilities


# ===========================================================================
# V2 — probe_vision / probe_all_vision (async, stubbed network)
# ===========================================================================


class TestProbeVision:
    """V2: the wiring-layer async probe functions."""

    @pytest.mark.asyncio
    async def test_llamacpp_props_vision_true(self):
        """llama.cpp /props with modalities.vision=true → probe returns True."""
        from disco.core.llm.wiring import _VISION_PROBE_CACHE, probe_vision

        _VISION_PROBE_CACHE.clear()  # reset memo for test isolation

        props_response = {"modalities": {"vision": True, "completion": True}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = props_response

        async def fake_get(url: str, **kw):
            assert "/props" in url
            return mock_resp

        mock_client = AsyncMock()
        mock_client.get = fake_get

        result = await probe_vision("http://192.168.1.231:18080/v1", "Qwen.gguf", mock_client)
        assert result is True

    @pytest.mark.asyncio
    async def test_llamacpp_props_vision_false(self):
        """llama.cpp /props with modalities.vision=false → probe returns False."""
        from disco.core.llm.wiring import _VISION_PROBE_CACHE, probe_vision

        _VISION_PROBE_CACHE.clear()

        props_response = {"modalities": {"vision": False, "completion": True}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = props_response

        async def fake_get(url: str, **kw):
            return mock_resp

        mock_client = AsyncMock()
        mock_client.get = fake_get

        result = await probe_vision("http://192.168.1.231:18080/v1", "Qwen.gguf", mock_client)
        assert result is False

    @pytest.mark.asyncio
    async def test_openrouter_input_modalities_image_gives_true(self):
        """OpenRouter model with input_modalities including 'image' → True."""
        from disco.core.llm.wiring import _VISION_PROBE_CACHE, probe_vision

        _VISION_PROBE_CACHE.clear()

        model_id = "google/gemini-3-flash-preview"
        or_response = {
            "data": [
                {
                    "id": model_id,
                    "architecture": {
                        "input_modalities": ["text", "image"],
                    },
                },
            ]
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = or_response

        async def fake_get(url: str, **kw):
            return mock_resp

        mock_client = AsyncMock()
        mock_client.get = fake_get

        result = await probe_vision("https://openrouter.ai/api/v1", model_id, mock_client)
        assert result is True

    @pytest.mark.asyncio
    async def test_openrouter_text_only_model_gives_false(self):
        """OpenRouter model with only 'text' modality → False."""
        from disco.core.llm.wiring import _VISION_PROBE_CACHE, probe_vision

        _VISION_PROBE_CACHE.clear()

        model_id = "some-text-only-model"
        or_response = {
            "data": [
                {
                    "id": model_id,
                    "architecture": {
                        "input_modalities": ["text"],
                    },
                },
            ]
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = or_response

        async def fake_get(url: str, **kw):
            return mock_resp

        mock_client = AsyncMock()
        mock_client.get = fake_get

        result = await probe_vision("https://openrouter.ai/api/v1", model_id, mock_client)
        assert result is False

    @pytest.mark.asyncio
    async def test_openrouter_provider_uses_models_on_an_approved_custom_origin(self):
        """The provider type, not a model-name special case, selects OpenRouter metadata."""
        from disco.core.llm.wiring import _VISION_PROBE_CACHE, probe_vision

        _VISION_PROBE_CACHE.clear()
        model_id = "vendor/vision-model"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {
                    "id": model_id,
                    "architecture": {"input_modalities": ["text", "image"]},
                }
            ]
        }
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        result = await probe_vision(
            "https://router.example.test/api/v1/",
            model_id,
            mock_client,
            provider="openrouter",
        )

        assert result is True
        mock_client.get.assert_awaited_once_with(
            "https://router.example.test/api/v1/models", timeout=10.0
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("base_url", "provider"),
        [
            ("https://api.openai.com/v1", "openai"),
            ("https://api.anthropic.com/v1", "anthropic"),
            ("https://opencode.ai/zen/go/v1", "opencode-go"),
            ("https://openrouter.ai.attacker.example/v1", "generic-cloud"),
        ],
    )
    async def test_other_cloud_providers_do_not_receive_invented_network_probes(
        self, base_url: str, provider: str
    ):
        """Clouds without documented modality metadata defer directly to the table."""
        from disco.core.llm.wiring import _VISION_PROBE_CACHE, probe_vision

        _VISION_PROBE_CACHE.clear()
        mock_client = AsyncMock()

        result = await probe_vision(
            base_url,
            "opaque-model-id",
            mock_client,
            provider=provider,
        )

        assert result is None
        mock_client.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_llamacpp_provider_probes_props_on_public_host(self):
        """An explicit llama.cpp provider preserves `/props` on non-LAN deployments."""
        from disco.core.llm.wiring import _VISION_PROBE_CACHE, probe_vision

        _VISION_PROBE_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"modalities": {"vision": True}}
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        result = await probe_vision(
            "https://models.example.test/v1",
            "Qwen.gguf",
            mock_client,
            provider="llama.cpp",
        )

        assert result is True
        mock_client.get.assert_awaited_once_with("https://models.example.test/props", timeout=10.0)

    @pytest.mark.asyncio
    async def test_network_error_returns_none_no_raise(self):
        """A network error during the probe must return None and never raise."""
        from disco.core.llm.wiring import _VISION_PROBE_CACHE, probe_vision

        _VISION_PROBE_CACHE.clear()

        async def fake_get(url: str, **kw):
            raise httpx.ConnectError("connection refused")

        mock_client = AsyncMock()
        mock_client.get = fake_get

        # Must not raise — fail-soft contract
        result = await probe_vision("http://192.168.1.1:18080/v1", "any-model", mock_client)
        assert result is None

    @pytest.mark.asyncio
    async def test_probe_result_is_memoized(self):
        """Second call with the same (base_url, model_id) returns the cached result."""
        from disco.core.llm.wiring import _VISION_PROBE_CACHE, probe_vision

        _VISION_PROBE_CACHE.clear()

        call_count = 0

        props_response = {"modalities": {"vision": True}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = props_response

        async def fake_get(url: str, **kw):
            nonlocal call_count
            call_count += 1
            return mock_resp

        mock_client = AsyncMock()
        mock_client.get = fake_get

        r1 = await probe_vision("http://host:8080/v1", "model-x", mock_client)
        r2 = await probe_vision("http://host:8080/v1", "model-x", mock_client)
        assert r1 is True
        assert r2 is True
        assert call_count == 1  # network called only once

    @pytest.mark.asyncio
    async def test_probe_all_vision_returns_dict_keyed_by_model_key(self):
        """probe_all_vision returns model-key → vision result dict."""
        from disco.core.llm.wiring import (
            _VISION_PROBE_CACHE,
            probe_all_vision_with_approvals,
        )

        _VISION_PROBE_CACHE.clear()

        # Build a minimal config with two models
        entry_a = ModelEntry(
            model_id="Qwen.gguf",
            provider="local",
            base_url="http://localhost:18080/v1",
            context_window=4096,
        )
        entry_nli = ModelEntry(
            model_id="bge-reranker-v2-m3",
            provider="nli",
            base_url=None,  # no base_url → should be skipped
            context_window=512,
        )
        config = RouterConfig(
            models={"local-model": entry_a, "nli-model": entry_nli},
            default_model="local-model",
            trusted_origins=("http://localhost:18080",),
        )

        # Stub the /props response for the local endpoint
        props_resp = MagicMock()
        props_resp.status_code = 200
        props_resp.json.return_value = {"modalities": {"vision": True}}

        async def fake_get(url: str, **kw):
            return props_resp

        # Patch httpx.AsyncClient so probe_all_vision uses the stub
        with patch("httpx.AsyncClient") as mock_async_client_cls:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.get = fake_get
            mock_async_client_cls.return_value = mock_ctx

            result = await probe_all_vision_with_approvals(
                config,
                origin_approved=lambda *_args: True,
            )

        assert "local-model" in result
        assert result["local-model"] is True
        # NLI has no base_url → not in results
        assert "nli-model" not in result

    @pytest.mark.asyncio
    async def test_probe_all_vision_never_probes_unsupported_cloud_provider(self):
        """Startup may inspect several models on one cloud without emitting `/props` calls."""
        from disco.core.llm.wiring import (
            _VISION_PROBE_CACHE,
            probe_all_vision_with_approvals,
        )

        _VISION_PROBE_CACHE.clear()
        base_url = "https://cloud-provider.example/v1"
        models = {
            f"cloud-{index}": ModelEntry(
                model_id=f"model-{index}",
                provider="generic-cloud",
                base_url=base_url,
                context_window=8192,
            )
            for index in range(3)
        }
        config = RouterConfig(models=models, default_model="cloud-0")

        with patch("httpx.AsyncClient") as mock_async_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = False
            mock_async_client_cls.return_value = mock_client

            result = await probe_all_vision_with_approvals(
                config,
                origin_approved=lambda *_args: True,
            )

        assert result == {"cloud-0": None, "cloud-1": None, "cloud-2": None}
        mock_client.get.assert_not_awaited()


# ===========================================================================
# Integration: probe → apply_runtime_capabilities → VISION on entry
# ===========================================================================


class TestVisionIntegration:
    """End-to-end: probe result → apply_runtime_capabilities → VISION on entry."""

    def test_llamacpp_probe_result_gives_driver_vision(self):
        """Simulates: llama.cpp /props returned True → driver-local gains VISION
        without the deprecated env var."""
        config = default_config()
        # Remove VISION from driver-local if it's there (simulate no env var)
        driver = config.models["driver-local"]
        caps = set(driver.capabilities) - {Requirement.VISION}
        config = config.model_copy(
            update={
                "models": {
                    **config.models,
                    "driver-local": driver.model_copy(update={"capabilities": frozenset(caps)}),
                }
            }
        )

        # Simulate probe result
        probe_results = {"driver-local": True}
        result = apply_runtime_capabilities(config, probe_results=probe_results)
        assert Requirement.VISION in result.models["driver-local"].capabilities

    def test_openrouter_vision_model_gets_vision_from_probe(self):
        """A cloud OpenRouter model gets VISION from a probe result of True."""
        entry = ModelEntry(
            model_id="google/gemini-3-flash-preview",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            context_window=1_048_576,
            capabilities=frozenset(),  # no capabilities declared yet
            family="gemini",
        )
        config = RouterConfig(
            models={"or-flash": entry},
            default_model="or-flash",
        )
        result = apply_runtime_capabilities(config, probe_results={"or-flash": True})
        assert Requirement.VISION in result.models["or-flash"].capabilities

    def test_pinned_false_blocks_probe_true(self):
        """vision=False pin blocks even a probe result of True."""
        entry = ModelEntry(
            model_id="google/gemini-3-flash-preview",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            context_window=1_048_576,
            capabilities=frozenset(),
            vision=False,  # explicit pin: no vision
        )
        config = RouterConfig(models={"or-flash": entry}, default_model="or-flash")
        result = apply_runtime_capabilities(config, probe_results={"or-flash": True})
        assert Requirement.VISION not in result.models["or-flash"].capabilities


# ===========================================================================
# W4 — ANCHORED_EDIT on capable models in default_config
# ===========================================================================


class TestAnchoredEdit:
    """W4: capable models get ANCHORED_EDIT; weak/unknown do not."""

    def test_driver_overflow_has_anchored_edit(self):
        """claude-3.5-sonnet (driver-overflow) is a confirmed capable diff editor."""
        config = default_config()
        caps = config.models["driver-overflow"].capabilities
        assert Requirement.ANCHORED_EDIT in caps

    def test_driver_local_does_not_have_anchored_edit(self):
        """driver-local (Qwen 27B local) does not have ANCHORED_EDIT by default
        — unknown/local models fall back to whole-file writes."""
        config = default_config()
        caps = config.models["driver-local"].capabilities
        assert Requirement.ANCHORED_EDIT not in caps

    def test_local_small_models_do_not_have_anchored_edit(self):
        """rewriter-local and summarizer-local (Gemma E2B) do not get it."""
        config = default_config()
        for key in ("rewriter-local", "summarizer-local"):
            assert Requirement.ANCHORED_EDIT not in config.models[key].capabilities


# ===========================================================================
# V7 / W6 — prompts are vision-aware
# ===========================================================================


class TestVisionAwarePrompts:
    """V7: the visual-verify mandate changes based on vision capability."""

    def _prompts_for(self, *, vision: bool) -> DriverPrompts:
        return DriverPrompts()

    def _system_prompt(self, *, vision: bool, mode: OperatingMode) -> str:
        caps: frozenset[Requirement] = frozenset({Requirement.VISION}) if vision else frozenset()
        driver = DriverPrompts()
        return driver.system_prompt(
            model_family="qwen",
            mode=mode,
            role=ModelRole.AGENT_DRIVER,
            capabilities=caps,
            assist=False,
        )

    def test_vision_driver_includes_screenshot_mandate(self):
        """When the driver has VISION, the prompt includes the vision bullet that
        mentions checking the screenshot."""
        prompt = self._system_prompt(vision=True, mode=OperatingMode.LONG_HORIZON)
        assert "screenshot" in prompt.lower()

    def test_non_vision_driver_has_no_screenshot_mandate(self):
        """When the driver has NO VISION, the vision bullet is absent (no pressure
        to 'see it render' — the build-exit/lint finish gate stands instead)."""
        prompt = self._system_prompt(vision=False, mode=OperatingMode.LONG_HORIZON)
        # The capability-gated vision bullet must be absent
        assert "You can SEE your latest browser screenshot" not in prompt

    def test_single_verify_pass_in_execution_prompt(self):
        """The execution prompt states 'verify once' — anti-loop framing."""
        prompt = self._system_prompt(vision=False, mode=OperatingMode.LONG_HORIZON)
        # The prompt should not re-add the unbounded "a build you have not seen
        # render is not finished" pressure — it should use the bounded framing.
        prompt_lower = prompt.lower()
        assert (
            "verify once" in prompt_lower
            or "one browser load" in prompt_lower
            or "once" in prompt_lower
        )

    def test_anti_monolith_in_execution_prompt(self):
        """Modularity is guidance; explicit user/target file shapes win."""
        prompt = self._system_prompt(vision=False, mode=OperatingMode.LONG_HORIZON)
        assert "user or target requires a particular layout" in prompt
        assert "large files are supported" in prompt.lower()
        assert "800 lines" not in prompt
        assert "48KB" not in prompt

    def test_vision_bullet_caps_gated(self):
        """The vision bullet appears IFF VISION is in capabilities."""
        vision_prompt = self._system_prompt(vision=True, mode=OperatingMode.LONG_HORIZON)
        no_vision_prompt = self._system_prompt(vision=False, mode=OperatingMode.LONG_HORIZON)
        # Vision prompt should have the vision bullet
        assert "You can SEE your latest browser screenshot" in vision_prompt
        # Non-vision prompt must NOT have it
        assert "You can SEE your latest browser screenshot" not in no_vision_prompt
