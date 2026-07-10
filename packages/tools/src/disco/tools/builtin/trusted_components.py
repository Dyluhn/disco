"""Trusted-component tools (spec §3, WO-TC2): `add_trusted_component` +
`eject_trusted_component`.

Free-form Build scope ONLY (D8): the names live in AGENT_TOOLS and nowhere
else — strict AppKit, artifact, and research scopes never see them. The
catalog (name + when-to-use per component) is rendered INTO the add tool's
schema (catalog-in-schema doctrine), and success returns the component's
GUIDE.md verbatim — guidance exactly at decision time.

Install layout (spec §1.4): files land under ``src/trusted/<name>/``; the
lockfile is ``.disco/components.lock``. The probe is never installed (D3) and
the manifest is never copied (D1).
"""

from __future__ import annotations

from datetime import UTC, datetime

from disco.core import SecurityRisk
from disco.core.trusted_components.lockfile import (
    LOCKFILE_RELPATH,
    ComponentsLock,
    LockfileCorrupt,
    dump_lock,
    parse_lock,
    record_eject,
    record_install,
)
from disco.core.trusted_components.registry import (
    TrustedComponentRegistry,
    parse_requirement,
)
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

INSTALL_PREFIX = "src/trusted"


def _install_path(name: str, relpath: str) -> str:
    return f"{INSTALL_PREFIX}/{name}/{relpath}"


def _catalog() -> str:
    lines = TrustedComponentRegistry.default().catalog_lines()
    return lines if lines else "(the component registry is empty on this build)"


async def _load_lock(ctx: ToolContext) -> ComponentsLock:
    """Read the workspace lockfile; raises LockfileCorrupt on garbage."""
    assert ctx.sandbox is not None
    if not await ctx.sandbox.file_exists(LOCKFILE_RELPATH):
        return parse_lock(None)
    return parse_lock(await ctx.sandbox.read_file(LOCKFILE_RELPATH))


def _corrupt_refusal(exc: LockfileCorrupt) -> ToolOutcome:
    return ToolOutcome(
        success=False,
        error="lockfile_corrupt",
        content=(
            f"{exc} — refusing to touch it (overwriting would lose eject history). "
            f"Fix or delete {LOCKFILE_RELPATH} first; deleting it drops every "
            "verified-component claim, which is safe but means reinstalling."
        ),
    )


class AddTrustedComponentArgs(BaseModel):
    name: str = Field(
        description=(
            "Which trusted component to install. Catalog (latest versions):\n"
            + _catalog()
        )
    )
    version: str | None = Field(
        default=None,
        description="Exact X.Y.Z version; omit for the latest. Omitting is almost always right.",
    )


