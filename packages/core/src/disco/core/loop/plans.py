"""Plan/alternatives builders: `submit_plan` → PlanEvent (+ C18 done-condition
harvest) and `ask_user` options → AlternativesEvent.

Extracted from engine.py as a stateful collaborator: `Planner` holds a back-ref
to its `AgentLoop` and writes the loop's per-(revision, index) C18 predicate map.
Bodies are byte-identical to the former AgentLoop methods with `self.` rewritten
to `self._loop.`.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import shlex
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..appkit.spec import APPSPEC_RELPATH, DESIGNSPEC_RELPATH
from ..dod import (
    CommandExitPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
    predicate_from_obj,
)
from ..dod_util import _hard_deny_reason
from ..events import (
    ActionEvent,
    AlternativeOption,
    AlternativesEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
)
from ..view import _latest_plan
from ..workspace_paths import strip_redundant_workspace_prefix
from .messages import (
    _PLAN_EXPLORE_FORCE,
    _PLAN_EXPLORE_READ_CAP,
    _REPLAN_FRAMING,
    _WORKFLOW_ROUTER_EXPLORE_FORCE,
    _render_replan_plan_digest,
)
from .turn_control import _CONTINUE_OPTION_ID

if TYPE_CHECKING:
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

# Plan-field wrapper tags the model sometimes ECHOES into the value it submits.
# `submit_plan` takes plain JSON args, but a model (observed: MiniMax) may leak a
# stray `<summary>` / `</summary>` (or a sibling plan-field tag) into the summary /
# step text — e.g. a summary stored verbatim as "...road plane.</summary>". We strip
# ONLY this closed vocabulary of plan-template tag names (open / close / self-close,
# case-insensitive) so the stored event is clean, while never touching legitimate
# markup the plan itself is about (a plan that mentions `<div>` or `<style>` is safe).
_PLAN_WRAPPER_TAGS = (
    "summary",
    "steps",
    "step",
    "context",
    "rationale",
    "plan",
    "title",
    "detail",
    "description",
)
_PLAN_TAG_RE = re.compile(
    r"</?\s*(?:" + "|".join(_PLAN_WRAPPER_TAGS) + r")\s*/?>",
    re.IGNORECASE,
)
_AUTONOMOUS_ASSUMPTIONS_PREAMBLE = (
    "## Assumptions\n"
    "- Autonomous mode is on, so questions_v2 structured intake was skipped.\n"
    "- Defer-don't-block: ambiguous preferences will be handled with reasonable "
    "defaults and kept easy to revise.\n"
)


def _is_loopback_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _is_local_preview_hostname(hostname: str | None) -> bool:
    """Whether a host can only name a process-local preview endpoint."""
    if _is_loopback_hostname(hostname):
        return True
    if not hostname:
        return False
    try:
        return ipaddress.ip_address(hostname.rstrip(".")).is_unspecified
    except ValueError:
        # A single-label DNS name is local/network-relative just like the
        # single-label hosts rejected for immutable ``http_ok`` predicates.
        return "." not in hostname.rstrip(".")


_COMMAND_SEPARATOR_TOKENS = frozenset({";", "&&", "||", "|", "&"})
_SERVER_SCRIPT_NAMES = frozenset({"dev", "preview", "serve", "server", "start"})
_SERVER_EXECUTABLES = frozenset(
    {
        "daphne",
        "gunicorn",
        "http-server",
        "hypercorn",
        "live-server",
        "serve",
        "streamlit",
        "uvicorn",
    }
)
_PACKAGE_OPTIONS_WITH_VALUES = frozenset(
    {
        "--cwd",
        "--config",
        "--dir",
        "--filter",
        "--prefix",
        "--workspace",
        "-c",
    }
)
_ENV_OPTIONS_WITH_VALUES = frozenset({"--chdir", "--split-string", "--unset", "-c", "-s", "-u"})
_TIMEOUT_OPTIONS_WITH_VALUES = frozenset({"--kill-after", "--signal", "-k", "-s"})
_VITE_OPTIONS_WITH_VALUES = frozenset(
    {"--base", "--config", "--host", "--loglevel", "--mode", "--port", "-c", "-l", "-m"}
)
_CURL_LONG_OPTIONS_WITH_VALUES = frozenset(
    {
        "--cacert",
        "--cert",
        "--connect-timeout",
        "--data",
        "--data-binary",
        "--data-raw",
        "--form",
        "--header",
        "--key",
        "--max-time",
        "--output",
        "--referer",
        "--request",
        "--resolve",
        "--retry",
        "--user",
        "--user-agent",
        "--write-out",
    }
)
_CURL_SHORT_OPTIONS_WITH_VALUES = frozenset(
    {
        "-A",
        "-b",
        "-c",
        "-d",
        "-e",
        "-E",
        "-F",
        "-H",
        "-K",
        "-m",
        "-o",
        "-Q",
        "-r",
        "-T",
        "-u",
        "-w",
        "-x",
        "-X",
        "-Y",
    }
)
_WGET_LONG_OPTIONS_WITH_VALUES = frozenset(
    {
        "--ca-certificate",
        "--header",
        "--output-document",
        "--post-data",
        "--timeout",
        "--user",
        "--user-agent",
    }
)
_WGET_SHORT_OPTIONS_WITH_VALUES = frozenset({"-a", "-o", "-P", "-t", "-T", "-U"})
_HTTPIE_LONG_OPTIONS_WITH_VALUES = frozenset(
    {
        "--auth",
        "--cert",
        "--cert-key",
        "--format-options",
        "--output",
        "--proxy",
        "--session",
        "--session-read-only",
        "--style",
        "--timeout",
        "--verify",
    }
)
_HTTPIE_SHORT_OPTIONS_WITH_VALUES = frozenset({"-a", "-o"})


def _shell_tokens(command: str) -> list[str] | None:
    """Tokenize enough shell syntax to identify lifecycle-bearing plan gates."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _command_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _COMMAND_SEPARATOR_TOKENS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1].lower()


