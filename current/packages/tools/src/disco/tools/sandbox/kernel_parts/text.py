"""Transport-agnostic kernel helpers: bounded capture, sanitizing, tokens.

No kernel state lives here — every function takes exactly the values it
needs. `ProcessKernel` and `GatewayKernel` both compose `_BoundedTextCapture`
for stdout/stderr/traceback accumulation, and the gateway auth/diagnostic
helpers are shared by the (single) gateway transport and its tests.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets as _secrets
from pathlib import Path

_LOG = logging.getLogger(__name__)

# Dev fallback for the kernel-gateway auth token when no app secret is set —
# a process-local random key (stable for this process's lifetime).
_DEV_GATEWAY_SECRET = _secrets.token_bytes(32)

_GATEWAY_DIAGNOSTIC_MAX_CHARS = 1200
_GATEWAY_SECRET_RE = re.compile(
    r"(?i)\b(?:kg_auth_token|authorization|api[_-]?key|token|secret)\b"
    r"(?:\s*[:=]\s*|\s+)(?:bearer\s+|token\s+)?[^\s;]+"
)

# C15: idle-cull knob. Default 300s (5 min) — long enough that an agent thinking
# between tool calls doesn't trigger churn, short enough to free a forgotten
# kernel in a quiet conversation. Set to 0 to disable culling entirely.
_DEFAULT_IDLE_TIMEOUT_S = 300.0
_IDLE_TIMEOUT_ENV = "DISCO_KERNEL_IDLE_TIMEOUT_S"

# ANSI escape sequence regex for stripping colors from tracebacks
_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_KERNEL_STREAM_HEAD = 16 * 1024
_KERNEL_STREAM_TAIL = 48 * 1024
_KERNEL_STREAM_CAP = _KERNEL_STREAM_HEAD + _KERNEL_STREAM_TAIL
_KERNEL_IMAGE_MAX_BYTES = 8 * 1024 * 1024


def _bounded_gateway_diagnostic(
    output: object,
    *,
    exit_code: object = None,
    auth_token: str = "",
) -> str:
    """Return bounded startup evidence without retaining gateway credentials.

    Shell-session output can include the echoed ``KG_AUTH_TOKEN=...`` launch
    assignment, arbitrary terminal controls, or a very large traceback.  Kernel
    readiness failures are durable AgentError evidence, so sanitize and bound the
    text before it crosses that boundary.  Raw exception strings are intentionally
    excluded by callers; transport failures are represented by their class only.
    """

    text = str(output or "")
    if auth_token:
        text = text.replace(auth_token, "<redacted>")
    text = _GATEWAY_SECRET_RE.sub("<redacted>", text)
    text = "".join(
        char for char in text if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127)
    ).strip()
    if len(text) > _GATEWAY_DIAGNOSTIC_MAX_CHARS:
        text = "…" + text[-(_GATEWAY_DIAGNOSTIC_MAX_CHARS - 1) :]
    prefix = f"exit={exit_code}; " if exit_code is not None else ""
    return (prefix + text).strip()[: _GATEWAY_DIAGNOSTIC_MAX_CHARS + len(prefix)]


def _gateway_transport_state(exc: BaseException) -> str:
    """Class-only transport evidence: actionable category without raw URL/detail."""

    name = type(exc).__name__
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
        name = "RequestError"
    return f"transport={name}"


def _gateway_auth_token(sandbox_id: str) -> str:
    """The kernel gateway's auth token for one sandbox. SECURITY: the gateway
    (`jupyter kernelgateway`) is an arbitrary-code-execution endpoint; without a
    token any caller that can reach its port can drive a kernel. The token is
    DERIVED, not stored, so it is STABLE and re-derivable: `_ensure_gateway`
    lazily REUSES a surviving gateway across suspend/resume / agent-server
    restart, and a fresh `GatewayKernel` must reconnect with the SAME token (a
    per-object random token would lock the object out of its own kernel).

    HMAC keyed on `DISCO_SECRET_KEY` so the value is secret from other sandboxes
    (interiors never receive the master key — SEC-1) and from the network; a
    process-local random key is the dev fallback when no app secret is set. The
    token is observable only from inside this sandbox (the gateway runs in its
    own container), which is the same trust boundary it protects."""
    from disco.core.env import disco_env

    master = disco_env("SECRET_KEY")
    key = master.encode() if master else _DEV_GATEWAY_SECRET
    return hmac.new(key, f"kernel-gateway:{sandbox_id}".encode(), hashlib.sha256).hexdigest()


def _default_idle_timeout_s() -> float:
    """Read `DISCO_KERNEL_IDLE_TIMEOUT_S`; missing/garbage -> 300.0; clamped to >=0.
    Read at call-time so tests can monkeypatch the env var per-case without
    process-level state."""
    raw = os.environ.get(_IDLE_TIMEOUT_ENV)
    if raw is None or raw.strip() == "":
        return _DEFAULT_IDLE_TIMEOUT_S
    try:
        v = float(raw)
    except ValueError:
        _LOG.warning(
            "%s=%r is not a float; using default %.0fs",
            _IDLE_TIMEOUT_ENV,
            raw,
            _DEFAULT_IDLE_TIMEOUT_S,
        )
        return _DEFAULT_IDLE_TIMEOUT_S
    return max(0.0, v)


def _rewrite_process_workspace_literals(code: str, workspace: Path) -> str:
    """Translate Python string literals rooted at the guest ``/workspace`` path.

    Container kernels have a real ``/workspace`` mount.  The dev-only process
    kernel instead runs directly in its per-conversation host directory, so a
    literal path that is valid in every sibling tool otherwise raises
    ``FileNotFoundError``.  Rewrite only Python string constants whose complete
    prefix is exactly ``/workspace``; relative paths and strings containing the
    word elsewhere are untouched.

    The resolved target is jailed before it is inserted.  This does not turn the
    process backend into an isolation boundary (model code already executes as a
    host process), but the compatibility layer must never manufacture an escape
    path such as ``/workspace/../../etc`` itself.

    Cells using IPython-only syntax are left byte-for-byte unchanged when Python's
    AST parser cannot parse them.  Normal Python cells -- including f-strings --
    take the strict translated path.
    """
    import ast

    from ..base import SandboxError

    if "/workspace" not in code:
        return code
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    root = workspace.resolve()

    def rewrite(value: str) -> str:
        if value == "/workspace":
            suffix = ""
        elif value.startswith("/workspace/"):
            suffix = value[len("/workspace/") :]
        else:
            return value
        target = (root / suffix).resolve()
        if target != root and root not in target.parents:
            raise SandboxError(f"process-kernel /workspace path escapes workspace: {value!r}")
        rendered = str(target)
        # In an f-string the literal segment often ends at `/workspace/` and
        # the next segment is a formatted value. Preserve that separator;
        # pathlib resolution intentionally removes it from the root path.
        if value.endswith("/") and not rendered.endswith("/"):
            rendered += "/"
        return rendered

    class _WorkspaceLiteralTransformer(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802
            if isinstance(node.value, str):
                translated = rewrite(node.value)
                if translated != node.value:
                    return ast.copy_location(ast.Constant(value=translated), node)
            return node

    rewritten = _WorkspaceLiteralTransformer().visit(tree)
    ast.fix_missing_locations(rewritten)
    return ast.unparse(rewritten)


class _BoundedTextCapture:
    """List-compatible bounded accumulator for streamed kernel text."""

    def __init__(self) -> None:
        self.total = 0
        self._small = ""
        self._head = ""
        self._tail = ""

    def append(self, value: object) -> None:
        text = str(value or "")
        self.total += len(text)
        if len(self._small) <= _KERNEL_STREAM_CAP:
            room = _KERNEL_STREAM_CAP + 1 - len(self._small)
            self._small += text[:room]
        if len(self._head) < _KERNEL_STREAM_HEAD:
            self._head += text[: _KERNEL_STREAM_HEAD - len(self._head)]
        if len(text) >= _KERNEL_STREAM_TAIL:
            self._tail = text[-_KERNEL_STREAM_TAIL:]
        else:
            self._tail = (self._tail + text)[-_KERNEL_STREAM_TAIL:]

    def render(self) -> str:
        if self.total <= _KERNEL_STREAM_CAP:
            return self._small[: self.total]
        dropped = max(0, self.total - len(self._head) - len(self._tail))
        return (
            self._head + f"\n[disco: kernel output truncated; {self.total} chars total, "
            f"{dropped} omitted]\n" + self._tail
        )

    def __iter__(self):
        yield self.render()


def _cap_kernel_scalar(value: object) -> str | None:
    if value is None:
        return None
    capture = _BoundedTextCapture()
    capture.append(value)
    return capture.render()


def _cap_kernel_traceback(values: object) -> str:
    capture = _BoundedTextCapture()
    if not isinstance(values, (list, tuple)):
        capture.append(_ANSI_ESCAPE.sub("", str(values or "")))
        return capture.render()
    for index, value in enumerate(values):
        if index:
            capture.append("\n")
        capture.append(_ANSI_ESCAPE.sub("", str(value or "")))
    return capture.render()
