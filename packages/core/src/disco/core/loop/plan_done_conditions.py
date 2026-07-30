"""Plan done-condition validation — extracted from ``plans.py``.

Pure, stateless validators that reject predicates which cannot be safely armed as
finish gates before a plan is approved. Each validator returns actionable errors
without touching the loop, emitting events, or reading runtime state.

The three validators (raw, generic, AppKit) are applied in a fixed order by
``validate_plan_conditions`` so a present malformed verifier is never silently
dropped to an implicit omission.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from ..appkit.spec import APPSPEC_RELPATH, DESIGNSPEC_RELPATH
from ..dod import (
    CommandExitPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
    predicate_from_obj,
)
from ..dod_util import _hard_deny_reason
from ..events import PlanEvent
from ..workspace_paths import strip_redundant_workspace_prefix
from .plan_command_validation import (
    _is_concrete_http_hostname,
    _is_loopback_hostname,
    _unsafe_command_done_condition_reason,
)
from .planner_arguments import _unwrap_steps_item_wrapper


def _comparable_workspace_path(path: str) -> PurePosixPath | None:
    """Normalize a model-authored guest path for cross-predicate comparison.

    Traversal/host-absolute paths remain the evaluator's fail-closed concern.  We
    simply exclude them from ancestor comparison rather than accidentally
    normalizing an unsafe shape into a different path.
    """
    clean = strip_redundant_workspace_prefix(path.strip())
    candidate = PurePosixPath(clean)
    if not clean or candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate


def _file_exists_path_error(index: int, predicate: FileExistsPredicate) -> str | None:
    """Return the safety error for one file_exists path, or None when it is safe."""
    comparable = _comparable_workspace_path(predicate.path)
    if comparable is None:
        return (
            f"step {index} file_exists path {predicate.path!r} is not a "
            "safe exact workspace file. The workspace root, directory-shaped "
            "paths, traversal, and host-absolute paths cannot become finish "
            "gates. Name one exact nonempty file under /workspace."
        )
    if predicate.path.rstrip() != predicate.path:
        return (
            f"step {index} file_exists path {predicate.path!r} ends in "
            "whitespace and is unsafe as an approved plan verifier. Name the "
            "exact workspace file or omit the condition."
        )
    if predicate.path.endswith("/"):
        return (
            f"step {index} file_exists path {predicate.path!r} is "
            "directory-shaped, but file_exists requires a nonempty regular "
            "file. Name an exact file inside the directory."
        )
    return None


def _command_condition_error(index: int, predicate: CommandExitPredicate) -> str | None:
    """Return the safety error for one command condition, or None when it is safe."""
    denied = _hard_deny_reason(predicate.cmd)
    if denied is not None:
        return (
            f"step {index} command condition is hard-denied ({denied}) and "
            "therefore cannot become an approved plan verifier. Use a safe "
            "read-only verification command or omit the condition."
        )
    lifecycle_issue = _unsafe_command_done_condition_reason(predicate.cmd)
    if lifecycle_issue is not None:
        return (
            f"step {index} command condition {lifecycle_issue} and therefore "
            "cannot become an approved plan verifier. Command conditions must "
            "be finite, deterministic verification such as `test -f index.html` "
            "or `npm test`; they must not start/background a server or probe a "
            "local/runtime-selected preview URL. Omit this condition and use "
            "`preview_start` plus platform browser verification during execution."
        )
    return None


def _http_ok_condition_error(index: int, predicate: HTTPOkPredicate) -> str | None:
    """Return the safety error for one http_ok condition, or None when it is safe."""
    try:
        parsed = urlsplit(predicate.url)
        hostname = parsed.hostname
    except ValueError:
        parsed = None
        hostname = None
    if parsed is None or parsed.scheme not in {"http", "https"} or not hostname:
        return (
            f"step {index} http_ok URL {predicate.url!r} is not an absolute "
            "HTTP(S) URL and cannot become an approved plan verifier. Correct "
            "the URL or omit the condition."
        )
    if _is_loopback_hostname(hostname):
        return (
            f"step {index} http_ok URL {predicate.url!r} targets a local "
            "preview host. Local preview ports and lifecycle are managed "
            "dynamically by the platform, so this would be an unreliable "
            "plan verifier. Omit this condition and use exact "
            "file_exists deliverables; platform preview verification runs "
            "separately."
        )
    if not _is_concrete_http_hostname(hostname):
        return (
            f"step {index} http_ok URL {predicate.url!r} does not name a "
            "concrete, already-known fully qualified hostname or IP address. "
            "A single-label, future, or placeholder host cannot become an "
            "approved plan verifier. Use the exact external FQDN/IP only when "
            "it is already known, or omit the condition."
        )
    return None


def _file_parent_child_errors(
    files: list[tuple[int, FileExistsPredicate, PurePosixPath]],
) -> list[str]:
    """Reject a file_exists gate that is a parent directory of another file gate."""
    errors: list[str] = []
    for index, predicate, path in files:
        for child_index, child_predicate, child_path in files:
            if index == child_index or path not in child_path.parents:
                continue
            errors.append(
                f"step {index} file_exists path {predicate.path!r} is a parent "
                f"directory of step {child_index} path {child_predicate.path!r}. "
                "file_exists requires a nonempty regular file, so both conditions "
                "cannot be true. Remove the directory condition and keep the exact "
                "nested file deliverable."
            )
            break
    return errors


def validate_plan_done_conditions(plan: PlanEvent) -> list[str]:
    """Return actionable errors for predicates that cannot be safely armed.

    Every accepted predicate becomes a verifier for the approved plan revision.
    Reject deterministic safety/lifecycle contradictions before approval; runtime
    satisfiability remains the bounded evaluator and replan path's responsibility.
    """
    errors: list[str] = []
    files: list[tuple[int, FileExistsPredicate, PurePosixPath]] = []
    for index, step in enumerate(plan.steps, start=1):
        predicate = step.done_condition
        if isinstance(predicate, FileExistsPredicate):
            path_error = _file_exists_path_error(index, predicate)
            if path_error is not None:
                errors.append(path_error)
                continue
            files.append((index, predicate, _comparable_workspace_path(predicate.path)))  # type: ignore[arg-type]
        elif isinstance(predicate, CommandExitPredicate):
            command_error = _command_condition_error(index, predicate)
            if command_error is not None:
                errors.append(command_error)
        elif isinstance(predicate, HTTPOkPredicate):
            http_error = _http_ok_condition_error(index, predicate)
            if http_error is not None:
                errors.append(http_error)
    errors.extend(_file_parent_child_errors(files))
    return errors


_APPKIT_CANONICAL_DOD_FILES = frozenset({APPSPEC_RELPATH, DESIGNSPEC_RELPATH})


def validate_appkit_plan_done_conditions(plan: PlanEvent) -> list[str]:
    """Reject finish gates outside the strict AppKit semantic contract.

    AppKit owns and regenerates its source tree, while ``verify_appkit_app`` owns
    behavioral proof. The only durable files a strict semantic plan may name as
    immutable existence gates are the two canonical specs that drive generation.
    A custom-build widening disables this validator at the caller, restoring the
    ordinary Build done-condition contract.
    """
    errors: list[str] = []
    canonical = ", ".join(sorted(_APPKIT_CANONICAL_DOD_FILES))
    for index, step in enumerate(plan.steps, start=1):
        predicate = step.done_condition
        if predicate is None:
            continue
        if isinstance(predicate, FileExistsPredicate):
            comparable = _comparable_workspace_path(predicate.path)
            if comparable is None:
                # The generic validator already renders the precise safety error.
                continue
            if str(comparable) in _APPKIT_CANONICAL_DOD_FILES:
                continue
            errors.append(
                f"step {index} file_exists path {predicate.path!r} is not a "
                "canonical strict AppKit finish gate. Generated source locations "
                "are owned by AppKit and must not be guessed. Omit the condition "
                f"or name only {canonical}; verify_appkit_app owns behavioral proof."
            )
            continue
        errors.append(
            f"step {index} {predicate.kind} is not a canonical strict AppKit "
            "finish gate. Strict AppKit plans may omit done_condition or use "
            f"file_exists only for {canonical}; verify_appkit_app owns behavioral proof."
        )
    return errors


def validate_raw_plan_done_conditions(arguments: dict) -> list[str]:
    """Reject malformed predicates bypassing the intercepted tool schema.

    ``submit_plan`` never executes as a normal tool in PLANNING, so its Pydantic
    model is not the enforcement boundary.  Parsing remains lenient for display
    compatibility, but approval must not silently drop a present, malformed gate.
    """
    raw_steps = _unwrap_steps_item_wrapper(arguments.get("steps"))
    if not isinstance(raw_steps, list):
        return []
    errors: list[str] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict) or "done_condition" not in raw_step:
            continue
        condition = raw_step.get("done_condition")
        if condition is None:
            continue
        try:
            predicate_from_obj(condition)
        except Exception as exc:  # noqa: BLE001 — rendered as bounded model feedback
            errors.append(
                f"step {index} done_condition is malformed ({type(exc).__name__}). "
                "Correct it to one documented predicate object or omit it; it was "
                "not silently discarded."
            )
    return errors


def validate_plan_conditions(
    arguments: dict,
    plan: PlanEvent,
    strict_appkit_active_reader: Callable[[], bool] | None = None,
) -> list[str]:
    """Apply the identical raw, generic, and AppKit validators to every entry path.

    Raw validation must run before callers trust the lenient plan parser: the
    parser intentionally preserves display compatibility by dropping malformed
    optional values, while approval is fail-closed and must never turn a
    present malformed verifier into an implicit omission.
    """
    errors = validate_raw_plan_done_conditions(arguments)
    errors.extend(validate_plan_done_conditions(plan))
    if strict_appkit_active_reader is None:
        return errors
    try:
        strict_appkit_active = strict_appkit_active_reader()
    except Exception:  # fail closed when the shared lifecycle reader is unavailable
        strict_appkit_active = True
    if strict_appkit_active:
        errors.extend(validate_appkit_plan_done_conditions(plan))
    return errors
