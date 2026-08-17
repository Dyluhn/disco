"""Shared logic for the project-local governance and hourly-review hooks.

Stdlib only, and deliberately independent of the repository virtualenv: these
hooks must keep working while `.venv` is being rebuilt mid-campaign.

State lives under `.claude/runtime/`, which is gitignored.  Nothing here writes
to a tracked path.

The protected-file set is imported from `development/scripts/check_governance_seal.py` so
there is exactly one definition of "what is sealed".  That script is also the
authoritative verification backstop; the hooks shell out to it rather than
reimplementing the digest comparison.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_INTERVAL_SECONDS = 3600

RUNTIME_DIRNAME = ".claude/runtime"
REVIEW_STATE_FILE = "review_state.json"

SEAL_SCRIPT = "development/scripts/check_governance_seal.py"

SELF_REVIEWS = "current/docs/governance/SELF-REVIEWS.md"
CAMPAIGN_STATUS = "current/docs/governance/CAMPAIGN-STATUS.md"
RELIABILITY_PATTERNS = "current/docs/governance/RELIABILITY-PATTERNS.md"
CURRENT_STATE = "current/docs/governance/CURRENT-STATE.md"

# Governance ledgers stay writable even while a review is overdue -- otherwise
# the agent could not perform the very review that clears the block.
LEDGER_PATHS: frozenset[str] = frozenset(
    {SELF_REVIEWS, CAMPAIGN_STATUS, RELIABILITY_PATTERNS, CURRENT_STATE}
)

# Every field an hourly review must actually contain.  A bare timestamp is not
# a review.
REQUIRED_REVIEW_FIELDS: tuple[str, ...] = (
    "Source fingerprint:",
    "Work completed since prior review:",
    "Evidence that it actually worked:",
    "What went well and why:",
    "What went rough / consumed time or tokens:",
    "Immediate process or technical correction:",
    "Recent fixes reviewed together:",
    "Repeated pattern detected?",
    "Overhardening check:",
    "Next action:",
)

REVIEW_HEADING_PREFIX = "## Review "

# The exact sentence CAMPAIGN-STATUS.md must contain for a voluntary stop to be
# permitted.  Deliberately verbose so it cannot be written by accident.
COMPLETION_SENTINEL = "COMPLETION CONTRACT SATISFIED: Epics 0-7 all acceptance items met."


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------


def project_dir() -> Path:
    """Resolve the repository root, preferring what Claude Code told us."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(out.stdout.strip()).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # .claude/hooks/_governance.py -> repo root
        return Path(__file__).resolve().parents[2]


def runtime_dir(root: Path | None = None) -> Path:
    root = root or project_dir()
    path = root / RUNTIME_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path(root: Path | None = None) -> Path:
    return runtime_dir(root) / REVIEW_STATE_FILE


# --------------------------------------------------------------------------
# Review state
# --------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def default_state(now: int | None = None) -> dict[str, Any]:
    now = _now() if now is None else now
    interval = DEFAULT_INTERVAL_SECONDS
    override = os.environ.get("DISCO_REVIEW_INTERVAL_SECONDS")
    if override and override.isdigit() and int(override) > 0:
        interval = int(override)
    return {
        "interval_seconds": interval,
        "initialized_epoch": now,
        "next_due_epoch": now + interval,
        "last_review_epoch": None,
        "reviews_completed": 0,
    }


def load_state(root: Path | None = None) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        state = default_state()
        save_state(state, root)
        return state
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        state = default_state()
        save_state(state, root)
        return state
    # Repair a truncated/partial state rather than crashing a hook.
    base = default_state()
    for key, value in base.items():
        state.setdefault(key, value)
    return state


def save_state(state: dict[str, Any], root: Path | None = None) -> None:
    """Atomically replace the state file (never a partial read for a peer hook)."""
    path = state_path(root)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".review_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def seconds_until_due(state: dict[str, Any], now: int | None = None) -> int:
    now = _now() if now is None else now
    return int(state.get("next_due_epoch", 0)) - now


def review_is_due(state: dict[str, Any], now: int | None = None) -> bool:
    return seconds_until_due(state, now) <= 0