def _discard_option_prefix(words: list[str], options_with_values: frozenset[str]) -> list[str]:
    """Drop leading CLI options while respecting options that consume a value."""
    remaining = list(words)
    while remaining:
        token = remaining[0].lower()
        if token == "--":
            return remaining[1:]
        option = token.split("=", 1)[0]
        if option in options_with_values:
            consumed = 1 if "=" in token else 2
            remaining = remaining[consumed:]
            continue
        if token.startswith("-"):
            remaining.pop(0)
            continue
        break
    return remaining


def _shell_nested_command(words: list[str]) -> str | None:
    """Return the command string passed through a shell's short ``-c`` options."""
    if not words or _basename(words[0]) not in {"bash", "dash", "sh", "zsh"}:
        return None
    index = 1
    while index < len(words):
        option = words[index]
        if option == "--":
            return None
        if option.startswith("-") and not option.startswith("--") and "c" in option[1:]:
            return words[index + 1] if index + 1 < len(words) else None
        if option in {"-o", "+o"}:
            index += 2
            continue
        if not option.startswith(("-", "+")):
            return None
        index += 1
    return None


def _tokens_start_background_work(tokens: list[str], *, depth: int = 0) -> bool:
    """Detect background operators, including inside a bounded shell wrapper."""
    if "&" in tokens:
        return True
    if depth >= 2:
        return False
    for segment in _command_segments(tokens):
        words = _strip_command_prefix(segment)
        nested_command = _shell_nested_command(words)
        nested = _shell_tokens(nested_command) if nested_command is not None else None
        if nested and _tokens_start_background_work(nested, depth=depth + 1):
            return True
    return False


def _strip_command_prefix(words: list[str]) -> list[str]:
    """Remove common execution wrappers before classifying a command segment."""
    remaining = list(words)
    while remaining and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", remaining[0]):
        remaining.pop(0)
    while remaining:
        head = _basename(remaining[0])
        if head in {"command", "exec", "nohup"}:
            remaining.pop(0)
            if remaining[:1] == ["--"]:
                remaining.pop(0)
            continue
        if head == "env":
            remaining.pop(0)
            remaining = _discard_option_prefix(remaining, _ENV_OPTIONS_WITH_VALUES)
            while remaining and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", remaining[0]):
                remaining.pop(0)
            continue
        if head == "timeout":
            remaining.pop(0)
            remaining = _discard_option_prefix(remaining, _TIMEOUT_OPTIONS_WITH_VALUES)
            if remaining:  # duration
                remaining.pop(0)
            continue
        break
    return remaining


def _package_command(words: list[str]) -> tuple[str | None, list[str]]:
    """Return a package-manager verb and the raw arguments following it."""
    remaining = _discard_option_prefix(words, _PACKAGE_OPTIONS_WITH_VALUES)
    if not remaining:
        return None, []
    return remaining[0].lower(), remaining[1:]