class AddTrustedComponentTool:
    definition = ToolDef(
        name="add_trusted_component",
        description=(
            "Install a VERIFIED, host-owned component (auth, database, RBAC…) into "
            "src/trusted/<name>/ instead of hand-rolling security-critical code. The "
            "core files are integrity-checked at verify time and must not be edited "
            "(edit the config/ surface instead; editing core = honest eject, the "
            "verified badge is removed). Catalog:\n" + _catalog() + "\n"
            "Dependencies are enforced: install a component's requirements first. "
            "Success returns the component's GUIDE.md — read it before wiring."
        ),
        args_model=AddTrustedComponentArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: AddTrustedComponentArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        registry = TrustedComponentRegistry.default()
        comp = registry.get(args.name, args.version)
        if comp is None:
            versions = registry.versions(args.name)
            detail = (
                f"no version {args.version!r} of {args.name!r} — available: {', '.join(versions)}"
                if versions
                else f"unknown component {args.name!r}"
            )
            return ToolOutcome(
                success=False,
                error="unknown_component",
                content=f"{detail}. Catalog:\n{_catalog()}",
            )

        try:
            lock = await _load_lock(ctx)
        except LockfileCorrupt as exc:
            return _corrupt_refusal(exc)

        # Requires preflight (spec §3.1.2) — fail closed, but the refusal names
        # the exact missing edge AND the command that fixes it. Ejected
        # dependencies still satisfy the edge structurally (D6); verify is what
        # taints the labels.
        for entry in comp.manifest.requires:
            req = parse_requirement(entry)
            installed = lock.components.get(req.name)
            if installed is None or not req.satisfied_by(req.name, installed.version):
                have = f"{req.name} {installed.version}" if installed else "nothing"
                return ToolOutcome(
                    success=False,
                    error="missing_dependency",
                    content=(
                        f"{comp.manifest.name} requires {entry}; installed: {have}. "
                        f"Install it first: add_trusted_component(name='{req.name}')."
                    ),
                )

        # Collision check (never clobber): byte-identical files are fine
        # (idempotent re-install), anything else refuses with the list.
        tree = comp.install_tree()
        collisions: list[str] = []
        to_write: dict[str, bytes] = {}
        for relpath, data in tree.items():
            target = _install_path(comp.manifest.name, relpath)
            if await ctx.sandbox.file_exists(target):
                if await ctx.sandbox.read_file(target) == data:
                    continue
                collisions.append(target)
            else:
                to_write[target] = data
        if collisions:
            return ToolOutcome(
                success=False,
                error="collision",
                content=(
                    "these files already exist with DIFFERENT content — refusing to "
                    "clobber your work: " + ", ".join(sorted(collisions)) + ". Move or "
                    "revert them (fresh component copies) and retry."
                ),
            )

        for target, data in to_write.items():
            await ctx.sandbox.write_file(target, data)
        record_install(
            lock, comp.manifest.name, comp.manifest.version, datetime.now(UTC).isoformat()
        )
        await ctx.sandbox.write_file(LOCKFILE_RELPATH, dump_lock(lock))

        m = comp.manifest
        mounts_bits = []
        if m.mounts.routes_prefix:
            mounts_bits.append(f"routes live under {m.mounts.routes_prefix}")
        if m.mounts.middleware:
            mounts_bits.append(
                f"middleware {m.mounts.middleware} mounts app-wide (opt-OUT via the config surface)"
            )
        mounts_line = ("Mounts: " + "; ".join(mounts_bits) + ".\n\n") if mounts_bits else ""
        guide = tree.get(m.guide, b"").decode("utf-8", errors="replace")
        return ToolOutcome(
            success=True,
            content=(
                f"installed trusted component '{m.name}' {m.version} into "
                f"{INSTALL_PREFIX}/{m.name}/ — core files are integrity-pinned (edit "
                f"config/, never core/). {mounts_line}{guide}"
            ),
            structured={
                "name": m.name,
                "version": m.version,
                "written": sorted(to_write),
                "config_surface": [_install_path(m.name, p) for p in m.config_surface],
            },
        )


class EjectTrustedComponentArgs(BaseModel):
    name: str = Field(description="Installed component to eject (take ownership of).")
    reason: str = Field(description="Why — recorded verbatim in the eject history.")


class EjectTrustedComponentTool:
    definition = ToolDef(
        name="eject_trusted_component",
        description=(
            "Take ownership of an installed trusted component's files: the verified "
            "badge is removed, upgrades stop applying, and the copy is yours to edit "
            "freely. One-way (reinstalling later requires reverting core edits). "
            "Never required for config/ edits — those are always allowed."
        ),
        args_model=EjectTrustedComponentArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: EjectTrustedComponentArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            lock = await _load_lock(ctx)
        except LockfileCorrupt as exc:
            return _corrupt_refusal(exc)
        entry = lock.components.get(args.name)
        if entry is None:
            return ToolOutcome(
                success=False,
                error="not_installed",
                content=f"{args.name!r} is not installed — nothing to eject.",
            )
        now = datetime.now(UTC).isoformat()
        changed = record_eject(lock, args.name, "explicit", now, note=args.reason)
        if not changed:
            return ToolOutcome(
                success=True,
                content=f"'{args.name}' was already ejected — it is already yours.",
                structured={"name": args.name, "already_ejected": True},
            )
        await ctx.sandbox.write_file(LOCKFILE_RELPATH, dump_lock(lock))
        guide_path = _install_path(args.name, "GUIDE.md")
        if await ctx.sandbox.file_exists(guide_path):
            guide = await ctx.sandbox.read_file(guide_path)
            banner = (
                f"\n\n---\n\n> ⚠ **Ejected {now}** — this copy diverged from the "
                f"registry and is now custom code you own. Upgrades and the verified "
                f"badge no longer apply. Reason: {args.reason}\n"
            ).encode()
            await ctx.sandbox.write_file(guide_path, guide + banner)
        return ToolOutcome(
            success=True,
            content=(
                f"ejected '{args.name}': its files are yours now. The verified badge is "
                "removed and upgrades stop applying — recorded in the lockfile history."
            ),
            structured={"name": args.name, "already_ejected": False},
        )