def advance_review(state: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Atomically record a completed review and schedule the next one."""
    now = _now()
    interval = int(state.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
    state["last_review_epoch"] = now
    state["next_due_epoch"] = now + interval
    state["reviews_completed"] = int(state.get("reviews_completed", 0)) + 1
    save_state(state, root)
    return state


def humanize(seconds: int) -> str:
    seconds = abs(int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# --------------------------------------------------------------------------
# Seal
# --------------------------------------------------------------------------


def _load_seal_module(root: Path):
    spec = importlib.util.spec_from_file_location("_seal", root / SEAL_SCRIPT)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def protected_paths(root: Path | None = None) -> tuple[str, ...]:
    """The sealed set, read from the gate script so there is one definition."""
    root = root or project_dir()
    module = _load_seal_module(root)
    if module is None or not hasattr(module, "PROTECTED"):
        # Fail safe: if we cannot read the set, protect the known names.
        return (
            "current/docs/governance/ENGINEERING-STANDARDS.md",
            "current/docs/governance/ARCHITECTURE-BOUNDARIES.md",
        )
    return tuple(module.PROTECTED)


def verify_seal(root: Path | None = None) -> tuple[int, str]:
    """Run the authoritative gate.  Returns (exit_code, combined_output)."""
    root = root or project_dir()
    script = root / SEAL_SCRIPT
    if not script.is_file():
        return 3, f"{SEAL_SCRIPT} is missing; the seal is not established."
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--quiet"],
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 3, f"could not run {SEAL_SCRIPT}: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# --------------------------------------------------------------------------
# Review content validation
# --------------------------------------------------------------------------


def last_review_block(root: Path | None = None) -> str | None:
    root = root or project_dir()
    path = root / SELF_REVIEWS
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    index = text.rfind(f"\n{REVIEW_HEADING_PREFIX}")
    if index == -1:
        if text.startswith(REVIEW_HEADING_PREFIX):
            return text
        return None
    return text[index + 1 :]


def missing_review_fields(block: str | None) -> list[str]:
    if not block:
        return list(REQUIRED_REVIEW_FIELDS)
    missing: list[str] = []
    for field in REQUIRED_REVIEW_FIELDS:
        position = block.find(field)
        if position == -1:
            missing.append(field)
            continue
        # The field must actually be answered, not merely present as a label.
        tail = block[position + len(field) :]
        line_end = tail.find("\n")
        same_line = tail if line_end == -1 else tail[:line_end]
        next_chunk = tail[line_end + 1 : line_end + 400] if line_end != -1 else ""
        answered = bool(same_line.strip()) or bool(
            [line for line in next_chunk.splitlines() if line.strip() and not line.startswith("#")]
        )
        if not answered:
            missing.append(f"{field} (label present but unanswered)")
    return missing


def status_is_fresh(root: Path | None = None, since_epoch: int | None = None) -> bool:
    """True when the standing ledger was updated at/after the review came due."""
    root = root or project_dir()
    path = root / CAMPAIGN_STATUS
    if not path.is_file():
        return False
    if since_epoch is None:
        return True
    try:
        return path.stat().st_mtime >= since_epoch
    except OSError:
        return False


def _strip_fenced_blocks(text: str) -> str:
    """Drop ``` fenced regions so quoting the sentinel cannot assert it."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return "\n".join(out)


def completion_contract_satisfied(root: Path | None = None) -> bool:
    """True only when the ledger *asserts* completion as a standalone line.

    Deliberately strict.  CAMPAIGN-STATUS.md documents the sentence that must
    eventually appear, and a substring search would treat that documentation as
    the assertion itself -- a false affordance in the very mechanism meant to
    prevent one.  So: fenced code blocks are stripped, indented (quoted) lines
    are ignored, and the remaining line must equal the sentinel exactly.
    """
    root = root or project_dir()
    path = root / CAMPAIGN_STATUS
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in _strip_fenced_blocks(text).splitlines():
        if raw.startswith((" ", "\t", ">")):
            continue  # indented or block-quoted: a citation, not a claim
        if raw.strip() == COMPLETION_SENTINEL:
            return True
    return False


# --------------------------------------------------------------------------
# Hook I/O
# --------------------------------------------------------------------------


def read_hook_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
