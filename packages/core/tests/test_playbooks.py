"""Playbooks: every bundled playbook parses, has the header the catalog needs, and
is reachable by id; the registry is stable across calls."""

from __future__ import annotations

from pathlib import Path

import pytest
from disco.core.playbooks import PlaybookRegistry, parse_playbook

EXPECTED_IDS = {
    "ai-assistant",
    "alerts-and-broadcast",
    "auth-and-roles",
    "booking-and-inventory",
    "chat-rooms-and-dms",
    "email-notifications",
    "fullstack-stack",
    "market-data-and-news",
    "media-uploads",
    "multiplayer-game",
    "payments-stripe",
    "rag-documents",
    "realtime-and-presence",
    "sandbox-verification",
    "voice-dictation",
}


def test_bundled_packs_are_the_expected_set():
    assert set(PlaybookRegistry.default().ids()) == EXPECTED_IDS


def test_every_pack_has_title_summary_and_a_prove_it_section():
    for pack_id in PlaybookRegistry.default().ids():
        pack = PlaybookRegistry.default().get(pack_id)
        assert pack is not None
        assert pack.title and pack.summary
        assert "## Prove it" in pack.raw or "## Delivery" in pack.raw, pack_id


def test_index_lists_every_pack_once_with_its_summary():
    registry = PlaybookRegistry.default()
    lines = registry.index().splitlines()
    assert len(lines) == len(registry.ids())
    for pack_id in registry.ids():
        pack = registry.get(pack_id)
        assert pack is not None
        assert f"- {pack_id} — {pack.summary}" in lines


def test_unknown_id_is_none():
    assert PlaybookRegistry.default().get("no-such-pack") is None


def test_parse_requires_title_and_summary():
    with pytest.raises(ValueError):
        parse_playbook("x", "no header here\n")
    with pytest.raises(ValueError):
        parse_playbook("x", "# Title only\n\nbody\n")
    pack = parse_playbook("x", "# Title\n\n> Summary line\n\nbody\n")
    assert (pack.title, pack.summary) == ("Title", "Summary line")


def test_explicit_directory_overrides_the_bundle(tmp_path: Path):
    (tmp_path / "one.md").write_text("# One\n\n> first\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    registry = PlaybookRegistry(pack_dir=tmp_path)
    assert registry.ids() == ("one",)
    assert registry.index() == "- one — first"


def test_packs_never_contain_secret_values():
    """Playbooks teach env var NAMES; a literal key would be copied into generated apps."""
    for pack_id in PlaybookRegistry.default().ids():
        raw = PlaybookRegistry.default().get(pack_id).raw  # type: ignore[union-attr]
        for marker in ("sk_live_", "sk_test_5", "whsec_1", "AKIA", "ghp_"):
            assert marker not in raw, (pack_id, marker)
