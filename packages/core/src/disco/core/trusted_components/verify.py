"""Trusted-components verification (spec §4, WO-TC3).

Three deterministic checks per installed, non-ejected component —
``component_integrity:<name>`` / ``component_deps:<name>`` /
``component_probe:<name>`` — computed PURELY from (lockfile, host registry,
workspace bytes, probe results). The sandbox/tool layer feeds the inputs and
persists the outputs; nothing here does IO except the probe callback the
caller injects.

Verdict semantics (spec §4 mapping):
- every check ``pass``            → the components are VERIFIED
- integrity mismatch / missing    → status ``ejected`` — auto-recorded in the
  returned lock (D4: honest relabel, converges in one pass, NEVER blocks)
- deps edge missing/unsatisfied   → status ``fail`` — BLOCKS finish
- probe failed                    → status ``fail`` — BLOCKS finish
- probe not runnable here         → status ``skipped`` — blocking is the
  caller's call (the finish-path verifier treats it as blocking; an
  ``unverifiable``-verdict path may release it honestly)

The canonical pin helper lives here so the authoring script and this check can
never drift (spec §1.5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from .lockfile import ComponentsLock, record_eject
from .registry import TrustedComponentRegistry, parse_requirement

# Where component installs live in a workspace (mirrored by the tools layer —
# single constant here so verify and install can never disagree).
INSTALL_PREFIX = "src/trusted"

PROBE_TIMEOUT_S = 60


def pin(data: bytes) -> str:
    """The canonical content pin: byte-exact sha256, no normalization."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def install_path(name: str, relpath: str) -> str:
    return f"{INSTALL_PREFIX}/{name}/{relpath}"


class ProbeCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ProbeVerdict(BaseModel):
    """The JSON contract a probe prints to stdout (spec §4.3). Probes are
    host-authored, registry-shipped, and re-materialized fresh on every run —
    a workspace copy is never trusted or reused."""

    passed: bool
    checks: list[ProbeCheck] = Field(default_factory=list)
    summary: str = ""


CheckStatus = Literal["pass", "fail", "ejected", "skipped"]

# run_probe(component_name) → ProbeVerdict, or None when probing is impossible
# in this context (no served app). Exceptions = probe infrastructure failures.
ProbeRunner = Callable[[str], Awaitable["ProbeVerdict | None"]]


@dataclass(frozen=True)
class ComponentCheck:
    name: str  # "component_integrity:auth-kit"
    status: CheckStatus
    evidence: str


@dataclass
class TrustedComponentsResult:
    checks: list[ComponentCheck] = field(default_factory=list)
    lock: ComponentsLock | None = None  # post-auto-eject state; caller persists
    newly_ejected: list[str] = field(default_factory=list)

    @property
    def failing(self) -> list[ComponentCheck]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def skipped_probes(self) -> list[ComponentCheck]:
        return [c for c in self.checks if c.status == "skipped"]

    def fingerprint_material(self) -> str:
        """Stable string folded into the verifier failure_fingerprint so the
        finish gate's STUCK detection keys on component state too."""
        return json.dumps([(c.name, c.status) for c in sorted(self.checks, key=lambda c: c.name)])


def _integrity_check(
    name: str,
    comp: Any,
    file_bytes: Mapping[str, bytes | None],
) -> tuple[list[str], bool]:
    """Return ``(diverged_files, passed)`` for the byte-exact integrity check."""

    diverged: list[str] = []
    for relpath, expected in sorted(comp.manifest.files.items()):
        data = file_bytes.get(install_path(name, relpath))
        if data is None or pin(data) != expected:
            diverged.append(relpath)
    return diverged, not diverged


def _deps_check(
    name: str,
    comp: Any,
    lock: ComponentsLock,
) -> tuple[bool, str, bool]:
    """Return ``(passed, evidence, has_missing)`` for the requires-graph check."""

    missing_edges: list[str] = []
    taints: list[str] = []
    for entry_str in comp.manifest.requires:
        req = parse_requirement(entry_str)
        dep = lock.components.get(req.name)
        if dep is None or not req.satisfied_by(req.name, dep.version):
            have = f"{req.name} {dep.version}" if dep else "nothing installed"
            missing_edges.append(f"{entry_str} (found: {have})")
        elif dep.ejected:
            taints.append(
                f"{req.name} is ejected — {name}'s guarantees now rest on custom code"
            )
    if missing_edges:
        return False, (
            f"{name} is missing required components: {'; '.join(missing_edges)}. "
            f"Install them with add_trusted_component."
        ), True
    if comp.manifest.requires:
        evidence = f"all {len(comp.manifest.requires)} dependency edge(s) satisfied."
        if taints:
            evidence += " CAVEAT: " + "; ".join(taints) + "."
        return True, evidence, False
    return True, "", False