def _probe_targets(head: str, args: list[str]) -> list[str]:
    """Extract endpoint operands, excluding headers/output paths and other option values."""
    if head in {"http", "httpie"}:
        long_options = _HTTPIE_LONG_OPTIONS_WITH_VALUES
        short_options = _HTTPIE_SHORT_OPTIONS_WITH_VALUES
    elif head == "curl":
        long_options = _CURL_LONG_OPTIONS_WITH_VALUES
        short_options = _CURL_SHORT_OPTIONS_WITH_VALUES
    else:
        long_options = _WGET_LONG_OPTIONS_WITH_VALUES
        short_options = _WGET_SHORT_OPTIONS_WITH_VALUES
    remaining = list(args)
    targets: list[str] = []
    while remaining:
        token = remaining.pop(0)
        lowered = token.lower()
        if head == "curl" and lowered in {"--url", "-url"}:
            if remaining:
                targets.append(remaining.pop(0))
            continue
        if head == "curl" and lowered.startswith("--url="):
            targets.append(token.split("=", 1)[1])
            continue
        long_option = lowered.split("=", 1)[0]
        if long_option in long_options or token in short_options:
            if "=" not in token and remaining:
                remaining.pop(0)
            continue
        if token.startswith("-"):
            continue
        targets.append(token)
    if (
        head in {"http", "httpie"}
        and targets
        and targets[0].upper()
        in {
            "CONNECT",
            "DELETE",
            "GET",
            "HEAD",
            "OPTIONS",
            "PATCH",
            "POST",
            "PUT",
            "TRACE",
        }
    ):
        targets.pop(0)
    if head in {"http", "httpie"}:
        return targets[:1]
    return targets


def _probe_target_issue(target: str) -> str | None:
    """Classify one shlex-normalized HTTP-client endpoint operand."""
    candidate = target.strip().rstrip(".,);")
    if not candidate:
        return None
    if any(marker in candidate for marker in ("$", "`", "<", ">", "{", "}")):
        return "probes a runtime-selected preview URL"
    parsed_target = candidate
    if "://" not in parsed_target:
        parsed_target = f"http://{parsed_target}"
    try:
        hostname = urlsplit(parsed_target).hostname
    except ValueError:
        return None
    if _is_local_preview_hostname(hostname):
        return "probes a loopback/local preview URL"
    return None


def _segment_launches_local_server(words: list[str], *, depth: int = 0) -> bool:
    """Recognize finite command gates that actually start a server lifecycle."""
    words = _strip_command_prefix(words)
    if not words:
        return False
    head = _basename(words[0])
    raw_args = words[1:]
    args = [arg.lower() for arg in raw_args]

    if head in {"bash", "dash", "sh", "zsh"} and depth < 2:
        nested_command = _shell_nested_command(words)
        nested = _shell_tokens(nested_command) if nested_command is not None else None
        return bool(
            nested
            and any(
                _segment_launches_local_server(segment, depth=depth + 1)
                for segment in _command_segments(nested)
            )
        )
    if head.startswith("python"):
        if "-m" in args:
            module_index = args.index("-m")
            module = args[module_index + 1] if module_index + 1 < len(args) else ""
            module_args = args[module_index + 2 :]
            if module in {"http.server", "uvicorn"}:
                return True
            if module == "flask" and "run" in module_args:
                return True
        scripts = [arg for arg in args if not arg.startswith("-")]
        return bool(scripts and _basename(scripts[0]) == "manage.py" and "runserver" in scripts[1:])
    if head in _SERVER_EXECUTABLES:
        return True
    if head == "flask":
        return "run" in args
    if head in {"django-admin", "django-admin.py", "manage.py"}:
        return "runserver" in args
    if head == "php":
        return "-s" in args
    if head == "vite":
        vite_args = _discard_option_prefix(raw_args, _VITE_OPTIONS_WITH_VALUES)
        return not vite_args or vite_args[0].lower() != "build"
    if head == "next":
        next_args = [arg for arg in args if not arg.startswith("-")]
        return not next_args or next_args[0] not in {"build", "info", "telemetry"}
    if head in {"npm", "pnpm", "yarn", "bun"}:
        verb, following = _package_command(raw_args)
        if verb in {"exec", "x", "dlx"}:
            inner = following[1:] if following[:1] == ["--"] else following
            inner = _discard_option_prefix(inner, _PACKAGE_OPTIONS_WITH_VALUES)
            return _segment_launches_local_server(inner, depth=depth + 1)
        if verb in {"run", "run-script"}:
            following = _discard_option_prefix(following, _PACKAGE_OPTIONS_WITH_VALUES)
            script = following[0].lower() if following else None
            return script in _SERVER_SCRIPT_NAMES
        return verb in _SERVER_SCRIPT_NAMES
    if head in {"npx", "pnpx", "bunx"}:
        inner = _discard_option_prefix(raw_args, _PACKAGE_OPTIONS_WITH_VALUES)
        return bool(inner) and _segment_launches_local_server(inner, depth=depth + 1)
    if head == "make":
        if any(arg in {"-n", "--dry-run", "--just-print", "--recon"} for arg in args):
            return False
        return any(arg in _SERVER_SCRIPT_NAMES for arg in args if not arg.startswith("-"))
    if head == "docker-compose":
        return "up" in args
    if head == "docker" and "compose" in args:
        return "up" in args[args.index("compose") + 1 :]
    return False


