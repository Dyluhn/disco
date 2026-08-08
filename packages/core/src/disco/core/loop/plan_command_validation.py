"""Shell/command classification for plan done-condition validation.

Pure, stateless helpers that tokenize and classify a plan's command done-condition
string so the done-condition validator can reject mutable local preview lifecycles
before a plan is approved. Extracted from ``plans.py`` so each collaborator has one
implementation owner and ``plans.py`` remains a compatibility facade.

Nothing here touches the loop, emits events, or reads runtime state: every
function is a pure projection of a command string to a classification verdict.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from urllib.parse import urlsplit

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

_HTTPIE_METHODS = frozenset(
    {
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
)

# The two done-condition rejection reasons this module authors, extracted from the
# bare `return` statements inside `_probe_target_issue` so a test can DERIVE them
# instead of retyping them (F62 / the Attestation-Binding Invariant, F58). Values
# verbatim; `_probe_target_issue`'s behaviour is unchanged.
_PROBE_RUNTIME_PREVIEW_ISSUE = "probes a runtime-selected preview URL"
_PROBE_LOOPBACK_PREVIEW_ISSUE = "probes a loopback/local preview URL"


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


def _http_client_option_sets(head: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return (long-options-with-values, short-options-with-values) for an HTTP client."""
    if head in {"http", "httpie"}:
        return _HTTPIE_LONG_OPTIONS_WITH_VALUES, _HTTPIE_SHORT_OPTIONS_WITH_VALUES
    if head == "curl":
        return _CURL_LONG_OPTIONS_WITH_VALUES, _CURL_SHORT_OPTIONS_WITH_VALUES
    return _WGET_LONG_OPTIONS_WITH_VALUES, _WGET_SHORT_OPTIONS_WITH_VALUES


def _extract_curl_url_targets(token: str, lowered: str, targets: list[str]) -> bool:
    """Recognize curl's explicit ``--url``/``-url`` operands.

    Returns True when the token was a curl URL-option form (caller skips it).
    """
    if lowered in {"--url", "-url"}:
        return True
    if lowered.startswith("--url="):
        targets.append(token.split("=", 1)[1])
        return True
    return False


def _probe_targets(head: str, args: list[str]) -> list[str]:
    """Extract endpoint operands, excluding headers/output paths and other option values."""
    long_options, short_options = _http_client_option_sets(head)
    remaining = list(args)
    targets: list[str] = []
    while remaining:
        token = remaining.pop(0)
        lowered = token.lower()
        if head == "curl" and _extract_curl_url_targets(token, lowered, targets):
            if lowered in {"--url", "-url"} and remaining:
                targets.append(remaining.pop(0))
            continue
        long_option = lowered.split("=", 1)[0]
        if long_option in long_options or token in short_options:
            if "=" not in token and remaining:
                remaining.pop(0)
            continue
        if token.startswith("-"):
            continue
        targets.append(token)
    if head in {"http", "httpie"} and targets and targets[0].upper() in _HTTPIE_METHODS:
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
        return _PROBE_RUNTIME_PREVIEW_ISSUE
    parsed_target = candidate
    if "://" not in parsed_target:
        parsed_target = f"http://{parsed_target}"
    try:
        hostname = urlsplit(parsed_target).hostname
    except ValueError:
        return None
    if _is_local_preview_hostname(hostname):
        return _PROBE_LOOPBACK_PREVIEW_ISSUE
    return None


def _segment_launches_via_python(head: str, args: list[str], raw_args: list[str]) -> bool:
    """Recognize a Python invocation that starts a local server lifecycle."""
    if not head.startswith("python"):
        return False
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


def _segment_launches_via_package_manager(head: str, raw_args: list[str], depth: int) -> bool:
    """Recognize npm/pnpm/yarn/bun/npx invocations that start a local server."""
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
    return False


def _segment_launches_via_runtime_server(head: str, args: list[str]) -> bool:
    """Recognize flask/django/php and bare server executables that start a server."""
    if head in _SERVER_EXECUTABLES:
        return True
    if head == "flask":
        return "run" in args
    if head in {"django-admin", "django-admin.py", "manage.py"}:
        return "runserver" in args
    if head == "php":
        return "-s" in args
    return False


def _segment_launches_via_build_tool(head: str, args: list[str], raw_args: list[str]) -> bool:
    """Recognize build/dev tool invocations that start a local server.

    Target-neutral: no framework or provider is named here. A tool starts a
    mutable local preview lifecycle when its first non-option operand is a
    server-start verb (a dev/preview/serve/start subcommand).
    """
    if head == "vite":
        vite_args = _discard_option_prefix(raw_args, _VITE_OPTIONS_WITH_VALUES)
        return not vite_args or vite_args[0].lower() != "build"
    if head == "make":
        if any(arg in {"-n", "--dry-run", "--just-print", "--recon"} for arg in args):
            return False
        return any(arg in _SERVER_SCRIPT_NAMES for arg in args if not arg.startswith("-"))
    if head == "docker-compose":
        return "up" in args
    if head == "docker" and "compose" in args:
        return "up" in args[args.index("compose") + 1 :]
    positional = [arg for arg in args if not arg.startswith("-")]
    return bool(positional) and positional[0] in _SERVER_SCRIPT_NAMES


def _segment_launches_via_named_tool(head: str, args: list[str], raw_args: list[str]) -> bool:
    """Recognize single-tool invocations that start a local server lifecycle."""
    if _segment_launches_via_runtime_server(head, args):
        return True
    return _segment_launches_via_build_tool(head, args, raw_args)


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
    if _segment_launches_via_python(head, args, raw_args):
        return True
    if _segment_launches_via_package_manager(head, raw_args, depth):
        return True
    return _segment_launches_via_named_tool(head, args, raw_args)


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
