"""Shared closeout acceptance fixtures — randomized, seed-derived name generation.

FROZEN acceptance path (WO-C0, plan §1.1). Every closeout test that needs a
project id / file / directory name draws it from a SEEDED RNG so the names VARY
from a recorded seed at test time (plan §4 criterion 8): hard-coding a fixture
path, project id, or exact fixture bytes cannot satisfy the matrix. The seed
source is the ``CLOSEOUT_SEED`` env var (a fixed default), so reruns are
byte-stable — never ``random.random`` / clock-based nondeterminism. The chosen
seed is recorded into the JUnit XML through ``record_property`` so the evidence
manifest produced by ``development/scripts/verify_export_track1_closeout.py`` can read it back.

Detector inputs never receive these names: a name flows only into a conversation
id / sidecar path / spec ``name`` field, never into the file CONTENTS the release
detector reads — so a detection cannot "recognize" a fixture by its name.

HONEST DEVIATION from the WO-C0 skeleton note ("each dir with __init__.py"):
this repo's ENTIRE test tree is rootless (there is no ``development/tests/__init__.py``
anywhere) and runs under pytest's default ``prepend`` import mode. Making three
identically-named ``export_track1_closeout`` dirs importable packages collides on
the shared package / ``conftest`` module name and BREAKS collection of the whole
non-live lane (verified empirically). Rootless dirs collect cleanly and keep each
conftest independent, so the acceptance harness stays runnable — which is the
entire point of a non-bypass gate. This file is therefore intentionally mirrored,
byte-for-byte, in each closeout dir rather than shared through an importable
package.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable

import pytest

CLOSEOUT_SEED_ENV = "CLOSEOUT_SEED"
DEFAULT_CLOSEOUT_SEED = "export-track1-closeout-v1"
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def closeout_seed(record_property: Callable[[str, object], None]) -> str:
    """The active randomized seed for this test (``CLOSEOUT_SEED`` or the fixed
    default). Recorded into the JUnit ``testcase`` so evidence can prove which seed
    ran — a reviewer can rerun with the SAME seed and get byte-identical names."""
    seed = os.environ.get(CLOSEOUT_SEED_ENV, DEFAULT_CLOSEOUT_SEED)
    record_property("closeout_seed", seed)
    return seed


@pytest.fixture
def closeout_rng(closeout_seed: str) -> random.Random:
    """A deterministic RNG seeded from ``closeout_seed``. Same seed ⇒ same names
    ⇒ reproducible reruns; a different seed varies every generated name."""
    return random.Random(closeout_seed)


@pytest.fixture
def closeout_name(closeout_rng: random.Random) -> Callable[[str], str]:
    """Factory ``prefix -> "<prefix>-<8 seeded chars>"``. Use it for conversation
    ids, project titles, and workspace directory segments so no closeout test can
    pass by hard-coding a name."""

    def _make(prefix: str) -> str:
        token = "".join(closeout_rng.choice(_ALPHABET) for _ in range(8))
        return f"{prefix}-{token}"

    return _make
