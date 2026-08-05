import json

import pytest
from disco.app_server.config_state import ConfigState
from disco.core.llm.config import ImageGenSettings, ModelEntry, default_config
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.types import ModelRole
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.image_gen import (
    ImageGenArgs,
    ImageGenNotConfigured,
    ImageGenTool,
    select_image_backend,
)
from disco.tools.builtin.slides import SlidesGenerateArgs, SlidesTool


def _write_config(tmp_path, monkeypatch, cfg):
    config_path = tmp_path / "disco-config.json"
    config_path.write_text(json.dumps(cfg.model_dump(mode="json")))
    monkeypatch.setenv("DISCO_CONFIG", str(config_path))
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "disco-secrets.json"))
    monkeypatch.setenv("DISCO_APPROVALS", str(tmp_path / "disco-approved-origins.json"))
    monkeypatch.setenv("DISCO_SECRET_KEY", "authorb-origin-approval-test-secret")
    monkeypatch.delenv("DISCO_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("PMX_OPENROUTER_API_KEY", raising=False)
    store = ConfigStore(config_path)
    # PKG-11-SETTINGS moved origin approval off ConfigStore onto its own
    # authority (`dbdc4afa`, PY-0459: "an approval registry is not a config-file
    # writer"), accepted at `a0e4b9b3` and recorded in public-api.json's
    # member_transitions. Every other consumer in the tree already calls
    # `store.approvals.approve_origin`; these were left behind only because
    # tests/unbiased_gate was outside `testpaths` and so ran nowhere.
    # Call route migrated; the assertions below are untouched.
    store.approvals.approve_origin(
        "https://openrouter.ai/api/v1",
        "image:openrouter",
        "openrouter",
    )
    driver = cfg.models[cfg.model_for(ModelRole.AGENT_DRIVER)]
    store.approvals.approve_origin(
        driver.base_url,
        f"model:{driver.provider}",
        driver.api_key_env,
    )
    return config_path


def _tool_context(sandbox=None) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path="/workspace",
        timeout_s=30,
        capabilities=None,
        owner_id="authorB",
        conversation_id="conv_authorB",
    )


def test_w04_openrouter_display_label_drops_or_prefix_but_keeps_catalogue_key(tmp_path):
    cfg = default_config().model_copy(
        update={
            "models": {
                "or-anthropic-claude-opus": ModelEntry(
                    model_id="anthropic/claude-opus",
                    provider="openrouter",
                    context_window=200_000,
                    price_in_per_m=15.0,
                    price_out_per_m=75.0,
                )
            },
            "default_model": "or-anthropic-claude-opus",
            "assignments": {},
        }
    )

    state = ConfigState(store=ConfigStore(tmp_path / "w04-config.json", base_factory=lambda: cfg))
    model = next(m for m in state.models.models() if m.id == "or-anthropic-claude-opus")

    assert model.id == "or-anthropic-claude-opus"
    assert not model.label.startswith("Or ")
    assert "Anthropic Claude Opus" in model.label


def test_w05_zero_price_subscription_is_not_free_or_local_in_catalogue_mapping(tmp_path):
    cfg = default_config().model_copy(
        update={
            "models": {
                "minimax-subscription": ModelEntry(
                    model_id="minimax/minimax-m2",
                    provider="minimax",
                    context_window=200_000,
                    price_in_per_m=0.0,
                    price_out_per_m=0.0,
                    pricing_mode="subscription",
                )
            },
            "default_model": "minimax-subscription",
            "assignments": {},
        }
    )

    state = ConfigState(store=ConfigStore(tmp_path / "w05-config.json", base_factory=lambda: cfg))
    model = next(m for m in state.models.models() if m.id == "minimax-subscription")

    assert model.pricing_mode == "subscription"
    assert model.provider != "local"
    assert model.price_in_per_m == 0.0
    assert model.price_out_per_m == 0.0


def test_w50_unconfigured_image_backend_fails_loudly_instead_of_procedural_fallback(
    tmp_path, monkeypatch
):
    cfg = default_config().model_copy(
        update={"image_gen": ImageGenSettings(provider="openrouter", model="")}
    )
    _write_config(tmp_path, monkeypatch, cfg)

    with pytest.raises(ImageGenNotConfigured) as exc:
        select_image_backend()

    msg = str(exc.value)
    assert "Image generation isn't configured" in msg
    assert "Settings" in msg
    assert "procedural" not in msg.lower()


@pytest.mark.asyncio
async def test_w50_image_tool_returns_clear_not_configured_error_without_fake_image(
    tmp_path, monkeypatch
):
    cfg = default_config().model_copy(
        update={"image_gen": ImageGenSettings(provider="openrouter", model="")}
    )
    _write_config(tmp_path, monkeypatch, cfg)

    outcome = await ImageGenTool().run(
        ImageGenArgs(prompt="a deck cover image", filename="cover"),
        _tool_context(),
    )

    assert outcome.success is False
    assert outcome.error == "image_gen_not_configured"
    assert "Image generation isn't configured" in outcome.content
    assert outcome.artifacts == []


