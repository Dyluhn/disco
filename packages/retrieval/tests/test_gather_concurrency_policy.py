"""Gather-leg concurrency policy — the RAM-aware OOM guard for EVERY tier.

These lock the load-bearing contract that the OOM peak-memory guard applies on
ALL tiers (it is correctness, not a free-tier compensation), that it is
RAM-derived (big box → effectively unbounded, small box → protected), that the
`in_process_encoders` flag only TUNES the per-leg estimate (heavier in-process),
and that the operator override behaves on every tier.
"""

from __future__ import annotations

import disco.retrieval.deep_research.concurrency as conc
from disco.retrieval.deep_research.concurrency import gather_concurrency_for


def test_remote_encoders_still_capped_but_with_lighter_per_leg(monkeypatch) -> None:
    """Remote (paid/self-host) tier is STILL capped — OOM protection applies
    everywhere — but the lighter per-leg estimate (0.4 GB vs 1.5) means the same
    RAM yields a HIGHER K than the in-process tier would."""
    monkeypatch.setattr(conc, "_mem_available_gb", lambda: 10.0)
    k_remote = gather_concurrency_for(
        in_process_encoders=False, n_subquestions=12, env={}
    )  # (10-4)//0.5 = 12 → clamped to n=12
    k_inproc = gather_concurrency_for(
        in_process_encoders=True, n_subquestions=12, env={}
    )  # (10-4)//1.5 = 4
    assert k_remote is not None and k_inproc is not None
    assert k_remote > k_inproc
    assert k_remote == 12
    assert k_inproc == 4


def test_single_subquestion_is_never_capped() -> None:
    assert gather_concurrency_for(in_process_encoders=True, n_subquestions=1, env={}) is None


def test_bundled_tier_caps_when_ram_constrained(monkeypatch) -> None:
    """Bundled in-process encoders on a small box → a real, clamped cap < n."""
    monkeypatch.setattr(conc, "_mem_available_gb", lambda: 7.0)  # (7-4)//1.5 = 2
    k = gather_concurrency_for(in_process_encoders=True, n_subquestions=12, env={})
    assert k == 2


def test_bundled_tier_cap_never_below_one(monkeypatch) -> None:
    # (2.5-4) is negative (reserve exceeds avail) → clamps up to the K_MIN floor.
    monkeypatch.setattr(conc, "_mem_available_gb", lambda: 2.5)
    k = gather_concurrency_for(in_process_encoders=True, n_subquestions=12, env={})
    assert k == 1


def test_big_box_runs_every_leg_effectively_unbounded(monkeypatch) -> None:
    """A large box derives a K so high it is bounded by the sub-question count, not
    the cap — i.e. every leg runs. That is how a capable box stays unthrottled
    while the same code protects a small one."""
    monkeypatch.setattr(conc, "_mem_available_gb", lambda: 256.0)
    k = gather_concurrency_for(in_process_encoders=True, n_subquestions=12, env={})
    assert k == 12  # clamped to n, not to a low ceiling


def test_unreadable_meminfo_falls_back_to_moderate_cap(monkeypatch) -> None:
    """inf (non-Linux / unreadable /proc) → a moderate cap, NOT unbounded, since
    we cannot prove headroom."""
    monkeypatch.setattr(conc, "_mem_available_gb", lambda: float("inf"))
    k = gather_concurrency_for(in_process_encoders=True, n_subquestions=12, env={})
    assert k == conc._K_UNKNOWN_RAM


def test_env_override_forces_value_on_bundled_tier(monkeypatch) -> None:
    monkeypatch.setattr(conc, "_mem_available_gb", lambda: 6.0)
    k = gather_concurrency_for(
        in_process_encoders=True,
        n_subquestions=12,
        env={"DISCO_DR_GATHER_CONCURRENCY": "5"},
    )
    assert k == 5


def test_env_override_can_lift_cap_even_on_bundled_tier() -> None:
    """An explicit `0`/`unbounded` opts a power user out entirely, any tier."""
    for val in ("0", "unbounded", "none", "off"):
        assert (
            gather_concurrency_for(
                in_process_encoders=True,
                n_subquestions=12,
                env={"DISCO_DR_GATHER_CONCURRENCY": val},
            )
            is None
        )


def test_env_override_clamps_to_subquestion_count() -> None:
    k = gather_concurrency_for(
        in_process_encoders=True,
        n_subquestions=3,
        env={"DISCO_DR_GATHER_CONCURRENCY": "99"},
    )
    assert k == 3
