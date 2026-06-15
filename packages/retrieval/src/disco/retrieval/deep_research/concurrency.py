"""Gather-leg concurrency policy — a RAM-aware OOM guard for EVERY tier.

The Deep Research run fans out one gather leg per sub-question. Each leg, while
running, holds its own fetch buffers + extracted markdown + per-leg passages +
its slice of the run vector store, and (on the bundled tier) its embed/rerank
ONNX batches. N simultaneous legs multiply the agent-server's peak RSS ~N× — an
exhaustive run (12 sub-questions) can OOM the box.

OOM protection is a CORRECTNESS guarantee, so this cap applies on **every tier**
(bundled, self-host, paid). It is RAM-derived: a large box yields a K high enough
to run every leg (effectively unbounded), while a constrained box is protected.
The cap is NOT a free-tier compensation — do not confuse it with the search
rate-limit workarounds, which ARE bundled-tier-only.

`in_process_encoders` does NOT gate the cap on/off; it only TUNES the per-leg RAM
estimate — a leg with in-process FastEmbed encoders carries a much larger working
set than one whose embed/rerank run on a remote box.

An explicit env override (`DISCO_DR_GATHER_CONCURRENCY`) wins on any tier: a
positive int forces K; `0`/`unbounded`/`none`/`off` lifts the cap entirely for a
power user who knows their box can take it.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..local_encoders import _mem_available_gb

# Per-leg RAM budget (GB) at peak. IN-PROCESS legs also hold a bge-m3 embed batch
# + a cross-encoder rerank batch over a round's fetched passages → the dominant
# term. REMOTE-encoder legs only hold the fetched markdown + extracted passages
# in this process (embed/rerank run off-box) → a much smaller envelope. Both are
# deliberately conservative so the derived K errs toward safety.
_PER_LEG_GB_IN_PROCESS = 1.5
_PER_LEG_GB_REMOTE = 0.4
# RAM we refuse to consume: shared ONNX models (bundled), the router/LLM client,
# the run vector store, and OS headroom must all fit in what's left.
_RESERVE_GB = 2.0
# Clamp the DERIVED value: never < 1 (a run must make progress); the high ceiling
# is only a sanity backstop against a pathological meminfo read — normal runs are
# bounded by the sub-question count, not this number.
_K_MIN = 1
_K_CEILING = 32
# When RAM is unmeasurable (non-Linux / unreadable /proc), we cannot prove
# headroom, so fall back to a moderate cap rather than fanning out unbounded.
_K_UNKNOWN_RAM = 4

_ENV_OVERRIDE = "DISCO_DR_GATHER_CONCURRENCY"


def gather_concurrency_for(
    *,
    in_process_encoders: bool,
    n_subquestions: int,
    env: Mapping[str, str],
) -> int | None:
    """Return the gather-leg concurrency cap, or `None` for unbounded.

    Applies on every tier (RAM-derived). `None` is returned only when there is
    nothing to bound (a single leg) or the operator explicitly lifts the cap.
    """
    # Explicit override wins on any tier (incl. lifting the cap with 0/unbounded).
    raw = env.get(_ENV_OVERRIDE)
    if raw is not None:
        raw = raw.strip().lower()
        if raw in ("0", "none", "unbounded", "off"):
            return None
        try:
            forced = int(raw)
        except ValueError:
            forced = 0
        if forced > 0:
            return min(forced, max(1, n_subquestions))

    if n_subquestions <= 1:
        # One leg can't contend with itself; let it run.
        return None

    per_leg = _PER_LEG_GB_IN_PROCESS if in_process_encoders else _PER_LEG_GB_REMOTE

    avail = _mem_available_gb()
    if avail == float("inf"):
        return min(_K_UNKNOWN_RAM, n_subquestions)

    derivable = int((avail - _RESERVE_GB) // per_leg)
    return max(_K_MIN, min(derivable, _K_CEILING, n_subquestions))
