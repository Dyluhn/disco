"""Trusted-component folding for the ``verify_web_app`` verdict.

WO-TC3 — append component_integrity/deps/probe checks to the W-45 verdict. No
lockfile → verdict unchanged (zero cost for ordinary builds). Blocking component
failures flip the verdict to FAIL with a teaching next_action; ejects are honest
relabels (persisted, never blocking). Failures extend failure_fingerprint so the
gate's STUCK detection keys on component state.

This is EVIDENCE folding, not a verdict authority: it never manufactures or
upgrades a typed ``HostVerificationResult``. The component checks are immutable
evidence bound for later host verification.

``TrustedComponentRegistry`` is read through the ``verify_app`` facade at call
time so a test that monkeypatches ``verify_app.TrustedComponentRegistry`` is in
effect for the call.
"""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from disco.core.trusted_components.lockfile import (
    LOCKFILE_RELPATH,
    LockfileCorrupt,
    dump_lock,
    parse_lock,
)
from disco.core.trusted_components.registry import TrustedComponentRegistry
from disco.core.trusted_components.verify import (
    PROBE_TIMEOUT_S,
    ProbeVerdict,
    install_path,
    parse_probe_stdout,
    verify_trusted_components,
)

from ...anatomy import ToolContext

RegistryFactory = Callable[[], TrustedComponentRegistry]


async def _fold_trusted_components(
    ctx: ToolContext,
    verdict: dict[str, Any],
    *,
    probes_allowed: bool,
    registry_factory: RegistryFactory,
) -> dict[str, Any]:
    """WO-TC3 — append component_integrity/deps/probe checks to the W-45
    verdict. No lockfile → verdict unchanged (zero cost for ordinary builds).
    Blocking component failures flip the verdict to FAIL with a teaching
    next_action; ejects are honest relabels (persisted, never blocking).
    Failures extend failure_fingerprint so the gate's STUCK detection keys
    on component state."""
    sandbox = ctx.sandbox
    assert sandbox is not None
    try:
        has_lock = await sandbox.file_exists(LOCKFILE_RELPATH)
    except Exception:  # noqa: BLE001 — backend can't answer existence: no
        # component could have been installed through the same protocol
        # either, so there is nothing to verify. Honest no-op.
        return verdict
    if not has_lock:
        return verdict
    checks_out: list[dict[str, str]] = []
    try:
        lock = parse_lock(await sandbox.read_file(LOCKFILE_RELPATH))
    except LockfileCorrupt as exc:
        # A corrupt lockfile can make NO claims — and a build carrying one
        # must not finish quietly, so it is a blocking failure with the fix
        # named (fail-closed, teach the exit).
        checks_out.append(
            {
                "name": "component_lockfile",
                "status": "fail",
                "evidence": (
                    f"{exc} — fix or delete {LOCKFILE_RELPATH} (deleting drops "
                    "all verified-component claims), then re-verify."
                ),
            }
        )
        return _merge_component_checks(verdict, checks_out, blocking=True)

    if not lock.components:
        return verdict

    try:
        return await _fold_checks(
            ctx,
            verdict,
            lock,
            checks_out,
            probes_allowed=probes_allowed,
            registry_factory=registry_factory,
        )
    except Exception as exc:  # noqa: BLE001 — a broken component verifier must
        # never silently PASS a build that carries a lockfile (fail-closed).
        checks_out.append(
            {
                "name": "component_verify_error",
                "status": "fail",
                "evidence": f"component verification crashed: {exc}",
            }
        )
        return _merge_component_checks(verdict, checks_out, blocking=True)


async def _fold_checks(
    ctx: ToolContext,
    verdict: dict[str, Any],
    lock: Any,
    checks_out: list[dict[str, str]],
    *,
    probes_allowed: bool,
    registry_factory: RegistryFactory,
) -> dict[str, Any]:
    registry = registry_factory()
    file_bytes = await _component_file_bytes(ctx, lock, registry)
    base_url = str(verdict.get("url") or "")
    run_probe = (
        _make_probe_runner(ctx, base_url, lock, registry)
        if probes_allowed and base_url
        else None
    )
    result = await verify_trusted_components(
        lock,
        registry,
        file_bytes,
        run_probe=run_probe,
        now_iso=datetime.now(UTC).isoformat(),
    )
    await _persist_component_state(ctx, lock, result)
    checks_out.extend(
        {"name": c.name, "status": c.status, "evidence": c.evidence}
        for c in result.checks
    )
    blocking = bool(result.failing) or (
        probes_allowed and bool(result.skipped_probes)
    )
    if checks_out:
        material = (
            verdict.get("failure_fingerprint") or ""
        ) + result.fingerprint_material()
        verdict["failure_fingerprint"] = hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()[:16]
    return _merge_component_checks(verdict, checks_out, blocking=blocking)


