"""P9B tests: .disco/tweaks.json IO (file_exists-first read, bytes write, require) + the
merge_tweak_defaults reconciliation (defaults fill, present-value coercion, TweaksError)."""

from __future__ import annotations

import pytest
from disco.core.tweaks import TweakEditor, TweakField, TweakSpec
from disco.tools.builtin.tweaks_io import (
    TWEAKS_PATH,
    TweaksError,
    merge_tweak_defaults,
    read_tweakspec,
    require_tweakspec,
    write_tweakspec,
)
from tool_fakes import FakeSandboxInstance


def _spec() -> TweakSpec:
    return TweakSpec(
        fields=(
            TweakField(
                key="lead.phone",
                label="Phone",
                editor=TweakEditor.BOOLEAN,
                affects=("lead_form.phone",),
                default=False,
            ),
            TweakField(
                key="hero.count",
                label="CTAs",
                editor=TweakEditor.INT,
                min=1,
                max=5,
                step=1,
                affects=("hero.ctas",),
                default=2,
            ),
            TweakField(
                key="brand.accent",
                label="Accent",
                editor=TweakEditor.PALETTE,
                colors=("#0a84ff", "#e2725b"),
                affects=("design.accent",),
            ),  # no default
        )
    )


# --- IO round-trip ------------------------------------------------------------
@pytest.mark.asyncio
async def test_write_then_read_round_trip() -> None:
    sbx = FakeSandboxInstance()
    spec = _spec()
    await write_tweakspec(sbx, spec)
    assert sbx._fs[TWEAKS_PATH].endswith(b"\n")  # bytes + trailing newline
    got = await read_tweakspec(sbx)
    assert got == spec


@pytest.mark.asyncio
async def test_read_absent_is_none() -> None:
    assert await read_tweakspec(FakeSandboxInstance()) is None


@pytest.mark.asyncio
async def test_read_corrupt_raises() -> None:
    sbx = FakeSandboxInstance()
    sbx._fs[TWEAKS_PATH] = b"{ not json"
    with pytest.raises(ValueError):  # pydantic ValidationError is a ValueError
        await read_tweakspec(sbx)


@pytest.mark.asyncio
async def test_read_error_after_file_exists_propagates_not_none() -> None:
    # file_exists True but read_file fails → must PROPAGATE (not be mistaken for absent)
    class ReadFails(FakeSandboxInstance):
        async def read_file(self, path):  # type: ignore[override]
            raise RuntimeError("backend down")

    sbx = ReadFails()
    sbx._fs[TWEAKS_PATH] = b"{}"  # exists
    with pytest.raises(RuntimeError):
        await read_tweakspec(sbx)


@pytest.mark.asyncio
async def test_require_absent_raises_tweaks_error() -> None:
    with pytest.raises(TweaksError):
        await require_tweakspec(FakeSandboxInstance())


# --- merge_tweak_defaults -----------------------------------------------------
def test_merge_fills_defaults_and_coerces_present_values() -> None:
    spec = _spec()
    merged = merge_tweak_defaults(spec, {"lead.phone": "true", "hero.count": "4"})
    assert merged == {
        "hero.count": 4,
        "lead.phone": True,
    }  # coerced + sorted; brand.accent has no default


def test_merge_present_value_not_overwritten_by_default() -> None:
    spec = _spec()
    merged = merge_tweak_defaults(spec, {"lead.phone": True})  # present True, default is False
    assert merged["lead.phone"] is True


def test_merge_rejects_unknown_key() -> None:
    with pytest.raises(TweaksError):
        merge_tweak_defaults(_spec(), {"nope.key": 1})


def test_merge_invalid_present_value_raises_tweaks_error() -> None:
    with pytest.raises(TweaksError):
        merge_tweak_defaults(_spec(), {"hero.count": "99"})  # out of range


def test_merge_present_none_is_validated_not_default_filled() -> None:
    # present None for a boolean → validated (rejected), NOT replaced by the default
    with pytest.raises(TweaksError):
        merge_tweak_defaults(_spec(), {"lead.phone": None})


def test_merge_deterministic_sorted_order() -> None:
    merged = merge_tweak_defaults(_spec(), {"hero.count": 3, "lead.phone": False})
    assert list(merged) == sorted(merged)  # byte-stable persistence
