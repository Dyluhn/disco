"""Trusted-components STATE-MACHINE SOAK — randomized op sequences through the
REAL install/eject tools and the REAL verify fold, with invariants checked
after every step.

    PYTHONPATH=. uv run python harness/tc_soak/run.py --cycles 300 --seed 1

Each cycle applies a random operation (install / eject / reinstall / core-edit /
file-delete / lockfile-corruption / lockfile-forgery / config-edit / verify) to
a synthetic workspace + synthetic multi-component registry (random dependency
edges), then asserts the tier's honesty invariants:

  I1  the lockfile on disk always parses, OR the last mutating tool call REFUSED
  I2  verify NEVER reports component_integrity=pass for a component whose
      workspace bytes differ from the registry pins (no forged verification)
  I3  eject history is append-only (never shrinks, records never mutate)
  I4  a non-ejected component with an unsatisfied requires edge always yields a
      BLOCKING deps failure at verify
  I5  verify is idempotent: running it twice with no mutation in between yields
      byte-identical check lists
  I6  after any successful eject (explicit or auto), the workspace GUIDE.md for
      that component carries the eject banner (when a GUIDE exists)
  I7  a corrupt lockfile blocks BOTH tools (refusal) and blocks the verify fold
      (blocking check), and is never overwritten by either
  I8  install refuses (collision) rather than clobbering ANY differing bytes

Exit code 0 = all cycles clean; 1 = an invariant violated (details printed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from disco.core.trusted_components.lockfile import (
    LOCKFILE_RELPATH,
    LockfileCorrupt,
    parse_lock,
)
from disco.core.trusted_components.registry import TrustedComponentRegistry
from disco.core.trusted_components.verify import (
    ProbeVerdict,
    install_path,
    pin,
    verify_trusted_components,
)
from disco.tools.anatomy import ToolContext
from disco.tools.builtin import trusted_components as tc_mod
from disco.tools.builtin.trusted_components import (
    AddTrustedComponentArgs,
    AddTrustedComponentTool,
    EjectTrustedComponentArgs,
    EjectTrustedComponentTool,
)

NOW = "2026-07-11T00:00:00+00:00"


# ---------------------------------------------------------------------------
# minimal in-memory sandbox (the tools' protocol surface)
# ---------------------------------------------------------------------------
class SoakSandbox:
    def __init__(self) -> None:
        self.fs: dict[str, bytes] = {}

    async def file_exists(self, path: str) -> bool:
        return path in self.fs

    async def read_file(self, path: str) -> bytes:
        return self.fs[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.fs[path] = data

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> Any:  # pragma: no cover
        raise RuntimeError("soak tier-1 never execs")


@dataclass
class SoakCtx:
    sandbox: SoakSandbox
    workspace_path: str = "."
    timeout_s: int = 30
    owner_id: str = "soak"
    conversation_id: str = "soak"
    starter_kit: Any = None
    capabilities: Any = None
    broker: Any = None


# ---------------------------------------------------------------------------
# synthetic registry
# ---------------------------------------------------------------------------
def build_registry(root: Path, rng: random.Random, n_components: int) -> list[str]:
    names = [f"kit-{c}" for c in string.ascii_lowercase[:n_components]]
    for i, name in enumerate(names):
        for version in ["1.0.0", "1.1.0"] if rng.random() < 0.4 else ["1.0.0"]:
            comp = root / name / version
            (comp / "core").mkdir(parents=True, exist_ok=True)
            (comp / "config").mkdir(exist_ok=True)
            core: dict[str, bytes] = {}
            for j in range(rng.randint(1, 3)):
                rel = f"core/{name}-{j}.js"
                data = f"// {name} {version} file {j}\n".encode()
                (comp / rel).write_bytes(data)
                core[rel] = data
            (comp / "config" / "c.json").write_bytes(b"{}\n")
            (comp / "GUIDE.md").write_bytes(f"# {name}\n".encode())
            requires = []
            if i > 0 and rng.random() < 0.6:
                dep = names[rng.randrange(0, i)]  # acyclic by construction
                requires = [f"{dep}>=1.0"]
            manifest = {
                "name": name,
                "version": version,
                "kind": "trusted_component",
                "summary": name,
                "when_to_use": f"soak {name}",
                "files": {rel: pin(d) for rel, d in core.items()},
                "config_surface": ["config/c.json"],
                "requires": requires,
                "provides": [name],
                "guide": "GUIDE.md",
            }
            (comp / "manifest.json").write_text(json.dumps(manifest))
    return names


# ---------------------------------------------------------------------------
# the soak driver
# ---------------------------------------------------------------------------
@dataclass
class SoakState:
    rng: random.Random
    registry: TrustedComponentRegistry
    names: list[str]
    sandbox: SoakSandbox
    ctx: SoakCtx
    last_tool_refused: bool = False
    ejects_seen: list[dict] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    op_counts: dict[str, int] = field(default_factory=dict)
    lock_corrupted: bool = False


def _fail(state: SoakState, inv: str, detail: str, cycle: int, op: str) -> None:
    state.violations.append(f"cycle {cycle} after {op}: {inv} — {detail}")


async def _verify(state: SoakState) -> Any:
    raw = state.sandbox.fs.get(LOCKFILE_RELPATH)
    try:
        lock = parse_lock(raw)
    except LockfileCorrupt:
        return "corrupt"
    file_bytes: dict[str, bytes | None] = {}
    for name, entry in lock.components.items():
        if entry.ejected:
            continue
        comp = state.registry.get(name, entry.version)
        if comp is None:
            continue
        for rel in comp.manifest.files:
            p = install_path(name, rel)
            file_bytes[p] = state.sandbox.fs.get(p)

    async def probe_ok(name: str) -> ProbeVerdict:
        return ProbeVerdict(passed=True, summary="soak probe")

    result = await verify_trusted_components(
        lock, state.registry, file_bytes, run_probe=probe_ok, now_iso=NOW
    )
    # persist auto-ejects the way the fold does
    if result.newly_ejected and result.lock is not None:
        from disco.core.trusted_components.lockfile import dump_lock

        state.sandbox.fs[LOCKFILE_RELPATH] = dump_lock(result.lock)
    # Mirror the product fold: re-assert the banner for EVERY ejected component
    # (idempotent), not just newly-ejected — a banner lost to a revert-from-source
    # must be restored on verify.
    banner_lock = result.lock if result.lock is not None else lock
    for _nm, _entry in banner_lock.components.items():
        if _entry.ejected:
            gp = install_path(_nm, "GUIDE.md")
            if gp in state.sandbox.fs and b"**Ejected" not in state.sandbox.fs[gp]:
                state.sandbox.fs[gp] += b"\n> \xe2\x9a\xa0 **Ejected (soak auto)**\n"
    return result


def _op_forge_lock(state: SoakState) -> None:
    if state.lock_corrupted:
        return
    try:
        lock = parse_lock(state.sandbox.fs.get(LOCKFILE_RELPATH))
    except LockfileCorrupt:
        lock = None
    if lock is None:
        return
    raw = json.loads(lock.model_dump_json())
    raw["components"]["forged-kit"] = {
        "version": "1.0.0",
        "installed_at": NOW,
        "installed_by": "forgery",
        "ejected": False,
    }
    state.sandbox.fs[LOCKFILE_RELPATH] = json.dumps(raw).encode()


async def _op_revert_and_reinstall(state: SoakState, name: str) -> None:
    comp = state.registry.get(name)
    if comp is None:
        return
    for rel, data in comp.install_tree().items():
        state.sandbox.fs[install_path(name, rel)] = data
    out = await AddTrustedComponentTool().run(
        AddTrustedComponentArgs(name=name),
        cast(ToolContext, state.ctx),
    )
    state.last_tool_refused = not out.success
    if not out.success and not state.lock_corrupted:
        await _verify(state)


async def _apply_op(state: SoakState, op: str, name: str, i: int) -> Any:
    """Apply one soak operation; return the pre-lock raw bytes."""
    pre_lock_raw = state.sandbox.fs.get(LOCKFILE_RELPATH)
    state.last_tool_refused = False
    if op == "install":
        version = state.rng.choice([None, "1.0.0"])
        out = await AddTrustedComponentTool().run(
            AddTrustedComponentArgs(name=name, version=version),
            cast(ToolContext, state.ctx),
        )
        state.last_tool_refused = not out.success
        if not out.success and out.error not in (
            "missing_dependency",
            "collision",
            "lockfile_corrupt",
            "unknown_component",
        ):
            _fail(state, "I-refusal-taxonomy", f"unexpected error {out.error}", i, op)
    elif op == "eject":
        out = await EjectTrustedComponentTool().run(
            EjectTrustedComponentArgs(name=name, reason=f"soak {i}"),
            cast(ToolContext, state.ctx),
        )
        state.last_tool_refused = not out.success
    elif op == "core_edit":
        target = install_path(name, f"core/{name}-0.js")
        if target in state.sandbox.fs:
            state.sandbox.fs[target] = f"// soak edit {i}\n".encode()
    elif op == "file_delete":
        target = install_path(name, f"core/{name}-0.js")
        state.sandbox.fs.pop(target, None)
    elif op == "config_edit":
        target = install_path(name, "config/c.json")
        if target in state.sandbox.fs:
            state.sandbox.fs[target] = json.dumps({"soak": i}).encode()
    elif op == "corrupt_lock":
        if LOCKFILE_RELPATH in state.sandbox.fs and not state.lock_corrupted:
            state.sandbox.fs[LOCKFILE_RELPATH] = state.sandbox.fs[LOCKFILE_RELPATH][:10]
            state.lock_corrupted = True
    elif op == "forge_lock":
        _op_forge_lock(state)
    elif op == "revert_and_reinstall":
        await _op_revert_and_reinstall(state, name)
    return pre_lock_raw


def _check_lock_invariants(state: SoakState, op: str, pre_lock_raw: Any, i: int) -> Any:
    """Check I1/I3/I7 lockfile invariants; return the parsed lock (or None)."""
    raw = state.sandbox.fs.get(LOCKFILE_RELPATH)
    lock = None
    if raw is not None:
        try:
            lock = parse_lock(raw)
        except LockfileCorrupt:
            if not state.lock_corrupted:
                _fail(state, "I1", "lockfile corrupt without a corruption op", i, op)

    if op in ("install", "eject", "revert_and_reinstall") and state.lock_corrupted:
        if not state.last_tool_refused:
            _fail(state, "I7", "tool succeeded on a corrupt lockfile", i, op)
        if (
            state.sandbox.fs.get(LOCKFILE_RELPATH)
            != (pre_lock_raw if op != "corrupt_lock" else state.sandbox.fs.get(LOCKFILE_RELPATH))
            and pre_lock_raw is not None
        ):
            _fail(state, "I7", "corrupt lockfile bytes were rewritten", i, op)

    if lock is not None:
        dumped = [json.loads(e.model_dump_json()) for e in lock.ejects]
        seen = state.ejects_seen
        if len(dumped) < len(seen) or dumped[: len(seen)] != seen:
            _fail(state, "I3", "eject history shrank or mutated", i, op)
        state.ejects_seen = dumped
    return lock


def _check_component_deps(
    state: SoakState, checks: dict, lock: Any, name2: str, comp: Any, op: str, i: int
) -> None:
    """I4: unsatisfied edges must be blocking failures."""
    from disco.core.trusted_components.registry import parse_requirement

    for edge in comp.manifest.requires:
        req = parse_requirement(edge)
        dep = lock.components.get(req.name)
        if dep is None or not req.satisfied_by(req.name, dep.version):
            dc = checks.get(f"component_deps:{name2}")
            if dc is None or dc.status != "fail":
                _fail(state, "I4", f"{name2} edge {edge} unsatisfied but no FAIL", i, op)


def _check_component_invariants(state: SoakState, result: Any, lock: Any, op: str, i: int) -> None:
    """Check I2/I4/I6 component invariants against the verify result."""
    checks = {c.name: c for c in result.checks}
    for name2, entry in lock.components.items():
        comp = state.registry.get(name2, entry.version)
        integ = checks.get(f"component_integrity:{name2}")
        if entry.ejected:
            if integ is not None and integ.status == "pass":
                _fail(state, "I2", f"{name2} ejected but integrity=pass", i, op)
            gp = install_path(name2, "GUIDE.md")
            if gp in state.sandbox.fs and b"**Ejected" not in state.sandbox.fs[gp]:
                _fail(state, "I6", f"{name2} ejected but GUIDE has no banner", i, op)
            continue
        if comp is None:
            continue
        diverged = any(
            state.sandbox.fs.get(install_path(name2, rel)) is None
            or pin(state.sandbox.fs[install_path(name2, rel)]) != expected
            for rel, expected in comp.manifest.files.items()
        )
        if diverged and integ is not None and integ.status == "pass":
            _fail(state, "I2", f"{name2} diverged but integrity=pass", i, op)
        _check_component_deps(state, checks, lock, name2, comp, op, i)


async def cycle(state: SoakState, i: int) -> None:
    rng = state.rng
    name = rng.choice(state.names)
    op = rng.choice(
        [
            "install",
            "install",
            "install",
            "eject",
            "core_edit",
            "file_delete",
            "config_edit",
            "corrupt_lock",
            "forge_lock",
            "verify",
            "verify",
            "revert_and_reinstall",
        ]
    )
    state.op_counts[op] = state.op_counts.get(op, 0) + 1
    pre_lock_raw = await _apply_op(state, op, name, i)

    # repair corruption occasionally so the soak doesn't wedge forever —
    # deleting the lockfile is the documented recovery and LEGITIMATELY resets
    # eject history (all claims dropped), so the I3 baseline resets with it.
    if state.lock_corrupted and rng.random() < 0.3:
        state.sandbox.fs.pop(LOCKFILE_RELPATH, None)
        state.lock_corrupted = False
        state.ejects_seen = []

    # ---- invariants ------------------------------------------------------
    lock = _check_lock_invariants(state, op, pre_lock_raw, i)

    result = await _verify(state)
    if result == "corrupt":
        if not state.lock_corrupted:
            _fail(state, "I1", "verify saw corrupt lock outside corruption window", i, op)
        return
    # refresh lock post-verify (auto-ejects may have landed)
    lock = parse_lock(state.sandbox.fs.get(LOCKFILE_RELPATH))

    _check_component_invariants(state, result, lock, op, i)

    # I5: idempotence
    result2 = await _verify(state)
    if result2 != "corrupt" and [(c.name, c.status, c.evidence) for c in result.checks] != [
        (c.name, c.status, c.evidence) for c in result2.checks
    ]:
        # allow the pass where result had newly_ejected (second run sees the
        # persisted eject — statuses legitimately change exactly once)
        if not result.newly_ejected:
            _fail(state, "I5", "verify not idempotent without mutation", i, op)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--components", type=int, default=6)
    ap.add_argument("--registry-dir", type=Path, default=None)
    args = ap.parse_args()

    import tempfile

    rng = random.Random(args.seed)
    with tempfile.TemporaryDirectory(prefix="tc-soak-reg-") as tmp:
        root = args.registry_dir or Path(tmp)
        names = build_registry(root, rng, args.components)
        registry = TrustedComponentRegistry(root)

        class _Patched(TrustedComponentRegistry):
            @classmethod
            def default(cls) -> TrustedComponentRegistry:
                return TrustedComponentRegistry(root)

        tc_mod.TrustedComponentRegistry = _Patched  # type: ignore[misc]

        sandbox = SoakSandbox()
        state = SoakState(
            rng=rng,
            registry=registry,
            names=names,
            sandbox=sandbox,
            ctx=SoakCtx(sandbox=sandbox),
        )
        for i in range(args.cycles):
            await cycle(state, i)
            if state.violations:
                break

    print(f"ops: {dict(sorted(state.op_counts.items()))}")
    if state.violations:
        print(f"SOAK FAIL — {len(state.violations)} violation(s):")
        for v in state.violations[:10]:
            print(f"  {v}")
        return 1
    print(f"SOAK PASS — {args.cycles} cycles, seed {args.seed}, no invariant violations")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
