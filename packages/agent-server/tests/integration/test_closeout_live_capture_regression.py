"""Owner-adjudicated A (2026-07-17): the ``record=False`` capture exemption is
load-bearing AND strictly narrow.

The frozen live lane's §12.9 sweep asserts the runtime secret is absent from
``ComposeBundle.captured_output``. The deliberate ``exec_env`` secret-presence
probe prints the secret BY DESIGN and is therefore excluded from recording; every
ordinary Compose lifecycle surface must remain recorded and swept. These
regressions prove both directions against the REAL ``docker compose`` binary (no
mocks — the anti-bypass reading forbids them), so they carry the ``integration``
marker and run on the Docker host alongside the frozen lane. They are NOT part of
the frozen seven-case §12 selection (no ``export_track1_closeout`` marker).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._closeout_live_support import (
    SECRET_ENV_SENTINEL,
    ComposeBundle,
    assert_sentinel_absent,
    require_live_runtime,
)

pytestmark = [pytest.mark.integration]


def _sentinel_bundle(tmp_path: Path) -> ComposeBundle:
    """A minimal synthetic bundle whose ``docker compose config`` output — an
    ORDINARY, always-recorded lifecycle surface — contains the sentinel (as a
    literal default value in the compose file). No container is ever started."""
    compose_yaml = tmp_path / "compose.yaml"
    compose_yaml.write_text(
        "services:\n"
        "  web:\n"
        '    image: "busybox:1.37"\n'
        "    environment:\n"
        f'      LEAKED: "{SECRET_ENV_SENTINEL}"\n'
    )
    return ComposeBundle(project="capture-regression", bundle_dir=tmp_path)


def test_ordinary_recorded_compose_output_with_sentinel_still_fails_the_sweep(
    tmp_path: Path,
) -> None:
    """A sentinel that reaches ANY ordinary (recorded) Compose surface must still
    fail the §12.9 sweep — the exemption cannot have widened the blind spot."""
    require_live_runtime()
    bundle = _sentinel_bundle(tmp_path)
    proc = bundle.compose("config")  # ordinary lifecycle call: recorded by default
    assert proc.returncode == 0, proc.stderr
    assert SECRET_ENV_SENTINEL in proc.stdout  # the surface REALLY carries it
    with pytest.raises(AssertionError):
        assert_sentinel_absent(
            SECRET_ENV_SENTINEL,
            [("captured compose output", "".join(bundle.captured_output).encode("utf-8"))],
        )


def test_record_false_is_excluded_and_default_recording_is_unchanged(
    tmp_path: Path,
) -> None:
    """The exemption is explicit and narrow: an unrecorded call leaves
    ``captured_output`` untouched, and the very same invocation WITH the default
    records both streams."""
    require_live_runtime()
    bundle = _sentinel_bundle(tmp_path)
    unrecorded = bundle.compose("config", record=False)
    assert unrecorded.returncode == 0, unrecorded.stderr
    assert bundle.captured_output == []
    recorded = bundle.compose("config")
    assert recorded.returncode == 0, recorded.stderr
    assert len(bundle.captured_output) == 2
    assert SECRET_ENV_SENTINEL in "".join(bundle.captured_output)
