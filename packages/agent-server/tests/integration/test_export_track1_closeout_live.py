"""WO-C0 live-lane skeleton — Docker/Compose availability FAILS (never skips).

FROZEN acceptance path (plan §1.1). Plan §4 criterion 5 and §1.3 are explicit: the
live test must FAIL — not skip — when Docker or Docker Compose is unavailable.
"Claiming the live lane is deferred" is an automatic failure; a skip/`skipif`/
environment-controlled early return is forbidden. So this test asserts (rather than
skips on) the presence of a real Docker Engine and Docker Compose v2.

On the CI/dev host used to AUTHOR this harness there is no local Docker, so this
test FAILS here — that is the CORRECT behavior for the skeleton: it proves the lane
cannot be silently green without a real engine. The intended runner is the
self-hosted Docker host (the sandbox host, e.g. the `disco-live` /
`export-track1-closeout` self-hosted runner referenced by
`.github/workflows/export-track1-closeout.yml`), where these assertions pass and
the later C8 tranche fills in the real clean-room bundle lifecycle
(build → up → HTTP → restart → state → cleanup) around this same marker pair.

Selected ONLY by `-m "export_track1_closeout and integration"`; it is excluded from
the default/non-live lanes by the `integration` marker.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

pytestmark = [pytest.mark.export_track1_closeout, pytest.mark.integration]


def _compose_version_ok() -> tuple[bool, str]:
    """True iff `docker compose version` runs and reports Compose v2. Any launch
    failure returns (False, reason) — the caller ASSERTS on it, so an absent engine
    fails the lane rather than skipping it."""
    docker = shutil.which("docker")
    if docker is None:
        return False, "docker executable not found on PATH"
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            [docker, "compose", "version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"`docker compose version` did not launch: {exc}"
    if proc.returncode != 0:
        return False, f"`docker compose version` exited {proc.returncode}: {proc.stderr.strip()}"
    return True, proc.stdout.strip()


def test_docker_and_compose_are_available_for_the_live_lane() -> None:
    """FAIL (assert), never skip, when the live runtime is absent (plan §4.5)."""
    docker = shutil.which("docker")
    assert docker is not None, (
        "Docker Engine is required for the export-track1-closeout live lane and was "
        "not found on PATH. This lane must FAIL — not skip — without a real engine "
        "(plan §1.3/§4.5). Run it on the self-hosted Docker host, never a hosted "
        "runner without Docker."
    )
    ok, detail = _compose_version_ok()
    assert ok, (
        "Docker Compose v2 is required for the export-track1-closeout live lane: "
        f"{detail}. The lane fails closed rather than skipping."
    )