def test_w50_legacy_procedural_config_loads_as_openrouter_without_losing_config(tmp_path):
    cfg = default_config()
    payload = cfg.model_dump(mode="json")
    payload["image_gen"]["provider"] = "procedural"
    payload["models"]["authorb-keep-me"] = {
        "model_id": "authorb/model",
        "provider": "authorb",
        "context_window": 12345,
        "capabilities": [],
        "price_in_per_m": 0.0,
        "price_out_per_m": 0.0,
    }
    config_path = tmp_path / "legacy-procedural.json"
    config_path.write_text(json.dumps(payload))

    loaded = ConfigStore(config_path).load()

    assert loaded.image_gen.provider == "openrouter"
    assert "authorb-keep-me" in loaded.models


@pytest.mark.asyncio
async def test_w50_slides_degrade_to_image_less_when_image_backend_unconfigured(
    tmp_path, monkeypatch
):
    """W-50 REAL degrade path: an UNCONFIGURED image backend must NOT sink deck
    generation. With image_gen set to a provider but no model (the real
    "not configured" state), the REAL select_image_backend() raises — and the slides
    pipeline must still run end-to-end (generate_deck → lower_deck → C3 render) and
    write a real, image-less deck artifact to the workspace, without crashing.

    Minimal mocking: ONLY the LLM network call is stubbed with canned AuthoredDeck JSON
    (which includes an image_prompt slide, so an omitted image is observable). The
    backend selector, the lowering, and the renderer all execute for real.
    """
    from unittest.mock import AsyncMock, patch

    from disco.tools.sandbox.base import SandboxSpec
    from disco.tools.sandbox.process import ProcessSandboxInstance

    cfg = default_config().model_copy(
        update={"image_gen": ImageGenSettings(provider="openrouter", model="")}
    )
    _write_config(tmp_path, monkeypatch, cfg)

    # The REAL selector raises for this config — this is what the pipeline must survive
    # (no mock of select_image_backend; we prove it genuinely raises first).
    with pytest.raises(ImageGenNotConfigured):
        select_image_backend()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sbx = ProcessSandboxInstance(
        id="w50-sbx",
        owner_id="authorB",
        conversation_id="conv_authorB",
        spec=SandboxSpec(),
        workspace=workspace,
    )
    ctx = _tool_context(sandbox=sbx)

    outline_json = json.dumps(
        {
            "title": "Q3 Market Brief",
            "theme": "disco-light",
            "slides": [
                {"type": "title", "title": "Q3 Market Brief"},
                {"type": "bullets", "title": "Demand Signals"},
                {"type": "image_right", "title": "Competitive Map"},
                {"type": "closing", "title": "Takeaways"},
            ],
        }
    )
    full_json = json.dumps(
        {
            "title": "Q3 Market Brief",
            "theme": "disco-light",
            "slides": [
                {"type": "title", "title": "Q3 Market Brief", "body": ["Where the quarter stands"]},
                {
                    "type": "bullets",
                    "title": "Demand Signals",
                    "body": ["Inbound up 18%", "Churn flat", "Pipeline widening"],
                },
                {
                    "type": "image_right",
                    "title": "Competitive Map",
                    "body": ["Two new entrants"],
                    # An image_prompt slide — proves the image is OMITTED, not rendered.
                    "image_prompt": "A 2x2 competitive landscape quadrant chart",
                },
                {"type": "closing", "title": "Takeaways", "body": ["Hold the lead"]},
            ],
        }
    )

    with patch(
        "disco.tools.builtin._slides_pipeline._call_llm", new_callable=AsyncMock
    ) as mock_llm:
        mock_llm.side_effect = [outline_json, full_json]
        outcome = await SlidesTool().run(
            SlidesGenerateArgs(goal="A four-slide market brief", filename="brief", format="html"),
            ctx,
        )

    # The deck was really produced (not a one-line crash fallback) and did not raise.
    assert outcome.success is True, outcome.content
    assert outcome.error is None
    # A real rendered artifact landed in the workspace.
    assert "brief.html" in outcome.artifacts
    artifact = workspace / "brief.html"
    assert artifact.exists() and artifact.stat().st_size > 0
    # All four slides lowered + rendered via the real C3 brand renderer.
    structured = outcome.structured or {}
    assert structured.get("slide_count") == 4
    assert structured.get("renderer") == "c3-brand"
    # DEGRADED to image-less: no image asset was generated/written for the image_prompt
    # slide (the whole point of W-50 — omit images, don't crash).
    assert not list(workspace.glob("*_img_*.png"))
