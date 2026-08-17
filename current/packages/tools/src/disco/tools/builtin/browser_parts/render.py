"""Agent-visible rendering of a browser observation, split out of ``BrowserTool``.

``_render_console`` was mccabe-21; decomposed into named per-entry helpers so
each stays under the callable budget. The decomposition is behavior-preserving
byte-for-byte: in particular, the original outer loop kept iterating after a
mid-stack truncation (its very next top-of-loop cap check then broke
immediately, a redundant but side-effect-free step) — `_build_console_lines`
below reproduces the same final `lines`/`truncated` state.
"""

from __future__ import annotations

from typing import Any


def format_source(location: dict[str, Any]) -> str:
    """B7: turn a console message's location dict into a `url:line:col` string."""
    url = location.get("url")
    if not url:
        return ""
    line = location.get("lineNumber")
    if line is None:
        return str(url)
    col = location.get("columnNumber")
    src = f"{url}:{line}"
    if col is not None:
        src += f":{col}"
    return src


def _console_counts(console: list[dict[str, Any]]) -> tuple[int, int]:
    errors = sum(1 for c in console if c.get("level") == "error")
    warnings = sum(1 for c in console if c.get("level") == "warning")
    return errors, warnings


def _append_stack(stack: str, lines: list[str], *, max_lines: int, max_stack_lines: int) -> bool:
    """Append truncated stack lines to `lines`. Returns True if the console cap was hit."""
    stack_lines = [s.rstrip() for s in stack.splitlines() if s.strip()]
    hit_cap = False
    for sl in stack_lines[:max_stack_lines]:
        if len(lines) >= max_lines:
            hit_cap = True
            break
        lines.append(f"      {sl.strip()}")
    if len(stack_lines) > max_stack_lines:
        lines.append(f"      ... (stack truncated to {max_stack_lines} lines)")
    return hit_cap


def _append_console_entry(
    entry: dict[str, Any], lines: list[str], *, max_lines: int, max_stack_lines: int
) -> bool:
    level = entry.get("level", "log")
    line = f"  - {level}: {entry.get('text', '')}"
    source = format_source(entry.get("location") or {})
    if source:
        line += f"  @ {source}"
    lines.append(line)
    stack = entry.get("stack")
    if not stack:
        return False
    return _append_stack(stack, lines, max_lines=max_lines, max_stack_lines=max_stack_lines)


def _build_console_lines(
    console: list[dict[str, Any]], has_problems: bool, *, max_lines: int, max_stack_lines: int
) -> tuple[list[str], bool]:
    lines: list[str] = []
    truncated = False
    for entry in console:
        if len(lines) >= max_lines:
            truncated = True
            break
        level = entry.get("level", "log")
        is_problem = level in ("error", "warning")
        # log/info/debug are noise on a healthy page — only surface them when
        # there is an error/warning to give them context.
        if not is_problem and not has_problems:
            continue
        hit_cap = _append_console_entry(
            entry, lines, max_lines=max_lines, max_stack_lines=max_stack_lines
        )
        if hit_cap:
            truncated = True
    return lines, truncated


def render_console(console: list[dict[str, Any]], *, max_lines: int, max_stack_lines: int) -> str:
    """B7: structured error block. For each error/warning emit level, text, the
    source:line (from location) and a stack truncated to ~max_stack_lines lines.
    console.log/info are included too, but only WHEN errors/warnings are present
    (the diagnostics leading up to a crash) — the whole block is capped at
    max_lines."""
    if not console:
        return ""
    errors, warnings = _console_counts(console)
    has_problems = errors > 0 or warnings > 0
    lines, truncated = _build_console_lines(
        console, has_problems, max_lines=max_lines, max_stack_lines=max_stack_lines
    )
    if not lines:
        return ""
    if truncated:
        lines.append(f"  ... (console truncated to {max_lines} lines)")
    return f"CONSOLE ({errors} errors, {warnings} warnings):\n" + "\n".join(lines) + "\n"


def render_network(network: list[dict[str, Any]], *, max_lines: int) -> str:
    """B7: failed/4xx-5xx requests as `NETWORK FAIL: <method> <url> -> <reason>`,
    capped at max_lines."""
    if not network:
        return ""
    lines: list[str] = []
    for n in network[:max_lines]:
        reason = n.get("failure") or n.get("status") or "failed"
        method = n.get("method", "GET")
        url = n.get("url", "")
        lines.append(f"  NETWORK FAIL: {method} {url} -> {reason}")
    if len(network) > max_lines:
        lines.append(f"  ... ({len(network) - max_lines} more network failures)")
    return "\n".join(lines) + "\n"


def render_observation(
    data: dict[str, Any],
    *,
    max_console_lines: int,
    max_stack_lines: int,
    max_network_lines: int,
    fence_open: str,
    fence_close: str,
) -> str:
    console_lines = render_console(
        data.get("console", []), max_lines=max_console_lines, max_stack_lines=max_stack_lines
    )
    network_lines = render_network(data.get("network", []), max_lines=max_network_lines)

    elements = data.get("elements", [])
    elements_lines = ""
    if elements:
        elements_lines = "ELEMENTS:\n" + "\n".join(f"  {el}" for el in elements) + "\n"

    content = (
        f"{fence_open}\n"
        f"URL: {data.get('url')}\n"
        f"TITLE: {data.get('title')}\n"
        f"{console_lines}"
        f"{network_lines}"
        f"{elements_lines}"
        f"TEXT:\n{data.get('text')}\n"
        f"{fence_close}"
    )
    if data.get("screenshot_path"):
        content += f"\nscreenshot: {data['screenshot_path']}"
    return content