def _segment_probe_issue(words: list[str], *, depth: int = 0) -> str | None:
    """Return a local/dynamic endpoint issue for a real HTTP-client invocation."""
    words = _strip_command_prefix(words)
    if not words:
        return None
    head = _basename(words[0])
    if head in {"bash", "dash", "sh", "zsh"} and depth < 2:
        nested_command = _shell_nested_command(words)
        nested = _shell_tokens(nested_command) if nested_command is not None else None
        if nested:
            for segment in _command_segments(nested):
                issue = _segment_probe_issue(segment, depth=depth + 1)
                if issue is not None:
                    return issue
        return None
    if head not in {"curl", "http", "httpie", "wget"}:
        return None
    targets = _probe_targets(head, words[1:])
    for target in targets:
        issue = _probe_target_issue(target)
        if issue is not None:
            return issue
    return None


def _unsafe_command_done_condition_reason(command: str) -> str | None:
    """Reject commands whose truth depends on a mutable local preview lifecycle."""
    tokens = _shell_tokens(command)
    if tokens is None:
        return "cannot be parsed as a finite shell verification command"
    if _tokens_start_background_work(tokens):
        return "starts background work"
    for segment in _command_segments(tokens):
        probe_issue = _segment_probe_issue(segment)
        if probe_issue is not None:
            return probe_issue
        if _segment_launches_local_server(segment):
            return "launches a local server"
    return None


