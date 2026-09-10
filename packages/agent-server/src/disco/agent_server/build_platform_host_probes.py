"""The host-side adapter that answers Core's capability probes.

This module sits **below the host port**.  It is the one place allowed to know
what an operating system is: Core declares what must be checked, and this
adapter knows how to look at a real machine.  Keeping the knowledge here is what
lets `disco.core.build_platform` stay free of platform conditionals.

Two properties matter more than the individual checks:

* **Detection selects a definition; it never grants a capability.**  Even if
  :func:`detect_profile_definition` picked the wrong profile, nothing would be
  advertised that a probe did not actually find, because a claim still has to be
  bound to a ``PRESENT`` observation stamped with that profile's identity.
  Misdetection can therefore produce an empty profile, never a false one.
* **A probe never guesses.**  Anything it cannot establish is reported
  ``ABSENT`` or ``NOT_RUN`` with a reason, and Core's grading rules turn that
  into ``unsupported``/``experimental``.  There is no code path from "I did not
  check" to "supported".

The egress probe is off by default: a capability check must not be the thing
that opens an unexpected outbound connection.  Left off it reports ``NOT_RUN``
with that reason, which honestly withholds the ``deploy`` phase.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from disco.core.build_platform.host_capabilities import (
    HostCapabilityProbe,
    ProbeObservation,
    ProbeOutcome,
    pending_observations,
    run_probes,
)
from disco.core.build_platform.host_profiles import (
    LINUX_HOST_V2,
    MACOS_HOST_V1,
    WSL2_HOST_V1,
    HostProfile,
    HostProfileDefinition,
)

#: Executables that would constitute a usable code-signing identity.  This is a
#: search, not a platform switch: whichever is present is the evidence.
_SIGNING_TOOLS: tuple[str, ...] = ("codesign", "signtool", "cosign", "osslsigncode")

#: Container runtimes that satisfy the optional OCI capability.
_CONTAINER_RUNTIMES: tuple[str, ...] = ("podman", "docker", "nerdctl")

#: Environment markers for an attached interactive display server.
_DISPLAY_MARKERS: tuple[str, ...] = ("DISPLAY", "WAYLAND_DISPLAY")

_WSL_MARKERS: tuple[str, ...] = ("microsoft", "wsl")


def detect_profile_definition() -> HostProfileDefinition | None:
    """Pick which shipped definition describes this machine, or ``None``.

    Returning ``None`` for an unrecognised host is deliberate.  Falling back to
    a "closest" profile would be the nominal-OS-label defect in another shape:
    a profile chosen by resemblance, then evidenced by probes that were written
    for a different machine.
    """
    system = platform.system()
    if system == "Linux":
        return WSL2_HOST_V1 if _is_wsl() else LINUX_HOST_V2
    if system == "Darwin":
        return MACOS_HOST_V1
    return None


def _is_wsl() -> bool:
    release = platform.release().lower()
    if any(marker in release for marker in _WSL_MARKERS):
        return True
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(marker in version for marker in _WSL_MARKERS)


class HostCapabilityProbeRunner:
    """Executes Core's declared probes against this machine.

    Implements :class:`~disco.core.build_platform.host_capabilities.HostProbeRunner`.
    """

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        allow_network_probe: bool = False,
        egress_endpoint: tuple[str, int] = ("127.0.0.1", 9),
    ) -> None:
        self._workspace_root = workspace_root
        self._allow_network_probe = allow_network_probe
        self._egress_endpoint = egress_endpoint

    def run(self, probe: HostCapabilityProbe, *, profile_id: str) -> ProbeObservation:
        handler = {
            "host.workspace_read": self._workspace_read,
            "host.workspace_write": self._workspace_write,
            "host.process_execute": self._process_execute,
            "host.network_egress": self._network_egress,
            "host.display_interactive": self._display_interactive,
            "host.toolchain_node": self._toolchain_node,
            "host.signing_identity": self._signing_identity,
            "host.container_runtime": self._container_runtime,
            "host.gvisor_sandbox": self._gvisor_sandbox,
        }.get(probe.probe_id)
        if handler is None:
            return _observation(
                probe, profile_id, ProbeOutcome.NOT_RUN, "no adapter implements this probe"
            )
        return handler(probe, profile_id)

    # -- filesystem ---------------------------------------------------------

    def _root(self) -> Path:
        return self._workspace_root or Path(tempfile.gettempdir())

    def _workspace_read(self, probe: HostCapabilityProbe, profile_id: str) -> ProbeObservation:
        root = self._root()
        if root.is_dir() and os.access(root, os.R_OK):
            return _observation(probe, profile_id, ProbeOutcome.PRESENT, f"readable at {root}")
        return _observation(probe, profile_id, ProbeOutcome.ABSENT, f"not readable at {root}")

    def _workspace_write(self, probe: HostCapabilityProbe, profile_id: str) -> ProbeObservation:
        root = self._root()
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix=".disco-probe-", delete=True) as fh:
                fh.write(b"probe")
                fh.flush()
        except OSError as exc:
            return _observation(
                probe,
                profile_id,
                ProbeOutcome.ABSENT,
                f"write refused at {root}: {type(exc).__name__}",
            )
        return _observation(
            probe, profile_id, ProbeOutcome.PRESENT, f"created and removed a file in {root}"
        )

    # -- process / network --------------------------------------------------

    def _process_execute(self, probe: HostCapabilityProbe, profile_id: str) -> ProbeObservation:
        try:
            # Fixed argv, no shell, no caller-supplied input: the probe answers
            # "can this host spawn a child at all", nothing more.
            completed = subprocess.run(
                [sys.executable, "-c", "pass"],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _observation(
                probe, profile_id, ProbeOutcome.ABSENT, f"spawn failed: {type(exc).__name__}"
            )
        if completed.returncode != 0:
            return _observation(
                probe, profile_id, ProbeOutcome.ABSENT, f"child exited {completed.returncode}"
            )
        return _observation(probe, profile_id, ProbeOutcome.PRESENT, "child process exited 0")

    def _network_egress(self, probe: HostCapabilityProbe, profile_id: str) -> ProbeObservation:
        if not self._allow_network_probe:
            return _observation(
                probe,
                profile_id,
                ProbeOutcome.NOT_RUN,
                "egress probe disabled: a capability check must not open an unexpected connection",
            )
        host, port = self._egress_endpoint
        try:
            with socket.create_connection((host, port), timeout=5):
                pass
        except OSError as exc:
            return _observation(
                probe, profile_id, ProbeOutcome.ABSENT, f"egress refused: {type(exc).__name__}"
            )
        return _observation(probe, profile_id, ProbeOutcome.PRESENT, f"connected to {host}:{port}")

    # -- environment / tooling ---------------------------------------------

    def _display_interactive(self, probe: HostCapabilityProbe, profile_id: str) -> ProbeObservation:
        present = [name for name in _DISPLAY_MARKERS if os.environ.get(name)]
        if present:
            return _observation(
                probe,
                profile_id,
                ProbeOutcome.PRESENT,
                "display server marker(s): " + ", ".join(present),
            )
        return _observation(
            probe, profile_id, ProbeOutcome.ABSENT, "no display server marker in the environment"
        )

    def _toolchain_node(self, probe: HostCapabilityProbe, profile_id: str) -> ProbeObservation:
        return _which_observation(probe, profile_id, ("node",), "Node.js toolchain")

    def _signing_identity(self, probe: HostCapabilityProbe, profile_id: str) -> ProbeObservation:
        return _which_observation(probe, profile_id, _SIGNING_TOOLS, "code-signing tool")

    def _container_runtime(self, probe: HostCapabilityProbe, profile_id: str) -> ProbeObservation:
        return _which_observation(probe, profile_id, _CONTAINER_RUNTIMES, "OCI container runtime")

    def _gvisor_sandbox(self, probe: HostCapabilityProbe, profile_id: str) -> ProbeObservation:
        return _which_observation(probe, profile_id, ("runsc",), "gVisor runsc runtime")


def _which_observation(
    probe: HostCapabilityProbe,
    profile_id: str,
    candidates: tuple[str, ...],
    label: str,
) -> ProbeObservation:
    for name in candidates:
        found = shutil.which(name)
        if found:
            return _observation(probe, profile_id, ProbeOutcome.PRESENT, f"{label} at {found}")
    return _observation(
        probe,
        profile_id,
        ProbeOutcome.ABSENT,
        f"no {label} on PATH (looked for: {', '.join(candidates)})",
    )


def _observation(
    probe: HostCapabilityProbe,
    profile_id: str,
    outcome: ProbeOutcome,
    detail: str,
) -> ProbeObservation:
    return ProbeObservation(
        probe_id=probe.probe_id,
        outcome=outcome,
        observed_on=profile_id,
        detail=detail[:400],
    )


class HostProbeMismatch(RuntimeError):
    """A profile was asked to be evidenced by a machine it does not describe."""


def probe_host_profile(
    definition: HostProfileDefinition,
    *,
    runner: HostCapabilityProbeRunner | None = None,
) -> HostProfile:
    """Run every declared probe for *definition* and bind the result.

    The definition is supplied explicitly, but it is checked against what this
    machine actually is before a single probe runs.  Without that check the
    adapter would happily probe *this* host and stamp the findings with some
    other platform's identity — genuine observations wearing a false label,
    which is the nominal-OS defect in its most convincing form.  Core cannot
    catch it, because by the time the observations exist they are internally
    consistent.  So it is refused here, where the machine is actually known.
    """
    detected = detect_profile_definition()
    if detected is None:
        raise HostProbeMismatch(
            f"this host matches no shipped profile, so no evidence may be gathered "
            f"for {definition.id.canonical}; model it as unprobed instead"
        )
    if (detected.id.namespace, detected.id.name) != (
        definition.id.namespace,
        definition.id.name,
    ):
        raise HostProbeMismatch(
            f"refusing to evidence {definition.id.canonical} on a "
            f"{detected.id.name} host: observations taken here describe "
            f"{detected.id.canonical} and are not evidence for another platform"
        )
    active = runner or HostCapabilityProbeRunner()
    observations = run_probes(active, definition.probes, profile_id=definition.id.canonical)
    return definition.bind(observations)


def unprobed_host_profile(definition: HostProfileDefinition, *, reason: str) -> HostProfile:
    """Model a platform this host is not, with no capability claimed.

    This is the honest counterpart to :func:`probe_host_profile`: a real,
    versioned, selectable profile whose every result is explicitly pending
    evidence rather than a profile quietly omitted or quietly assumed.
    """
    return definition.bind(
        pending_observations(definition.probes, profile_id=definition.id.canonical, detail=reason),
        experimental=definition.probed_capabilities,
    )
