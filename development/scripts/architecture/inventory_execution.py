"""Execution-coverage gate: a certified test id must be reachable by a sanctioned command.

PKG-19-CERT-STRUCTURAL findings F1, F2 and F6 are three faces of one defect: the
repository has **two collectors and no comparator**.

* The inventory's own collector (:mod:`architecture.test_inventory`) names each
  root explicitly, clears ``addopts``, and — decisively —
  :func:`architecture.test_inventory_parts._rows.collection_env` sets
  ``PYTHONPATH`` to the repository root.
* The *sanctioned* commands (the ``Makefile`` hermetic targets and the CI
  workflow steps) did none of that.

So ``development/architecture/test-inventory.json`` could certify, in good faith, ids that no
command the repository actually commits to running was able to collect:

* **F2** — 62 ids in 6 modules importing ``harness.*`` / ``packages.*``, which
  bare ``uv run pytest`` (``make unit``) could not collect at all.
* **F1** — 25 ids under ``development/tests/unbiased_gate``, excluded from ``testpaths``,
  16 of them red for months against APIs three accepted transitions had moved.
* A third instance found while writing this gate: the ``integrations`` root is
  certified with ids and named by **no** Makefile target and **no** CI step.

None of it was visible to any gate, because no gate compared the two sets. This
module is that comparison.

**This gate refuses to become the very thing it catches.** Its declared command
list is checked against the real ``Makefile`` and CI workflow bytes
(:func:`declaration_drift`), so a third copy of the truth cannot silently drift
from the first two — the mistake F7 records against ``ci_contract.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .policy import REPO_ROOT, load_json

#: Commands the repository actually commits to running, each tagged with the
#: file and target/step it is quoted from. ``needs_repo_root_on_path`` records
#: whether the command itself provisions ``PYTHONPATH`` — the F2 variable.
SANCTIONED_COMMANDS: tuple[dict[str, Any], ...] = (
    {
        "label": "make unit",
        "source": "Makefile:unit",
        "argv": ["-m", "pytest", "--collect-only"],
        "needs_repo_root_on_path": False,
        "quote": "uv run pytest",
    },
    {
        "label": "make harness",
        "source": "Makefile:harness",
        "argv": ["-m", "pytest", "--collect-only", "development/harness"],
        "needs_repo_root_on_path": True,
        "quote": "PYTHONPATH=. uv run pytest development/harness",
    },
    {
        "label": "make integrations",
        "source": "Makefile:integrations",
        "argv": ["-m", "pytest", "--collect-only", "current/integrations"],
        "needs_repo_root_on_path": True,
        "quote": "PYTHONPATH=. uv run pytest current/integrations",
    },
    {
        "label": "ci required unit",
        "source": ".github/workflows/ci.yml",
        "argv": ["-m", "pytest", "--collect-only", "-m", "not integration"],
        "needs_repo_root_on_path": False,
        "quote": 'uv run pytest -m "not integration"',
    },
)

# `addopts` in pyproject.toml already carries `-q -m 'not sandbox_integration'`.
# Adding a second `-q` here makes it `-qq`, which switches pytest's
# `--collect-only` output from `path::testname` lines to a per-file count summary
# — so every command would collect zero ids and the comparison would pass
# vacuously. The first run of this gate did exactly that; the empty-set guard in
# `check_inventory_execution` caught it. The marker selection is deliberately
# left to `addopts`, because that IS the sanctioned commands' real behavior.

_DECLARATION_SOURCES = ("Makefile", ".github/workflows/ci.yml")
_MAX_REPORTED = 25


def _collection_env(root: Path, *, repo_root_on_path: bool) -> dict[str, str]:
    """Environment for one sanctioned command.

    Only ``PYTHONPATH`` varies, and only when the quoted command itself sets it.
    Injecting it everywhere is exactly the provisioning difference that let F2
    hide, so this gate reproduces each command's real provisioning rather than
    its own.
    """
    import os

    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    if repo_root_on_path:
        env["PYTHONPATH"] = str(root)
    else:
        env.pop("PYTHONPATH", None)
    return env


def collect_for_command(root: Path, spec: dict[str, Any]) -> tuple[set[str], str]:
    """Collect the node ids one sanctioned command reaches.

    A non-zero exit is returned as an error string rather than an empty set: a
    control that compares two empty outputs proves nothing, and a collection
    failure is precisely the F2 symptom this gate exists to surface.
    """
    try:
        result = subprocess.run(
            [sys.executable, *spec["argv"]],
            capture_output=True,
            text=True,
            cwd=root,
            env=_collection_env(root, repo_root_on_path=spec["needs_repo_root_on_path"]),
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), f"{spec['label']}: collection did not run: {exc}"
    ids = {line.strip() for line in result.stdout.splitlines() if "::" in line.strip()}
    if result.returncode and not ids:
        tail = "; ".join((result.stderr or result.stdout).strip().splitlines()[-3:])
        return set(), f"{spec['label']}: exit {result.returncode} collecting nothing: {tail}"
    return ids, ""


def certified_ids(root: Path) -> dict[str, set[str]]:
    """The ids ``development/architecture/test-inventory.json`` certifies, per root."""
    inventory = load_json(root / "development" / "architecture" / "test-inventory.json")
    roots = inventory.get("collected", {}).get("roots", {})
    return {name: set(ids) for name, ids in roots.items()}


def declaration_drift(root: Path) -> list[str]:
    """Fail if a declared command is not quoted verbatim in its named source.

    Without this, :data:`SANCTIONED_COMMANDS` would be a third authority beside
    the Makefile and the CI workflow, free to drift from both — the shape F7
    records against ``ci_contract.py``'s private copy of the ordered gate list.
    """
    texts: dict[str, str] = {}
    for name in _DECLARATION_SOURCES:
        path = root / name
        texts[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    problems: list[str] = []
    for spec in SANCTIONED_COMMANDS:
        source_file = spec["source"].split(":", 1)[0]
        if spec["quote"] not in texts.get(source_file, ""):
            problems.append(
                f"declared command {spec['label']!r} quotes {spec['quote']!r}, "
                f"which no longer appears in {source_file} — the declaration has "
                "drifted from the command the repository actually runs"
            )
    return problems


def _executable_ids(root: Path) -> tuple[set[str], list[str]]:
    executable: set[str] = set()
    problems: list[str] = []
    for spec in SANCTIONED_COMMANDS:
        ids, error = collect_for_command(root, spec)
        if error:
            problems.append(error)
            continue
        executable |= ids
    return executable, problems


def _orphan_problems(certified: dict[str, set[str]], executable: set[str]) -> list[str]:
    problems: list[str] = []
    for name, ids in sorted(certified.items()):
        orphans = sorted(ids - executable)
        if not orphans:
            continue
        shown = ", ".join(orphans[:_MAX_REPORTED])
        more = f" (+{len(orphans) - _MAX_REPORTED} more)" if len(orphans) > _MAX_REPORTED else ""
        problems.append(
            f"root {name!r}: {len(orphans)} of {len(ids)} certified ids are reachable "
            f"by no sanctioned command: {shown}{more}"
        )
    return problems


def check_inventory_execution(root: Path | None = None) -> dict[str, Any]:
    """Compare the certified inventory against what sanctioned commands collect."""
    resolved = REPO_ROOT if root is None else root
    certified = certified_ids(resolved)
    total_certified = len(set().union(*certified.values())) if certified else 0

    problems = declaration_drift(resolved)
    executable, collection_problems = _executable_ids(resolved)
    problems.extend(collection_problems)

    if not executable:
        problems.append(
            "no sanctioned command collected any id — refusing to report coverage "
            "from an empty set (a comparison against nothing always passes)"
        )
        return {
            "ok": False,
            "problems": problems,
            "certified_total": total_certified,
            "executable_total": 0,
            "orphan_total": total_certified,
        }

    problems.extend(_orphan_problems(certified, executable))
    orphan_total = len(set().union(*certified.values()) - executable) if certified else 0
    return {
        "ok": not problems,
        "problems": problems,
        "certified_total": total_certified,
        "executable_total": len(executable),
        "orphan_total": orphan_total,
    }
