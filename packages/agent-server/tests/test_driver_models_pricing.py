"""W-05-fu — the driver-models endpoint exposes pricing_mode so the Build picker
can tell a SUBSCRIPTION model (flat-rate plan, price 0/token) apart from a
genuinely FREE one. `free` is keyed off the effective mode, so a subscription
model is NOT reported free.

Hermetic: the live-model probe cache is pre-seeded so driver_models() never
touches the network (it serves the static fallback from cache).
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime
from disco.agent_server import runtime as rt_mod
from disco.core import SqliteEventStore
from disco.core.llm import ModelEntry, Requirement, RouterConfig
from disco.core.llm.config_store import ConfigStore


def _seed_probe(*base_urls: str) -> None:
    # Bare-dict (legacy) cache entry is treated as fresh → no network probe.
    for u in base_urls:
        rt_mod._LIVE_MODEL_PROBE_CACHE[u] = {"model_id": None, "n_ctx": None}


def test_driver_models_exposes_pricing_mode_and_honest_free(tmp_path):
    cfg = RouterConfig(
        models={
            "local-free": ModelEntry(
                model_id="lf",
                provider="local",
                context_window=8192,
                base_url="http://lf:1/v1",
                capabilities=frozenset({Requirement.TOOL_CALLING}),
            ),
            "driver-minimax": ModelEntry(
                model_id="mm",
                provider="minimax",
                context_window=8192,
                base_url="http://mm:1/v1",
                capabilities=frozenset({Requirement.TOOL_CALLING}),
                pricing_mode="subscription",
            ),
            "or-paid": ModelEntry(
                model_id="op",
                provider="openrouter",
                context_window=8192,
                base_url="http://op:1/v1",
                capabilities=frozenset({Requirement.TOOL_CALLING}),
                price_out_per_m=15.0,
            ),
        },
        default_model="local-free",
    )
    _seed_probe("http://lf:1/v1", "http://mm:1/v1", "http://op:1/v1")

    # Point the config store at a nonexistent file so load() uses ONLY our
    # base_factory (no on-disk disco-config.json shadowing the test fixture).
    store_cfg = ConfigStore(path=tmp_path / "absent.json", base_factory=lambda: cfg)
    rt = ConversationRuntime(SqliteEventStore(":memory:"), config_store=store_cfg)
    rt._origin_approved = lambda *_args: True
    out = rt.driver_models()
    by_id = {m["id"]: m for m in out["models"]}

    # Every model carries an effective pricing_mode.
    assert all("pricing_mode" in m for m in out["models"])

    # Genuinely free local model.
    assert by_id["local-free"]["pricing_mode"] == "free"
    assert by_id["local-free"]["free"] is True

    # Subscription: price 0/token but NOT free — the W-05-fu bug.
    assert by_id["driver-minimax"]["pricing_mode"] == "subscription"
    assert by_id["driver-minimax"]["free"] is False

    # Metered paid model.
    assert by_id["or-paid"]["pricing_mode"] == "metered"
    assert by_id["or-paid"]["free"] is False


def test_driver_models_excludes_models_the_live_router_cannot_wire(tmp_path):
    cfg = RouterConfig(
        models={
            "ready": ModelEntry(
                model_id="ready-wire",
                provider="ready-provider",
                context_window=8192,
                base_url="https://ready.example/v1",
                api_key_env="provider_ready",
                capabilities=frozenset({Requirement.TOOL_CALLING}),
            ),
            "unapproved": ModelEntry(
                model_id="unapproved-wire",
                provider="unapproved-provider",
                context_window=8192,
                base_url="https://unapproved.example/v1",
                api_key_env="provider_unapproved",
                capabilities=frozenset({Requirement.TOOL_CALLING}),
            ),
            "locked": ModelEntry(
                model_id="locked-wire",
                provider="locked-provider",
                context_window=8192,
                base_url="https://locked.example/v1",
                api_key_env="provider_locked",
                capabilities=frozenset({Requirement.TOOL_CALLING}),
            ),
        },
        default_model="ready",
    )
    _seed_probe(
        "https://ready.example/v1",
        "https://unapproved.example/v1",
        "https://locked.example/v1",
    )
    store_cfg = ConfigStore(path=tmp_path / "absent.json", base_factory=lambda: cfg)
    rt = ConversationRuntime(SqliteEventStore(":memory:"), config_store=store_cfg)
    rt._origin_approved = lambda url, *_args: "unapproved" not in url
    rt._resolve_secret = lambda ref: "decrypted" if ref == "provider_ready" else None

    out = rt.driver_models()

    assert [model["id"] for model in out["models"]] == ["ready"]
    assert out["default"] == "ready"
