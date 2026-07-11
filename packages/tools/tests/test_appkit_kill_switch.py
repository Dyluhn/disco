"""DISCO_APPKIT_ENABLED=0 — the AppKit kill switch, tools layer.

Full separation with one config change: flag off ⇒ the app_* v2 mutators leave
the default registry entirely (unknown_tool everywhere) and the lead_form
starter refuses with a free-form alternative. Flag absent ⇒ byte-identical to
pre-switch behavior. The legacy governed pair (app_set_tweak /
app_snapshot_version) is a DIFFERENT surface and must survive the switch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from disco.core.flags import APPKIT_ENABLED_ENV, appkit_enabled
from disco.tools.builtin import build_default_registry
from disco.tools.builtin.app_kit import APPKIT_V2_TOOLS
from disco.tools.builtin.scaffold_starter import ScaffoldStarterArgs, ScaffoldStarterTool

_V2_NAMES = frozenset(cls().definition.name for cls in APPKIT_V2_TOOLS)
# app_snapshot_version is owned by the LEGACY governed tool in the default
# registry (the v2 duplicate is always skipped there) — it is expected to
# survive the kill switch.
_V2_ONLY_NAMES = _V2_NAMES - {"app_snapshot_version"}


def test_flag_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(APPKIT_ENABLED_ENV, raising=False)
    assert appkit_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "No", " OFF ", "disabled"])
def test_falsy_values_disable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(APPKIT_ENABLED_ENV, value)
    assert appkit_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "banana"])
def test_anything_else_stays_on(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # A feature flag fails OPEN on unrecognized values: only an explicit,
    # recognized disable removes a shipped surface.
    monkeypatch.setenv(APPKIT_ENABLED_ENV, value)
    assert appkit_enabled() is True


def test_registry_ships_appkit_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(APPKIT_ENABLED_ENV, raising=False)
    names = build_default_registry().names()
    assert _V2_ONLY_NAMES <= set(names)


def test_registry_drops_appkit_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(APPKIT_ENABLED_ENV, "0")
    names = set(build_default_registry().names())
    assert names.isdisjoint(_V2_ONLY_NAMES), sorted(names & _V2_ONLY_NAMES)
    # The legacy governed surface is untouched by the switch.
    assert "app_snapshot_version" in names


@pytest.mark.asyncio
async def test_lead_form_starter_refuses_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APPKIT_ENABLED_ENV, "0")
    ctx = SimpleNamespace(sandbox=object(), starter_kit=None)
    outcome = await ScaffoldStarterTool().run(
        ScaffoldStarterArgs(title="t", kind="lead_form"), ctx  # type: ignore[arg-type]
    )
    assert outcome.success is False
    assert outcome.error == "appkit_disabled"
    # The refusal must teach the free-form alternative, not dead-end.
    assert "app_shell" in (outcome.content or "")
