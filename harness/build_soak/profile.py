"""Fast qualification profiles — F0 / F1 preflight, with zero promotion credit.

Three levels exist in the reliability workflow::

    F0 — provider-free deterministic checks
    F1 — small live qualification profile   (this module)
    F2 — the unchanged full diagnostic/promotion campaign

F0 and F1 are *falsification* tools: they find a broken candidate cheaply. Only
the governed F2 lanes can certify anything.

This module deliberately owns almost nothing. Selection is a versioned manifest
of scenario ids that already exist in the governed scenarios file; execution is
the existing :mod:`harness.build_soak.run` runner, with its own oracles,
evidence collection, classification, cleanup, resource admission, and
stop-first cohort behaviour. Nothing here forks or reimplements those.

Structural exclusion from promotion
-----------------------------------
``harness/reliability/run.py::_build_soak_result`` discovers evidence with
``sorted(out.rglob("batch-summary.json"), key=mtime)`` and reads the newest
match. A label saying "this does not count" would therefore be worthless: the
reader never looks at labels.

So the exclusion is structural instead. This runner passes
``--summary-name profile-summary.json``, so the filename the promotion reader
globs for **is never created**. :func:`assert_not_promotion_visible` re-checks
that after the fact, and the receipt records ``counts_toward_promotion: false``
for humans.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .run import _BATCH_SUMMARY_NAME, load_scenarios

_PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
_SCENARIO_DIR = Path(__file__).resolve().parent
_PROFILE_SUMMARY_NAME = "profile-summary.json"
_RECEIPT_NAME = "qualification-receipt.json"

SUPPORTED_SCHEMA = 1


class ProfileError(ValueError):
    """A profile could not be resolved. Always raised BEFORE provider spend."""


# --------------------------------------------------------------------------
# Resolution — every failure here happens before a single provider call
# --------------------------------------------------------------------------


def profile_path(name: str) -> Path:
    if "/" in name or "\\" in name:
        raise ProfileError(f"profile name must be a bare id, got {name!r}")
    path = _PROFILE_DIR / f"{name}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in _PROFILE_DIR.glob("*.yaml"))
        raise ProfileError(f"unknown profile {name!r}; available: {available}")
    return path


def manifest_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_profile(name: str) -> tuple[dict[str, Any], Path, str]:
    path = profile_path(name)
    digest = manifest_digest(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"profile {name!r} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"profile {name!r} must be a mapping")

    schema = raw.get("schema_version")
    if schema != SUPPORTED_SCHEMA:
        raise ProfileError(
            f"profile {name!r} declares schema_version {schema!r}; "
            f"this runner supports {SUPPORTED_SCHEMA}"
        )
    if raw.get("counts_toward_promotion") is not False:
        raise ProfileError(
            f"profile {name!r} must declare counts_toward_promotion: false — "
            "a qualification profile can never earn promotion credit"
        )
    if not isinstance(raw.get("base"), list) or not raw["base"]:
        raise ProfileError(f"profile {name!r} has no base scenario list")
    return raw, path, digest


def resolve_selection(
    profile: dict[str, Any], overlays: list[str]
) -> tuple[list[str], dict[str, Any]]:
    """Deterministic scenario ids and order, validated against the real file.

    Unknown, duplicate, and missing ids all raise here — before provider spend.
    """
    scenarios_file = _SCENARIO_DIR / str(profile.get("scenarios_file") or "scenarios.yaml")
    if not scenarios_file.is_file():
        raise ProfileError(f"profile scenarios_file does not exist: {scenarios_file}")
    known = load_scenarios(str(scenarios_file))

    selected: list[str] = []
    overlay_meta: dict[str, Any] = {}

    def admit(ids: list[Any], origin: str) -> None:
        for entry in ids:
            if not isinstance(entry, str) or not entry:
                raise ProfileError(f"{origin}: scenario id must be a non-empty string")
            if entry not in known:
                raise ProfileError(
                    f"{origin}: unknown scenario id {entry!r} "
                    f"(not present in {scenarios_file.name})"
                )
            if entry in selected:
                raise ProfileError(f"{origin}: duplicate scenario id {entry!r}")
            selected.append(entry)

    admit(list(profile["base"]), "base")

    declared_overlays = profile.get("overlays") or {}
    for name in overlays:
        if name not in declared_overlays:
            raise ProfileError(f"unknown overlay {name!r}; declared: {sorted(declared_overlays)}")
        spec = declared_overlays[name] or {}
        overlay_meta[name] = {"kind": spec.get("kind")}
        if spec.get("kind") == "live":
            admit(list(spec.get("scenarios") or []), f"overlay {name}")
        else:
            overlay_meta[name]["pytest"] = list(spec.get("pytest") or [])

    return selected, overlay_meta


# --------------------------------------------------------------------------
# Structural promotion exclusion
# --------------------------------------------------------------------------


def assert_not_promotion_visible(root: Path) -> None:
    """Fail loudly if anything under ``root`` is discoverable as promotion evidence.

    This is the backstop for the structural exclusion. The runner already
    writes a different filename; this proves none leaked back in.
    """
    visible = sorted(root.rglob(_BATCH_SUMMARY_NAME))
    if visible:
        raise ProfileError(
            "qualification output is visible to the promotion reader: "
            + ", ".join(str(p) for p in visible)
            + f" — the reader discovers {_BATCH_SUMMARY_NAME!r} by rglob and reads "
            "the newest match, so this run could be counted as governed evidence."
        )


# --------------------------------------------------------------------------
# Receipt
# --------------------------------------------------------------------------


def _source_fingerprint(repo_root: Path) -> str | None:
    script = repo_root / "scripts" / "source_fingerprint.py"
    if not script.is_file():
        return None
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--which", "source", "--quiet"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def build_receipt(
    *,
    profile_name: str,
    profile: dict[str, Any],
    manifest_path: Path,
    digest: str,
    selection: list[str],
    overlay_meta: dict[str, Any],
    level: str,
    repo_root: Path,
    binding: dict[str, Any],
    results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "fast_qualification_receipt",
        # The whole point of this file, stated first.
        "counts_toward_promotion": False,
        "promotion_exclusion": {
            "mechanism": "structural",
            "summary_name_written": _PROFILE_SUMMARY_NAME,
            "promotion_reader_globs": _BATCH_SUMMARY_NAME,
            "note": (
                "harness/reliability/run.py::_build_soak_result discovers evidence "
                "by rglob on the promotion name and reads the newest match; that "
                "name is never created by this lane."
            ),
        },
        "level": level,
        "profile": {
            "id": profile_name,
            "declared_id": profile.get("id"),
            "version": profile.get("version"),
            "schema_version": profile.get("schema_version"),
            "manifest_path": str(manifest_path),
            "manifest_sha256": digest,
            "scenarios_file": profile.get("scenarios_file"),
        },
        "selection": {
            "scenario_ids": list(selection),
            "count": len(selection),
            "order_is_deterministic": True,
        },
        "overlays": overlay_meta,
        "binding": binding,
        "source_fingerprint": _source_fingerprint(repo_root),
        "created_at": datetime.now(UTC).isoformat(),
        "results": results or {},
    }


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_pytest(targets: list[str], repo_root: Path) -> tuple[int, str]:
    if not targets:
        return 0, "no targets"
    venv_python = repo_root / ".venv" / "bin" / "python3"
    exe = str(venv_python) if venv_python.is_file() else sys.executable
    cmd = [exe, "-m", "pytest", "-q", "-m", "not integration", *targets]
    proc = subprocess.run(cmd, cwd=str(repo_root))
    return proc.returncode, " ".join(cmd)


def cmd_list(args: argparse.Namespace) -> int:
    """Resolve and print the exact selection. Makes ZERO provider calls."""
    profile, path, digest = load_profile(args.profile)
    selection, overlay_meta = resolve_selection(profile, args.overlay)
    print(f"profile            : {args.profile} v{profile.get('version')}")
    print(f"manifest           : {path}")
    print(f"manifest sha256    : {digest}")
    print(f"scenarios file     : {profile.get('scenarios_file')}")
    print(f"counts_toward_promotion: {profile.get('counts_toward_promotion')}")
    print(f"overlays requested : {args.overlay or '(none)'}")
    print(f"selection ({len(selection)}), in exact run order:")
    for index, scenario_id in enumerate(selection):
        print(f"  {index:>2}. {scenario_id}")
    for name, meta in overlay_meta.items():
        if meta.get("pytest"):
            print(f"provider-free overlay {name}: {meta['pytest']}")
    print("\nNO provider calls were made.")
    return 0


def cmd_f0(args: argparse.Namespace) -> int:
    """F0 — provider-free deterministic checks."""
    profile, path, digest = load_profile(args.profile)
    selection, overlay_meta = resolve_selection(profile, args.overlay)
    repo_root = _repo_root()

    targets = list((profile.get("f0") or {}).get("pytest") or [])
    for meta in overlay_meta.values():
        targets.extend(meta.get("pytest") or [])
    code, cmd = _run_pytest(targets, repo_root)

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    receipt = build_receipt(
        profile_name=args.profile,
        profile=profile,
        manifest_path=path,
        digest=digest,
        selection=selection,
        overlay_meta=overlay_meta,
        level="F0",
        repo_root=repo_root,
        binding={"provider": None, "model": None, "note": "F0 makes no provider calls"},
        results={"pytest_command": cmd, "exit_code": code},
    )
    (out_root / _RECEIPT_NAME).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    assert_not_promotion_visible(out_root)
    print(f"[F0] receipt -> {out_root / _RECEIPT_NAME}  (counts_toward_promotion: false)")
    return code


def cmd_f1(args: argparse.Namespace) -> int:
    """F1 — the small live qualification profile, via the existing runner."""
    profile, path, digest = load_profile(args.profile)
    selection, overlay_meta = resolve_selection(profile, args.overlay)
    repo_root = _repo_root()
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    scenarios_file = _SCENARIO_DIR / str(profile.get("scenarios_file"))
    argv = [
        "--scenario",
        ",".join(selection),
        "--scenarios",
        str(scenarios_file),
        "--iterations",
        str(len(selection)),
        "--seed-base",
        str(args.seed_base),
        "--out",
        str(out_root),
        # THE structural exclusion: the promotion reader's filename is never written.
        "--summary-name",
        _PROFILE_SUMMARY_NAME,
        "--model",
        args.model,
    ]
    if args.autonomous:
        argv.append("--autonomous")
    if args.parallel:
        argv += ["--parallel", args.parallel]

    binding = {
        "runner": "harness.build_soak.run",
        "argv": argv,
        "model": args.model,
        "seed_base": args.seed_base,
    }

    if args.dry_run:
        receipt = build_receipt(
            profile_name=args.profile,
            profile=profile,
            manifest_path=path,
            digest=digest,
            selection=selection,
            overlay_meta=overlay_meta,
            level="F1-dry-run",
            repo_root=repo_root,
            binding=binding,
            results={"dry_run": True, "provider_calls": 0},
        )
        (out_root / _RECEIPT_NAME).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        assert_not_promotion_visible(out_root)
        print("[F1 dry-run] would invoke: python -m harness.build_soak.run " + " ".join(argv))
        print(f"[F1 dry-run] receipt -> {out_root / _RECEIPT_NAME}")
        print("NO provider calls were made.")
        return 0

    from .run import main as run_main

    code = run_main(argv)
    receipt = build_receipt(
        profile_name=args.profile,
        profile=profile,
        manifest_path=path,
        digest=digest,
        selection=selection,
        overlay_meta=overlay_meta,
        level="F1",
        repo_root=repo_root,
        binding=binding,
        results={"exit_code": code},
    )
    (out_root / _RECEIPT_NAME).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    assert_not_promotion_visible(out_root)
    print(f"[F1] receipt -> {out_root / _RECEIPT_NAME}  (counts_toward_promotion: false)")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fast qualification profiles (F0/F1). Earns ZERO promotion credit."
    )
    parser.add_argument("--profile", default="fast_qualification")
    parser.add_argument(
        "--overlay",
        action="append",
        default=[],
        help="named overlay, repeatable (e.g. --overlay context --overlay freeze)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    lister = sub.add_parser("list", help="resolve and print the selection; no provider calls")
    lister.set_defaults(func=cmd_list)

    f0 = sub.add_parser("f0", help="provider-free deterministic checks")
    f0.add_argument("--out", default="test-record/qualification/f0")
    f0.set_defaults(func=cmd_f0)

    f1 = sub.add_parser("f1", help="small live qualification profile")
    f1.add_argument("--out", default="test-record/qualification/f1")
    f1.add_argument("--model", default="")
    f1.add_argument("--seed-base", type=int, required=True)
    f1.add_argument("--parallel", default="")
    f1.add_argument("--autonomous", action="store_true")
    f1.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve, validate and write a receipt without any provider call",
    )
    f1.set_defaults(func=cmd_f1)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ProfileError as exc:
        print(f"[qualification] REFUSED: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