def _probe_skip_check(name: str) -> ComponentCheck:
    return ComponentCheck(
        f"component_probe:{name}",
        "skipped",
        f"{name}'s seam probe needs the served app — start the preview "
        "and re-run verification.",
    )


def _probe_result_check(name: str, probe: ProbeVerdict) -> ComponentCheck:
    if probe.passed:
        return ComponentCheck(
            f"component_probe:{name}",
            "pass",
            probe.summary or f"{len(probe.checks)} probe check(s) passed.",
        )
    failing = [c for c in probe.checks if not c.passed]
    detail = "; ".join(f"{c.name}: {c.detail}" for c in failing[:3])
    return ComponentCheck(
        f"component_probe:{name}",
        "fail",
        (probe.summary + " — " if probe.summary else "") + (detail or "probe failed"),
    )


async def _run_probe_check(
    name: str,
    run_probe: ProbeRunner,
) -> ComponentCheck:
    """Run the probe and return the resulting check, handling infra failures."""

    try:
        probe = await run_probe(name)
    except Exception as exc:  # noqa: BLE001 — infra failure = honest FAIL, never a pass
        return ComponentCheck(
            f"component_probe:{name}",
            "fail",
            f"{name}'s probe could not run: {exc}",
        )
    if probe is None:
        return _probe_skip_check(name)
    return _probe_result_check(name, probe)


async def verify_trusted_components(
    lock: ComponentsLock,
    registry: TrustedComponentRegistry,
    file_bytes: Mapping[str, bytes | None],
    *,
    run_probe: ProbeRunner | None,
    now_iso: str,
) -> TrustedComponentsResult:
    """Run the three checks over every installed component.

    ``file_bytes`` must cover ``install_path(name, relpath)`` for every pinned
    file of every non-ejected component (value None = file missing). The
    caller assembles it from the sandbox; missing keys are treated as missing
    files (fail-closed: absence can never verify)."""
    result = TrustedComponentsResult(lock=lock)

    for name in sorted(lock.components):
        entry = lock.components[name]
        if entry.ejected:
            last = next((e for e in reversed(lock.ejects) if e.name == name), None)
            why = f"{last.reason} at {last.at}" if last else "recorded earlier"
            result.checks.append(
                ComponentCheck(
                    f"component_integrity:{name}",
                    "ejected",
                    f"{name} was ejected ({why}) — custom code the user owns; "
                    "no verified-component claims apply.",
                )
            )
            continue

        comp = registry.get(name, entry.version)
        if comp is None:
            record_eject(lock, name, "registry-version-missing", now_iso)
            result.newly_ejected.append(name)
            result.checks.append(
                ComponentCheck(
                    f"component_integrity:{name}",
                    "ejected",
                    f"{name} {entry.version} is no longer in this Disco's registry — "
                    "we can no longer vouch for it, so it is now custom code you own "
                    "(ejected: registry-version-missing).",
                )
            )
            continue

        # ---- 4.1 integrity: byte-exact pins from the HOST manifest (D1) ------
        diverged, integrity_ok = _integrity_check(name, comp, file_bytes)
        if not integrity_ok:
            record_eject(lock, name, "core-edit-detected", now_iso, diverged_files=diverged)
            result.newly_ejected.append(name)
            result.checks.append(
                ComponentCheck(
                    f"component_integrity:{name}",
                    "ejected",
                    f"{name} core was modified ({', '.join(diverged)}) — it is now "
                    "custom code you own; the verified badge is removed. This does "
                    "not block finishing.",
                )
            )
            continue
        result.checks.append(
            ComponentCheck(
                f"component_integrity:{name}",
                "pass",
                f"{len(comp.manifest.files)} core file(s) match the registry pins.",
            )
        )

        # ---- 4.2 requires-graph (D5/D6/D10) ----------------------------------
        deps_ok, deps_evidence, has_missing = _deps_check(name, comp, lock)
        if has_missing:
            result.checks.append(
                ComponentCheck(f"component_deps:{name}", "fail", deps_evidence)
            )
        elif comp.manifest.requires:
            result.checks.append(ComponentCheck(f"component_deps:{name}", "pass", deps_evidence))

        # ---- 4.3 probe (host-authored, injected runner) -----------------------
        if comp.manifest.probe is None:
            continue
        if run_probe is None:
            result.checks.append(_probe_skip_check(name))
            continue
        result.checks.append(await _run_probe_check(name, run_probe))

    return result


def parse_probe_stdout(stdout: str) -> ProbeVerdict:
    """Parse a probe subprocess's stdout: the LAST non-empty line must be the
    verdict JSON (spec §4.3). Anything else is an infra failure — raise."""
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise ValueError("probe produced no output")
    return ProbeVerdict.model_validate(json.loads(lines[-1]))