def _is_concrete_http_hostname(hostname: str | None) -> bool:
    """Whether an immutable HTTP gate names a concrete FQDN or IP address.

    Single-label names are commonly model-authored lifecycle placeholders (the
    observed live failure used ``should-be-verified-later``). They may also depend
    on private search-domain state that the immutable evaluator cannot establish.
    IP literals remain valid, as do DNS names with at least two RFC-compatible
    labels. IDNA conversion keeps legitimate internationalized hostnames usable.
    """
    if not hostname:
        return False
    candidate = hostname.rstrip(".")
    if not candidate:
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        try:
            ascii_hostname = candidate.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        labels = ascii_hostname.split(".")
        if len(labels) < 2 or len(ascii_hostname) > 253:
            return False
        return all(
            label
            and len(label) <= 63
            and label[0].isalnum()
            and label[-1].isalnum()
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
    else:
        return True


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
            comparable = _comparable_workspace_path(predicate.path)
            if comparable is None:
                errors.append(
                    f"step {index} file_exists path {predicate.path!r} is not a "
                    "safe exact workspace file. The workspace root, directory-shaped "
                    "paths, traversal, and host-absolute paths cannot become finish "
                    "gates. Name one exact nonempty file under /workspace."
                )
            elif predicate.path.rstrip() != predicate.path:
                errors.append(
                    f"step {index} file_exists path {predicate.path!r} ends in "
                    "whitespace and is unsafe as an approved plan verifier. Name the "
                    "exact workspace file or omit the condition."
                )
            elif predicate.path.endswith("/"):
                errors.append(
                    f"step {index} file_exists path {predicate.path!r} is "
                    "directory-shaped, but file_exists requires a nonempty regular "
                    "file. Name an exact file inside the directory."
                )
            else:
                files.append((index, predicate, comparable))
        elif isinstance(predicate, CommandExitPredicate):
            denied = _hard_deny_reason(predicate.cmd)
            if denied is not None:
                errors.append(
                    f"step {index} command condition is hard-denied ({denied}) and "
                    "therefore cannot become an approved plan verifier. Use a safe "
                    "read-only verification command or omit the condition."
                )
                continue
            lifecycle_issue = _unsafe_command_done_condition_reason(predicate.cmd)
            if lifecycle_issue is not None:
                errors.append(
                    f"step {index} command condition {lifecycle_issue} and therefore "
                    "cannot become an approved plan verifier. Command conditions must "
                    "be finite, deterministic verification such as `test -f index.html` "
                    "or `npm test`; they must not start/background a server or probe a "
                    "local/runtime-selected preview URL. Omit this condition and use "
                    "`preview_start` plus platform browser verification during execution."
                )
        elif isinstance(predicate, HTTPOkPredicate):
            try:
                parsed = urlsplit(predicate.url)
                hostname = parsed.hostname
            except ValueError:
                parsed = None
                hostname = None
            if parsed is None or parsed.scheme not in {"http", "https"} or not hostname:
                errors.append(
                    f"step {index} http_ok URL {predicate.url!r} is not an absolute "
                    "HTTP(S) URL and cannot become an approved plan verifier. Correct "
                    "the URL or omit the condition."
                )
            elif _is_loopback_hostname(hostname):
                errors.append(
                    f"step {index} http_ok URL {predicate.url!r} targets a local "
                    "preview host. Local preview ports and lifecycle are managed "
                    "dynamically by the platform, so this would be an unreliable "
                    "plan verifier. Omit this condition and use exact "
                    "file_exists deliverables; platform preview verification runs "
                    "separately."
                )
            elif not _is_concrete_http_hostname(hostname):
                errors.append(
                    f"step {index} http_ok URL {predicate.url!r} does not name a "
                    "concrete, already-known fully qualified hostname or IP address. "
                    "A single-label, future, or placeholder host cannot become an "
                    "approved plan verifier. Use the exact external FQDN/IP only when "
                    "it is already known, or omit the condition."
                )

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


def _strip_plan_tags(text: object | None) -> str:
    """Defensively remove leaked plan-template wrapper tags from a submitted plan
    field (summary / step title / detail / context). Idempotent: a clean field is
    returned unchanged (modulo surrounding whitespace).

    Accepts `object | None` and coerces to `str` AFTER the falsy check: a model
    that submits a non-string value (int / list / dict) for one of these fields
    must NOT crash the plan parse — it is stringified exactly as the old
    `str(...)`-coercing code did, then tag-stripped."""
    if not text:
        return ""
    return _PLAN_TAG_RE.sub("", str(text)).strip()


def _with_autonomous_assumptions(context: str) -> str:
    if "questions_v2 structured intake was skipped" in context:
        return context
    if context:
        return f"{_AUTONOMOUS_ASSUMPTIONS_PREAMBLE}\n{context}"
    return _AUTONOMOUS_ASSUMPTIONS_PREAMBLE.strip()


def _coerce_step(s: object) -> PlanStep | None:
    """Coerce one raw submit_plan step element into a PlanStep (or None if it has no title).
    Shared by the normal `steps[]` path and the A3 harvest so both apply identical lenient
    shape handling (dict with title/step/name + optional detail + optional done_condition; or a
    bare string title)."""
    if isinstance(s, dict):
        title = _strip_plan_tags(s.get("title") or s.get("step") or s.get("name"))
        detail = _strip_plan_tags(s.get("detail") or s.get("description")) or None
        cond = s.get("done_condition")
        dc = None
        if isinstance(cond, dict):
            try:
                dc = predicate_from_obj(cond)
            except Exception:  # noqa: BLE001 — malformed predicate is advisory-only
                dc = None
        if title:
            return PlanStep(title=title, detail=detail, done_condition=dc)
    elif isinstance(s, str):
        title = _strip_plan_tags(s)
        if title:
            return PlanStep(title=title)
    return None


# [REL-RC A3] Recovery for MiniMax-M3's nested-array tool-arg SERIALIZATION failure: the model
# authors correct structured steps but routes the whole {steps:[{title,…}],…} object STRINGIFIED
# into the wrong submit_plan parameter, leaving the real `steps` kwarg empty (live-proven:
# revise_after_finish run001 rev4). We recover the step titles WITHOUT invoking any parser/eval —
# a bounded, non-backtracking regex over a size-capped input (Codex-gated x5: ast.literal_eval has
# unbounded parser-stack-overflow surface via in-string brackets and unbracketed unary nesting; a
# regex has none). Strictly gated on `not steps` by the caller, so worst case (0 matches) is
# exactly today's behavior — it can never regress a plan that already parsed steps.
_MAX_PLAN_BLOB_BYTES = 65_536  # size pre-cap before any scan
_MAX_HARVESTED_STEPS = 64  # post-match coercion cap
# Non-backtracking + bounded: a `title` key (single or double quoted) → its quoted string value,
# capped at 200 chars. Single-quoted value = no inner single quotes; double-quoted = escapes ok.
# Bounded char classes with no nested/overlapping quantifiers → linear time, no ReDoS.
_TITLE_HARVEST_RE = re.compile(
    r"""(?:'title'|"title")\s*:\s*(?:'([^']{1,200})'|"((?:[^"\\]|\\.){1,200})")"""
)
# A `steps` key marker — required before harvesting from summary/context/rationale so an incidental
# `title:` outside a mis-routed steps payload can NEVER fabricate a step (Codex r5 fail-closed).
_STEPS_ENVELOPE_RE = re.compile(r"""(?:'steps'|"steps")\s*:""")


def _harvest_steps(arguments: dict) -> list[PlanStep]:
    """[REL-RC A3] Recover step titles the model mis-routed into the wrong submit_plan parameter.
    The `steps` param when it arrived AS A STRING is harvested directly (a title there is a step by
    definition); summary/context/rationale are harvested ONLY past a `steps:` envelope. No parser
    is ever invoked. Returns [] if nothing is safely recoverable."""
    out: list[PlanStep] = []
    candidates: list[tuple[str, bool]] = []  # (blob, require_steps_envelope)
    raw_steps = arguments.get("steps")
    if isinstance(raw_steps, str):
        candidates.append((raw_steps, False))  # the steps slot itself — harvest directly
    for key in ("summary", "context", "rationale"):
        v = arguments.get(key)
        if isinstance(v, str):
            candidates.append((v, True))  # require a steps: envelope (no false positives)
    for blob, require_envelope in candidates:
        if len(blob) > _MAX_PLAN_BLOB_BYTES:
            continue
        start = 0
        if require_envelope:
            env = _STEPS_ENVELOPE_RE.search(blob)
            if env is None:
                continue
            start = env.end()
        for mt in _TITLE_HARVEST_RE.finditer(blob, start):
            title = _strip_plan_tags(mt.group(1) or mt.group(2))
            if title:
                out.append(PlanStep(title=title))
            if len(out) >= _MAX_HARVESTED_STEPS:
                break
        if out:
            break  # first blob that yields a real step wins
    return out


def _unwrap_steps_item_wrapper(value: object) -> object:
    if not isinstance(value, dict) or len(value) != 1:
        return value
    key, wrapped = next(iter(value.items()))
    if key in {"item", "items"} and isinstance(wrapped, list):
        return wrapped
    return value


class Planner:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    def discard_plan_predicates(self, revision: int) -> None:
        """Remove transient C18 entries for a plan rejected before persistence."""
        predicates = getattr(self._loop, "_plan_step_predicates", None)
        if not isinstance(predicates, dict):
            return
        for key in tuple(predicates):
            if key[0] == revision:
                predicates.pop(key, None)

    async def note_planning_read_and_maybe_force(self) -> None:
        """(B2/B6) Count one consecutive PLANNING-mode read and, on CROSSING the
        cap, inject ONE forcing reminder ("you have enough context — submit_plan
        now"). Append-once: it fires only at the exact threshold, never on every
        subsequent read. The counter is reset on a submit_plan and on
        (re-)entering planning, so legitimate Phase-1 gathering below the cap is
        unchanged. Without this a re-plan model in an execution frame of mind
        reads indefinitely and never proposes a plan (the revise spinner hangs
        forever — B2 — and it free-builds without re-planning — B6)."""
        self._loop._plan_explore_reads += 1
        if self._loop._plan_explore_reads == _PLAN_EXPLORE_READ_CAP:
            content = (
                _WORKFLOW_ROUTER_EXPLORE_FORCE
                if self._loop._workflow_router_phase_active()
                else _PLAN_EXPLORE_FORCE.format(n=self._loop._plan_explore_reads)
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=content),
                )
            )

    async def emit_replan_framing_if_revision(self, text: str) -> None:
        """(B2/B6) When RE-entering planning after a build was already approved
        (a revision — a prior `plan_approved` StatusEvent exists) AND the user
        gave a concrete new instruction, emit ONE RE-PLANNING framing reminder so
        the model proposes a revised plan instead of free-building against the
        old plan. A first plan (no prior approval) gets nothing — unchanged.

        Safe to call AFTER enter_planning has emitted the new user turn + the
        `planning` status: neither adds a `plan_approved` status, so the revision
        check is identical whether computed before or after them."""
        if not text.strip():
            return
        events = await self._loop._events()
        is_revision = any(
            isinstance(e, StatusEvent) and e.detail == "plan_approved" for e in events
        )
        if is_revision:
            # Thread the CURRENT approved plan + the new instruction INLINE into the
            # framing so the model revises the actual plan (and emits submit_plan) instead
            # of reconstructing it from a long post-build history and narrating it in prose
            # (the intermittent NO_REPLAN narrate-without-submit residual).
            plan = _latest_plan(events)
            digest = (
                _render_replan_plan_digest(plan)
                if plan is not None and getattr(plan, "steps", None)
                else "  (the prior plan's steps are not on record — restate the full plan)"
            )
            content = _REPLAN_FRAMING.format(current_plan=digest, instruction=text.strip())
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=content),
                )
            )

    def plan_from_args(self, arguments: dict, events: list[Event]) -> PlanEvent:
        """Build a PlanEvent from a `submit_plan` tool call. Defensive against the
        model's shape drift (steps as dicts or bare strings); revision counts prior
        plans so a re-plan is visibly the next iteration. `context` carries any
        markdown rationale / exploration findings the planner included.

        C18 — also harvest each step's optional `done_condition` (an advisory
        DoDPredicate) into `self._plan_step_predicates` keyed by
        (revision, 1-based index). The predicate is later evaluated when the
        agent emits `plan_step(idx, 'done')`. Malformed predicates (wrong
        `kind`, missing fields, non-dict) are silently dropped — the step
        degrades to "no predicate" exactly like a step that omitted the
        field in the first place. C18 is advisory, not a gate, so a bad
        predicate never blocks the plan from being approved."""
        steps: list[PlanStep] = []
        # Re-seed per-plan-revision (a re-plan supersedes the prior map; we
        # don't keep stale predicates from an obsolete revision).
        revision = 1 + sum(1 for e in events if isinstance(e, PlanEvent))
        # A rejected submit_plan is allowed to recover with a corrected plan at
        # the same revision. Clear its transient advisory map before rebuilding,
        # otherwise an omitted condition from the rejected attempt survives and
        # can be evaluated against the corrected plan.
        self.discard_plan_predicates(revision)
        # [REL-RC A3] Only ITERATE `steps` when it is a real list. A model that mis-routes the
        # step array as a STRING into the `steps` slot would otherwise char-iterate ("abc" →
        # 'a','b','c' → bogus single-char steps); that string is instead routed to _harvest_steps
        # below. done_condition is parsed ONTO each PlanStep via _coerce_step (resume-durable).
        _raw_steps = _unwrap_steps_item_wrapper(arguments.get("steps"))
        if isinstance(_raw_steps, list):
            for s in _raw_steps:
                step = _coerce_step(s)
                if step is not None:
                    steps.append(step)
        # [REL-RC A3] If no steps parsed, attempt the bounded regex-harvest recovery for steps the
        # model serialized into the wrong submit_plan parameter (no parser invoked; fail-closed).
        if not steps:
            steps.extend(_harvest_steps(arguments))
        # When the model submits a summary but NO real steps, keep `steps` EMPTY
        # rather than inserting a fake "(the planner returned no concrete steps)"
        # placeholder — that rendered as a broken numbered "1." step in the UI.
        # Every plan consumer already guards `not plan.steps` (the honest no-steps
        # state), so the UI shows a clean summary-only plan card instead.
        summary = _strip_plan_tags(arguments.get("summary")) or "Proposed plan"
        context = _strip_plan_tags(arguments.get("context") or arguments.get("rationale"))
        if getattr(self._loop, "_autonomous", False):
            context = _with_autonomous_assumptions(context)
        # C18 — harvest the predicates (revision-scoped). We walk the raw
        # args (not the rebuilt `steps`) so we can preserve the 1-based
        # step index even when the title/format was leniently coerced.
        # [REL-RC A3] guard: only a real list carries indexed predicates (a string never does).
        _rs = _unwrap_steps_item_wrapper(arguments.get("steps"))
        raw_steps = _rs if isinstance(_rs, list) else []
        for one_based, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, dict):
                continue
            cond = raw.get("done_condition")
            if not isinstance(cond, dict):
                # Optional field, absent by default — back-compat: steps
                # without a predicate behave exactly as today.
                continue
            try:
                predicate = predicate_from_obj(cond)
            except Exception:  # noqa: BLE001 — malformed predicate is advisory-only
                # A bad shape is logged once at WARNING (not ERROR — a
                # broken advisory note is not a run failure) and the
                # step silently drops the predicate.
                _LOG.warning(
                    "C18: malformed done_condition on plan step %d (revision %d); "
                    "ignoring (advisory only): %r",
                    one_based,
                    revision,
                    cond,
                )
                continue
            self._loop._plan_step_predicates[(revision, one_based)] = predicate
        return PlanEvent(summary=summary, steps=steps, revision=revision, context=context)

    def alternatives_from_args(
        self, arguments: dict, events: list[Event]
    ) -> AlternativesEvent | None:
        """Build an AlternativesEvent from an `ask_user` tool call's options.
        Defensive against the model's shape drift — options may come as dicts
        with various key names. Correlates with the most recent ActionEvent so
        the UI can show which step the alternatives are answering.

        Returns None when the args are too malformed to produce a useful gate;
        the loop falls back to a free-form question (see the ASK-USER GATE)."""
        raw_options = arguments.get("options") or arguments.get("alternatives") or []
        options: list[AlternativeOption] = []
        for i, opt in enumerate(raw_options):
            # BW-03 — DECOUPLE "labeled choice" from "runnable recovery pick".
            # An option does NOT need a tool_name to be a valid choice: the model
            # often enumerates plain-language paths ("Use approach A") with no
            # tool call. Those still become clickable cards — the pick is recorded
            # as a user message that steers the agent — so we must NOT drop them.
            # A `tool_name`, when present, additionally makes the pick directly
            # runnable; its absence just means "label-only".
            if isinstance(opt, str):
                # A bare string IS the choice label (no tool, no description).
                label = opt.strip()
                if not label:
                    continue
                options.append(
                    AlternativeOption(
                        id=f"opt_{i + 1}",
                        title=label,
                        description="",
                        tool_name="",
                        arguments={},
                    )
                )
                continue
            if not isinstance(opt, dict):
                continue
            tool_name = str(
                opt.get("tool_name")
                or (opt.get("tool_call") or {}).get("name")
                or (opt.get("tool_call") or {}).get("tool_name")
                or ""
            ).strip()
            args = opt.get("arguments") or (opt.get("tool_call") or {}).get("arguments") or {}
            if not isinstance(args, dict):
                # Malformed args shouldn't sink the whole option — drop the args,
                # keep the labeled choice. (The executor revalidates args anyway.)
                args = {}
            description = str(opt.get("description") or opt.get("why") or "").strip()
            # Broaden label extraction and NEVER fall back to a content-free
            # "Option N": derive a real label from any human-readable field, and
            # only as a last resort name the tool the pick will run. If none of
            # those exist the option carries no information to show — skip it
            # rather than render a meaningless button.
            title = str(
                opt.get("title") or opt.get("label") or opt.get("name") or description or tool_name
            ).strip()
            if not title:
                continue
            option_id = str(opt.get("id") or f"opt_{i + 1}")
            options.append(
                AlternativeOption(
                    id=option_id,
                    title=title,
                    description=description,
                    tool_name=tool_name,
                    arguments=args,
                )
            )
        # `ask_user` semantics: the question text is the primary signal; options
        # are optional clickable alternatives. With no options, the loop emits
        # the question as a model message instead of building an AlternativesEvent
        # (handled by the caller — see the ASK-USER GATE).
        if not options:
            return None
        summary = str(
            arguments.get("question") or arguments.get("summary") or arguments.get("goal") or ""
        ).strip()
        if not summary:
            summary = "the agent is asking which path to take"
        # Correlate with the most recent failed action (the immediate predecessor).
        failed_action_id = ""
        for e in reversed(events):
            if isinstance(e, ActionEvent):
                failed_action_id = e.id
                break
        # D2 — always append the "Continue anyway" bypass so the user is never trapped
        # at a decision gate: pick it to reset the failure streak and let the agent
        # keep going with its own judgment (pick_alternative special-cases this id).
        options = [
            *options,
            AlternativeOption(
                id=_CONTINUE_OPTION_ID,
                title="Continue anyway",
                description="Let the agent keep working with its own best next step.",
                tool_name="",  # not a tool — the loop intercepts this id
                arguments={},
            ),
        ]
        return AlternativesEvent(
            failed_action_id=failed_action_id,
            summary=summary,
            options=options,
        )
