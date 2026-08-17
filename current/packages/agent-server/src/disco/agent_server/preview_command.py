"""Preview command normalization and launch-intent policy."""

from __future__ import annotations

import logging
import shlex

from .preview_models import (
    _FRAMEWORK_COMMANDS,
    _RAW_HARDCODED_PORT_RE,
    _SHELL_OPERATOR_RE,
    PreviewCommandError,
    PreviewSession,
    _adapt_framework_command_port,
    _classify_port_flag,
    _looks_like_port,
)
from .preview_models import (
    preview_requires_node_dependencies as preview_requires_node_dependencies,
)
from .preview_port_allocator import _PreviewPortResource

_LOG = logging.getLogger("disco.agent_server.preview_manager")


class _PreviewCommandResource(_PreviewPortResource):
    def _scrub_port_flag_tokens(self, tokens: list[str]) -> list[str]:
        """Walk the shlex'd argv and DROP every port-specifying flag that carries a
        CONCRETE model-chosen port (in ANY normalized form — `--port 8000`, `--port "8000"`,
        `--port='8000'`, `-p8000`, `-p 8000`, `PORT='8000'`), so the platform port can never
        be overridden through the command. The platform owns the port and injects it via the
        `PORT=<its port>` prefix, so a dropped flag's server still gets the right port.

        A flag whose value is the literal `{port}` placeholder is the SANCTIONED way to
        position the platform port: keep the `--port`/`-p` flag untouched (it is filled with
        the allocated port later). A redundant `PORT={port}` is dropped (the platform already
        injects PORT=). Everything else passes through unchanged.

        This replaces the old regex-on-raw-string scrub, which lost to quoting/`=`-joining —
        after shlex those forms are normalized tokens we handle uniformly here."""
        out: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            value, span, kind = _classify_port_flag(tok, nxt)
            if kind and value is not None:
                if value.isdigit():
                    # Concrete port → drop the flag (+ its value token for `--port X`/`-p X`).
                    i += span
                    continue
                if value == "{port}":
                    if kind == "env":
                        i += span  # PORT={port}: redundant with the platform PORT= prefix.
                        continue
                    # `--port {port}` / `--port={port}` / `-p{port}`: keep — filled later.
                    out.append(tok)
                    if span == 2 and nxt is not None:
                        out.append(nxt)
                    i += span
                    continue
            out.append(tok)
            i += 1
        return out

    def _reject_positional_tokens(self, tokens: list[str]) -> None:
        """Reject any BARE positional port-like token in the (operator-free, port-flag-scrubbed)
        argv — a hardcoded port the platform can't override sitting as a plain positional arg
        (`http.server <port>`, or a port positioned anywhere). HEURISTIC: a numeric token
        immediately following a flag (a token starting with `-`) is that flag's VALUE, not a
        port, so legit numeric args like `--workers 4` / `--timeout 30` are not false-rejected.
        A `{port}` placeholder token is not a digit, so it is never mistaken for a hardcoded
        port."""
        prev_was_flag = False
        for tok in tokens:
            if _looks_like_port(tok) and not prev_was_flag:
                raise PreviewCommandError(
                    f"preview command carries a hardcoded port ({tok!r}) as a positional "
                    "argument — the platform owns the port, so it can't be supplied here. "
                    "Remove it (the platform injects PORT=<its port>), or put the literal "
                    "placeholder '{port}' where the port goes (e.g. 'python3 -m "
                    "http.server {port} -d dist')."
                )
            prev_was_flag = tok.startswith("-")

    def _resolve_command(
        self,
        port: int,
        *,
        serve_dir: str | None,
        command: str | None,
        framework: str | None,
    ) -> str:
        """Turn model INTENT into the actual command, with the PLATFORM port baked in.

        An explicit `command` chooses the project script; a known `framework` still
        adapts it to the platform port. Otherwise use a known framework template or
        statically serve `serve_dir`.

        A raw command never gets to pick the port: any `--port/-p/PORT=` the model
        embedded (quoted, `=`-joined, or bare) is stripped on the SHLEX'D TOKENS and the
        platform port is injected via `PORT=`. A raw command that ALSO binds a port through
        a form the scrub can't override (a positional `http.server <port>`, a `host:port`
        bind) is REJECTED (`PreviewCommandError`) — unless it uses the literal `{port}`
        placeholder, which the platform fills with ITS port (the explicit, safe way to put
        the platform port at a positional slot).
        """
        serve = serve_dir or self._sandbox_workspace() or "."
        if command:
            # GRAMMAR RESTRICTION (the real guarantee): a raw preview command must be a
            # SINGLE FOREGROUND process. Reject shell control / chaining / backgrounding /
            # piping / substitution operators (and newlines) at the root — they're how a
            # model smuggles a second listener on a hardcoded port past the ownership probe.
            if _SHELL_OPERATOR_RE.search(command):
                raise PreviewCommandError(
                    "preview command must be a SINGLE FOREGROUND process — shell control, "
                    "chaining, backgrounding, piping, or substitution operators "
                    "(; & | && || ` $(...) >(...) <(...) newlines) are not allowed (they can "
                    "smuggle a second server on a hardcoded port the platform can't see). "
                    "Use one command and put the serve port as the literal placeholder "
                    "'{port}' (e.g. 'python3 -m http.server {port} -d dist')."
                )
            # Tokenize ONCE (the operator-ban above guarantees a single command). All
            # port-flag handling happens on these tokens, not on the raw string, so the
            # quoted/`=`-joined forms that beat a raw-string regex are normalized first.
            try:
                tokens = shlex.split(command)
            except ValueError as exc:
                raise PreviewCommandError(
                    f"preview command could not be parsed as a single shell command ({exc}). "
                    "Provide a simple foreground command with the serve port as '{port}'."
                ) from exc
            # Drop every concrete port flag (`--port/-p/PORT=`, any quoting/join); keep a
            # `{port}` placeholder flag for the platform to fill.
            cleaned = self._scrub_port_flag_tokens(tokens)
            # Reject ANY other hardcoded/positional port the flag-scrub can't account for —
            # even on the `{port}`-placeholder path. The placeholder is the ONE sanctioned
            # way to put the platform port at a positional slot, but the REST of the command
            # must still be port-clean: a second, hardcoded port alongside `{port}` (e.g.
            # 'http.server {port} 9999') would bind a model-chosen port the platform doesn't
            # own (P1 #1). The `{port}` token is not a digit, so it never trips this check.
            self._reject_positional_tokens(cleaned)
            # Backstop for `host:port` / `:port` BIND forms (gunicorn `-b :8000`, `serve -l
            # 0.0.0.0:8000`) that carry a colon and so are not a bare integer token above.
            # Run it on a residual rebuilt from the cleaned tokens with `{port}` blanked, so
            # only a CONCRETE bind (`:8000`) trips it — never the sanctioned `:{port}`.
            residual = " ".join(t.replace("{port}", " ") for t in cleaned)
            bound = _RAW_HARDCODED_PORT_RE.search(residual)
            if bound is not None:
                raise PreviewCommandError(
                    "preview command binds a hardcoded port "
                    f"({bound.group(0).strip()!r}) — the platform owns the port, so it "
                    "can't be supplied here. Remove the port (the platform injects PORT="
                    "<its port>), or put the literal placeholder '{port}' where the port "
                    "goes (e.g. 'python3 -m http.server {port} -d dist')."
                )
            cleaned = _adapt_framework_command_port(cleaned, framework=framework)
            # Fill sanctioned placeholders before safely rejoining. PORT covers generic
            # custom runtimes; configured adapters add their required CLI binding.
            filled = [t.replace("{port}", str(port)) for t in cleaned]
            return f"PORT={port} {shlex.join(filled)}"
        if framework:
            template = _FRAMEWORK_COMMANDS.get(framework.strip().lower())
            if template is not None:
                return template.format(port=port, dir=shlex.quote(serve))
        # Default + unknown frameworks: serve the build output statically.
        return f"python3 -m http.server {port} -d {shlex.quote(serve)}"

    def _sandbox_workspace(self) -> str | None:
        try:
            return getattr(self._sandbox, "workspace_path", None)
        except Exception:  # noqa: BLE001 — best-effort default
            return None

    async def _launch_workspace(self, explicit_cwd: str | None) -> str | None:
        """Resolve the command cwd independently from ``serve_dir``.

        Container sandboxes deliberately expose no host ``workspace_path`` even though
        their in-guest workspace is ``/workspace``.  Falling back to a relative
        ``serve_dir`` made that directory both the shell cwd and ``http.server -d``
        argument (``release/release``).  Ask the sandbox for its actual cwd instead;
        leaving it unset is safer than ever reusing the content path as a cwd.
        """

        if explicit_cwd:
            return explicit_cwd
        workspace = self._sandbox_workspace()
        if workspace:
            return workspace
        try:
            result = await self._sandbox.exec_shell("pwd", timeout_s=5)
            if getattr(result, "exit_code", 1) == 0:
                discovered = str(getattr(result, "stdout", "")).strip()
                if discovered:
                    return discovered
        except Exception:  # noqa: BLE001 — shell manager can use its own safe default
            _LOG.debug("preview workspace discovery failed", exc_info=True)
        return None

    @staticmethod
    def _requires_successful_root(session: PreviewSession) -> bool:
        """Static directory intent is ready only when its selected root is successful."""

        return session.intent.get("launch_kind") == "static"

    @staticmethod
    def _launch_kind(*, command: str | None, framework: str | None) -> str:
        """Mirror command resolution so root-health policy matches the actual launcher."""

        normalized = (framework or "").strip().lower()
        if (
            normalized
            and normalized in _FRAMEWORK_COMMANDS
            and normalized
            not in {
                "static",
                "http",
            }
        ):
            return "framework"
        if command:
            return "custom"
        return "static"

    def _default_name(self, serve_dir: str | None, framework: str | None) -> str:
        if framework:
            return f"preview-{framework.strip().lower()}"
        if serve_dir and serve_dir not in (".", ""):
            leaf = serve_dir.rstrip("/").rsplit("/", 1)[-1]
            if leaf:
                return f"preview-{leaf}"
        return "preview"

    async def _coordinate_auto_preview(self) -> None:
        """Stand the sandbox's legacy fire-and-forget static auto-preview DOWN the first
        time the platform manager starts a preview for this sandbox. After this, the
        manager is the SINGLE authority for previews — the auto-preview can no longer
        squat a curated port or answer health on a port the manager allocates (the P1 #2
        false-validate). Best-effort + idempotent: a fake/old sandbox without the hook is
        simply left as-is (allocation's tracked-port + live-listener guards still hold)."""
        if self._auto_preview_coordinated:
            return
        self._auto_preview_coordinated = True
        disable = getattr(self._sandbox, "disable_auto_preview", None)
        if disable is None:
            return
        try:
            await disable()
        except Exception:  # noqa: BLE001 — coordination is best-effort, never fatal
            _LOG.debug("auto-preview coordination failed", exc_info=True)