async def _component_file_bytes(
    ctx: ToolContext, lock: Any, registry: TrustedComponentRegistry
) -> dict[str, bytes | None]:
    sandbox = ctx.sandbox
    assert sandbox is not None
    file_bytes: dict[str, bytes | None] = {}
    for name, entry in lock.components.items():
        if entry.ejected:
            continue
        component = registry.get(name, entry.version)
        if component is None:
            continue
        for relpath in component.manifest.files:
            target = install_path(name, relpath)
            file_bytes[target] = (
                await sandbox.read_file(target)
                if await sandbox.file_exists(target)
                else None
            )
    return file_bytes


async def _persist_component_state(ctx: ToolContext, lock: Any, result: Any) -> None:
    if result.newly_ejected and result.lock is not None:
        from disco.tools.builtin.files import _atomic_write

        sandbox = ctx.sandbox
        assert sandbox is not None
        await _atomic_write(sandbox, LOCKFILE_RELPATH, dump_lock(result.lock))
    banner_lock = result.lock if result.lock is not None else lock
    if banner_lock is not None:
        for name, entry in banner_lock.components.items():
            if entry.ejected:
                await _append_eject_banner(ctx, banner_lock, name)


def _merge_component_checks(
    verdict: dict[str, Any], checks: list[dict[str, str]], *, blocking: bool
) -> dict[str, Any]:
    if not checks:
        return verdict
    verdict["component_checks"] = checks
    ejected = [c for c in checks if c["status"] == "ejected"]
    bad = [c for c in checks if c["status"] in ("fail", "skipped")]
    if blocking and bad:
        first = next((c for c in checks if c["status"] == "fail"), bad[0])
        verdict["passed"] = False
        verdict["verdict"] = "fail"
        verdict["summary"] = f"trusted components: {first['evidence']}"
        verdict["next_action"] = first["evidence"]
    elif ejected:
        note = "; ".join(c["evidence"] for c in ejected[:2])
        verdict["summary"] = f"{verdict.get('summary', '')} [components: {note}]".strip()
    return verdict


def _make_probe_runner(
    ctx: ToolContext,
    base_url: str,
    lock: Any,
    registry: Any,
):
    """Probes execute INSIDE the sandbox (the preview URL is sandbox-local),
    re-materialized fresh from the HOST registry on every run — a workspace
    copy is never trusted, so 'fixing the check' is impossible (D3). Probes
    are python3-stdlib-only by authoring rule."""

    async def run(name: str) -> ProbeVerdict | None:
        sandbox = ctx.sandbox
        assert sandbox is not None
        entry = lock.components[name]
        comp = registry.get(name, entry.version)
        if comp is None:
            return None
        src = comp.probe_source()
        if src is None:
            return None
        probe_path = f".disco/tc-probe/{name}/probe.py"
        await sandbox.write_file(probe_path, src)
        cmd = (
            f"python3 {shlex.quote(probe_path)} "
            f"--base-url {shlex.quote(base_url)} --workspace ."
        )
        res = await sandbox.exec_shell(cmd, timeout_s=PROBE_TIMEOUT_S)
        if res.exit_code != 0:
            tail = (res.stderr or res.stdout or "")[-300:]
            raise RuntimeError(f"probe exited {res.exit_code}: {tail}")
        return parse_probe_stdout(res.stdout)

    return run


async def _append_eject_banner(ctx: ToolContext, lock: Any, name: str) -> None:
    sandbox = ctx.sandbox
    assert sandbox is not None
    record = next((e for e in reversed(lock.ejects) if e.name == name), None)
    guide_path = install_path(name, "GUIDE.md")
    if record is None or not await sandbox.file_exists(guide_path):
        return
    guide = await sandbox.read_file(guide_path)
    if b"**Ejected" in guide:
        return  # idempotent — the banner is already present
    banner = (
        f"\n\n---\n\n> ⚠ **Ejected {record.at}** ({record.reason}) — this copy "
        f"diverged from the registry and is now custom code you own. Upgrades and "
        f"the verified badge no longer apply."
        + (f" Diverged: {', '.join(record.diverged_files)}." if record.diverged_files else "")
        + "\n"
    ).encode("utf-8")
    await sandbox.write_file(guide_path, guide + banner)
