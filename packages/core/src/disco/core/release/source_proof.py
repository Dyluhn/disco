"""Bounded, conservative source/config proofs for neutral release plans.

This module owns lexical facts only. It does not choose a runtime, construct release
models, or emit an adapter bundle; ``detect`` orchestrates those decisions and maps an
unproved fact to a typed blocker. Ambiguous source is deliberately unproved.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass

from disco.core.release.command_grammar import check_workspace_rel_path

_SAFE_JS_LITERAL_RE = re.compile(
    r"(?:[A-Z_][A-Z0-9_]*|node:[a-z_]+(?:/[a-z_]+)?|0\.0\.0\.0|/[A-Za-z0-9._~/-]*|"
    r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9._-]+)*)"
)
# A declaration keyword with NO binding identifier (`const = …` / `const;` / `let;`) — a
# runtime SyntaxError. Anchored away from a preceding `.`/word char so a legal reserved-word
# member access (`obj.const = 5`) is never mistaken for a declaration.
_DECL_NO_BINDING_RE = re.compile(r"(?<![.\w$])(?:const|let|var)\s*[=;]")
# An ASSIGNMENT `=` (not `==`/`===`/`!=`/`<=`/`>=`/`=>`) with an EMPTY right-hand side — the
# `=` is immediately followed (whitespace only) by a statement/closing terminator. Group 1 is
# that whitespace run, correlated back to the ORIGINAL source so a masked template/string/regex
# RHS (valid code, blanked to spaces in the view) is not mistaken for a truly empty one.
_EMPTY_ASSIGN_RHS_RE = re.compile(r"(?<![=!<>])=(?![=>])(\s*)[;)\]}]")
_REQUIRE_RE = re.compile(r"\brequire\s*\(\s*(['\"])([^'\"]+)\1")
_IMPORT_FROM_RE = re.compile(r"\b(?:from|import)\s*(['\"])([^'\"]+)\1")
_VITE_ENV_DOT_RE = re.compile(r"import\.meta\.env\.(VITE_[A-Za-z0-9_]+)")
_VITE_ENV_BRACKET_RE = re.compile(r"import\.meta\.env\[\s*(['\"])(VITE_[A-Za-z0-9_]+)\1\s*\]")
_VITE_ENV_DESTRUCTURE_RE = re.compile(
    r"\b(?:const|let|var)\s*\{([^{}]+)\}\s*=\s*import\.meta\.env\b"
)
_VITE_OUTDIR_LITERAL_RE = re.compile(r"\boutDir\s*:\s*(['\"])([^'\"]+)\1")
_VITE_OUTDIR_KEY_RE = re.compile(r"\boutDir\b")
_VITE_ENV_PREFIX_LITERAL_RE = re.compile(r"\benvPrefix\s*:\s*(['\"])([^'\"]+)\1")
_VITE_ENV_PREFIX_KEY_RE = re.compile(r"\benvPrefix\b")
_VITE_CONFIG_NAMES = frozenset(
    {
        "vite.config.js",
        "vite.config.ts",
        "vite.config.mjs",
        "vite.config.cjs",
        "vite.config.mts",
        "vite.config.cts",
    }
)
_NODE_BUILTINS = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)
_NPM_CREDENTIAL_KEY_MARKERS = (
    "_auth",
    "authtoken",
    "token",
    "password",
    "secret",
    "apikey",
    "api_key",
    "credential",
)


@dataclass(frozen=True)
class NodeSourceProof:
    """Conservative facts about the exact file a direct Node start executes."""

    executable_view: str | None
    external_packages: frozenset[str]
    error: str | None = None


def _masked(segment: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in segment)


def _regex_can_start(parts: list[str]) -> bool:
    tail = ""
    for part in reversed(parts):
        tail = part[-64:] + tail
        if len(tail) >= 64:
            break
    stripped = tail.rstrip()
    if not stripped:
        return True
    if stripped.endswith("=>") or stripped[-1] in "=(:,[!&|?;{}":
        return True
    word = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*)$", stripped)
    return word is not None and word.group(1) in {"case", "delete", "return", "throw", "void"}


def js_executable_view(text: str) -> str | None:
    """Mask comments/string data/templates/regex and reject obvious invalid structure.

    Exact host/env/route/path literals remain visible where executable syntax needs their
    value. Everything else is spaces with offsets/newlines preserved, so callers can
    correlate an original-source match with whether its leading syntax is executable.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        following = text[index + 1] if index + 1 < length else ""
        if char == "/" and following == "/":
            end = text.find("\n", index + 2)
            end = length if end == -1 else end
            out.append(_masked(text[index:end]))
            index = end
            continue
        if char == "/" and following == "*":
            close = text.find("*/", index + 2)
            if close == -1:
                return None
            end = close + 2
            out.append(_masked(text[index:end]))
            index = end
            continue
        if char in {"'", '"'}:
            quote = char
            cursor = index + 1
            escaped = False
            closed = False
            while cursor < length:
                current = text[cursor]
                if current == "\\":
                    escaped = True
                    cursor += 2
                    continue
                cursor += 1
                if current == quote:
                    closed = True
                    break
            if not closed:
                return None
            segment = text[index:cursor]
            content = segment[1:-1]
            out.append(
                segment
                if not escaped and _SAFE_JS_LITERAL_RE.fullmatch(content) is not None
                else _masked(segment)
            )
            index = cursor
            continue
        if char == "`":
            cursor = index + 1
            closed = False
            while cursor < length:
                current = text[cursor]
                if current == "\\":
                    cursor += 2
                    continue
                cursor += 1
                if current == "`":
                    closed = True
                    break
            if not closed:
                return None
            out.append(_masked(text[index:cursor]))
            index = cursor
            continue
        if char == "/" and _regex_can_start(out):
            cursor = index + 1
            in_class = False
            closed = False
            while cursor < length:
                current = text[cursor]
                if current == "\\":
                    cursor += 2
                    continue
                if current == "[":
                    in_class = True
                elif current == "]":
                    in_class = False
                elif current == "/" and not in_class:
                    cursor += 1
                    while cursor < length and text[cursor].isalpha():
                        cursor += 1
                    closed = True
                    break
                if current == "\n":
                    break
                cursor += 1
            if not closed:
                return None
            out.append(_masked(text[index:cursor]))
            index = cursor
            continue
        out.append(char)
        index += 1
    view = _mask_literal_false_branches("".join(out))
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for char in view:
        if char in "([{":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return None
    if stack or re.search(r"(?:%%|@@|##)", view) is not None:
        return None
    return view


def _mask_literal_false_branches(view: str) -> str:
    """Mask block bodies whose condition is the exact literal ``false``.

    This is intentionally tiny rather than an optimizer. It prevents a statically dead
    listener from establishing release ingress while leaving every non-literal branch
    unclassified. Offsets and newlines are preserved.
    """
    chars = list(view)
    search_from = 0
    pattern = re.compile(r"\bif\s*\(\s*false\s*\)\s*\{")
    while match := pattern.search(view, search_from):
        opening = match.end() - 1
        depth = 1
        cursor = opening + 1
        while cursor < len(view) and depth:
            if view[cursor] == "{":
                depth += 1
            elif view[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth:
            return view
        for index in range(match.start(), cursor):
            if chars[index] != "\n":
                chars[index] = " "
        search_from = cursor
    return "".join(chars)


def node_external_packages(source: str, view: str) -> frozenset[str]:
    """External package roots imported by executable require/import syntax."""
    modules: set[str] = set()
    for pattern in (_REQUIRE_RE, _IMPORT_FROM_RE):
        for match in pattern.finditer(source):
            # Offsets are preserved. A match whose leading keyword was masked lived in
            # inert data and cannot establish a runtime dependency.
            prefix_length = len(match.group(0).split("(", 1)[0])
            if not view[match.start() : match.start() + prefix_length].strip():
                continue
            module = match.group(2)
            root_name = module.split("/", 1)[0]
            if (
                module.startswith((".", "/", "node:"))
                or module in _NODE_BUILTINS
                or root_name in _NODE_BUILTINS
            ):
                continue
            parts = module.split("/")
            root = "/".join(parts[:2]) if module.startswith("@") and len(parts) >= 2 else parts[0]
            if root:
                modules.add(root.lower())
    return frozenset(modules)


def _executable_match(view: str, match: re.Match[str]) -> bool:
    keyword = re.match(r"[A-Za-z]+", match.group(0))
    width = keyword.end() if keyword is not None else 1
    return bool(view[match.start() : match.start() + width].strip())


def _resolve_local_module(
    module: str,
    entry_path: str,
    sources: Mapping[str, str],
    *,
    esm_import: bool,
) -> str | None:
    parent = posixpath.dirname(entry_path)
    resolved = posixpath.normpath(posixpath.join(parent, module))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        return None
    if esm_import:
        # Node ESM requires an explicit file extension and does not perform CommonJS
        # directory/index resolution. JSON additionally needs import attributes, which
        # are outside this closed source grammar.
        if not posixpath.splitext(resolved)[1] or resolved.endswith(".json"):
            return None
        candidates = [resolved]
    else:
        candidates = [resolved]
    if not posixpath.splitext(resolved)[1]:
        candidates.extend(resolved + suffix for suffix in (".js", ".cjs", ".mjs", ".json"))
        package_path = posixpath.join(resolved, "package.json")
        if package_path in sources:
            try:
                package = json.loads(sources[package_path])
            except (json.JSONDecodeError, ValueError):
                return None
            if not isinstance(package, dict):
                return None
            main = package.get("main", "index.js")
            if not isinstance(main, str) or not main or package.get("exports") is not None:
                return None
            target = posixpath.normpath(posixpath.join(resolved, main))
            if target == resolved or not target.startswith(resolved.rstrip("/") + "/"):
                return None
            candidates.append(target)
        candidates.extend(
            posixpath.join(resolved, name) for name in ("index.js", "index.cjs", "index.mjs")
        )
    target = next((candidate for candidate in candidates if candidate in sources), None)
    if target is None or (not esm_import and target.endswith(".mjs")):
        return None
    parent = posixpath.dirname(target)
    while parent:
        if posixpath.join(parent, "package.json") in sources:
            return None
        parent = posixpath.dirname(parent)
    return target


def _invalid_bounded_js(view: str, source: str) -> bool:
    """A targeted static invalid-token class the balanced/decoy lexical proof accepts but the
    Node runtime rejects with a SyntaxError at boot: a declaration keyword with no binding
    identifier, or an assignment `=` with an empty right-hand side.

    Both are checked on the MASKED ``view`` so string/comment/template/regex CONTENT can never
    trip them. The empty-RHS match is additionally confirmed against the ORIGINAL ``source``:
    a masked non-empty RHS (e.g. ``const x = `tpl`;`` or ``const r = /re/;``) blanks to spaces
    in the view but is non-whitespace in the source, so it is correctly left as valid code —
    preserving the lexical-mask controls (a listener hidden in a template/regex/comment stays
    unproved via the port proof, not misclassified as a syntax error)."""
    if _DECL_NO_BINDING_RE.search(view) is not None:
        return True
    for match in _EMPTY_ASSIGN_RHS_RE.finditer(view):
        if source[match.start(1) : match.end(1)].strip() == "":
            return True
    return False


def _prove_one_node_source(
    source: str,
    *,
    entry_path: str,
    sources: Mapping[str, str],
    package_type: str | None,
) -> tuple[NodeSourceProof, tuple[str, ...]]:
    """Prove syntax boundary, module mode, and static imports for one Node entry file.

    This is a deliberately closed ceiling, not a JavaScript interpreter. Dynamic imports,
    dynamic ``require`` calls, a mismatched ESM/CJS package boundary, and missing local
    modules are unproved. External package names are returned for reconciliation with the
    exact npm install plan by the detector.
    """
    view = js_executable_view(source)
    if view is None:
        return (
            NodeSourceProof(
                None,
                frozenset(),
                "the exact Node entrypoint is not valid bounded JavaScript",
            ),
            (),
        )
    if _invalid_bounded_js(view, source):
        return (
            NodeSourceProof(
                None,
                frozenset(),
                "the exact Node entrypoint is not valid bounded JavaScript",
            ),
            (),
        )

    require_matches = tuple(_REQUIRE_RE.finditer(source))
    import_matches = tuple(_IMPORT_FROM_RE.finditer(source))
    executable_requires = tuple(
        match for match in require_matches if _executable_match(view, match)
    )
    executable_imports = tuple(match for match in import_matches if _executable_match(view, match))

    # Any executable require/import call not accounted for by a literal form has dynamic
    # module resolution. Such a graph cannot be proven from immutable file names.
    literal_require_offsets = {match.start() for match in executable_requires}
    for match in re.finditer(r"\brequire\s*\(", view):
        if match.start() not in literal_require_offsets:
            return (
                NodeSourceProof(view, frozenset(), "the Node entrypoint uses a dynamic require"),
                (),
            )
    if re.search(r"\bimport\s*\(", view) is not None:
        return NodeSourceProof(view, frozenset(), "the Node entrypoint uses a dynamic import"), ()

    suffix = posixpath.splitext(entry_path)[1].lower()
    esm = suffix == ".mjs" or (suffix not in {".cjs", ".cts"} and package_type == "module")
    if esm and executable_requires:
        return (
            NodeSourceProof(view, frozenset(), "the ESM Node entrypoint uses CommonJS require"),
            (),
        )
    if not esm and executable_imports:
        return (
            NodeSourceProof(
                view,
                frozenset(),
                "the CommonJS Node entrypoint uses ESM import syntax",
            ),
            (),
        )

    local_paths: list[str] = []
    for match in executable_requires:
        module = match.group(2)
        if module.startswith("."):
            resolved = _resolve_local_module(module, entry_path, sources, esm_import=False)
            if resolved is None:
                return (
                    NodeSourceProof(
                        view,
                        frozenset(),
                        "the Node entrypoint imports a missing or unproved local module",
                    ),
                    (),
                )
            local_paths.append(resolved)
    for match in executable_imports:
        module = match.group(2)
        if module.startswith("."):
            resolved = _resolve_local_module(module, entry_path, sources, esm_import=True)
            if resolved is None:
                return (
                    NodeSourceProof(
                        view,
                        frozenset(),
                        "the Node entrypoint imports a missing or unproved local module",
                    ),
                    (),
                )
            local_paths.append(resolved)
    return NodeSourceProof(view, node_external_packages(source, view)), tuple(local_paths)


def prove_node_source(
    sources: Mapping[str, str], *, entry_path: str, package_type: str | None
) -> NodeSourceProof:
    """Recursively prove the exact literal local module graph of a Node entrypoint."""
    pending = [entry_path]
    visited: set[str] = set()
    external: set[str] = set()
    entry_view: str | None = None
    while pending:
        if len(visited) >= 128:
            return NodeSourceProof(
                entry_view, frozenset(), "the local Node module graph is too large"
            )
        path = pending.pop()
        if path in visited:
            continue
        source = sources.get(path)
        if source is None:
            return NodeSourceProof(entry_view, frozenset(), "the local Node module is absent")
        if path.endswith(".json"):
            try:
                json.loads(source)
            except (json.JSONDecodeError, ValueError):
                return NodeSourceProof(
                    entry_view, frozenset(), "an imported local JSON module does not parse"
                )
            visited.add(path)
            continue
        proof, local_paths = _prove_one_node_source(
            source,
            entry_path=path,
            sources=sources,
            package_type=package_type,
        )
        if proof.error is not None:
            return proof
        if path == entry_path:
            entry_view = proof.executable_view
        external.update(proof.external_packages)
        visited.add(path)
        pending.extend(path for path in local_paths if path not in visited)
    return NodeSourceProof(entry_view, frozenset(external))


def _balanced_call_arguments(view: str, opening: int) -> tuple[str, ...] | None:
    stack = ["("]
    start = opening + 1
    parts: list[str] = []
    cursor = start
    pairs = {")": "(", "]": "[", "}": "{"}
    while cursor < len(view):
        char = view[cursor]
        if char in "([{":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return None
            if not stack:
                parts.append(view[start:cursor].strip())
                return tuple(parts)
        elif char == "," and len(stack) == 1:
            parts.append(view[start:cursor].strip())
            start = cursor + 1
        cursor += 1
    return None


def node_has_public_port_bind(view: str, port_env: str) -> bool:
    """Whether a recognized Node HTTP server listens on the adapter-owned port."""
    port = (
        r"process\.env(?:\."
        + re.escape(port_env)
        + r"\b|\[\s*['\"]"
        + re.escape(port_env)
        + r"['\"]\s*\])"
    )
    port_value = r"(?:Number\(\s*" + port + r"\s*\)|" + port + r")"
    aliases = {
        match.group(1)
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            + port_value
            + r"(?:\s*(?:\|\||\?\?)\s*[0-9]+)?\s*;?",
            view,
        )
    }

    http_aliases = {
        match.group(1)
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            r"require\s*\(\s*['\"](?:node:)?(?:http|https|net)['\"]\s*\)",
            view,
        )
    }
    http_aliases.update(
        match.group(1)
        for match in re.finditer(
            r"\bimport\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+from\s+"
            r"['\"](?:node:)?(?:http|https|net)['\"]",
            view,
        )
    )
    server_aliases = {
        match.group(1)
        for alias in http_aliases
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            + re.escape(alias)
            + r"\.createServer\s*\(",
            view,
        )
    }
    server_aliases.update(
        match.group(1)
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            r"require\s*\(\s*['\"](?:node:)?(?:http|https|net)['\"]\s*\)"
            r"\.createServer\s*\(",
            view,
        )
    )

    express_factories = {
        match.group(1)
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            r"require\s*\(\s*['\"]express['\"]\s*\)",
            view,
        )
    }
    express_factories.update(
        match.group(1)
        for match in re.finditer(
            r"\bimport\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+from\s+['\"]express['\"]",
            view,
        )
    )
    server_aliases.update(
        match.group(1)
        for factory in express_factories
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            + re.escape(factory)
            + r"\s*\(",
            view,
        )
    )
    server_aliases.update(
        match.group(1)
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            r"require\s*\(\s*['\"]express['\"]\s*\)\s*\(",
            view,
        )
    )
    callback_aliases = {
        match.group(1)
        for match in re.finditer(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", view)
    }
    callback_aliases.update(
        match.group(1)
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>",
            view,
        )
    )

    def brace_depth(position: int) -> int:
        depth = 0
        for char in view[:position]:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
        return depth

    top_level_exit = any(
        brace_depth(match.start()) == 0 for match in re.finditer(r"\bprocess\.exit\s*\(", view)
    )

    for match in re.finditer(r"\.listen\s*\(", view):
        if brace_depth(match.start()) != 0:
            continue
        arguments = _balanced_call_arguments(view, match.end() - 1)
        if not arguments or not arguments[0]:
            continue
        first = arguments[0]
        if re.fullmatch(port_value, first) is None and first not in aliases:
            continue

        prefix = view[: match.start()].rstrip()
        if top_level_exit and re.search(r"\bprocess\.exit\s*\(", prefix) is not None:
            continue
        receiver = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*$", prefix)
        recognized = receiver is not None and receiver.group(1) in server_aliases
        if not recognized:
            statement = prefix[max(prefix.rfind(";"), prefix.rfind("\n")) + 1 :]
            if "&&" in statement or "||" in statement or "?" in statement:
                continue
            recognized = any(
                re.search(r"\b" + re.escape(alias) + r"\.createServer\s*\(", statement) is not None
                for alias in http_aliases
            )
            recognized = (
                recognized
                or re.search(
                    r"\brequire\s*\(\s*['\"](?:node:)?(?:http|https|net)['\"]\s*\)"
                    r"\.createServer\s*\(",
                    statement,
                )
                is not None
            )
        if not recognized:
            continue

        if len(arguments) == 1:
            return True
        second = arguments[1]
        if second in {"'0.0.0.0'", '"0.0.0.0"'}:
            return True
        if second.startswith(("(", "function", "async")) or second in callback_aliases:
            return True
    return False


def vite_env_names(source: str) -> frozenset[str] | None:
    view = js_executable_view(source)
    if view is None:
        return None
    names = set(_VITE_ENV_DOT_RE.findall(view))
    names.update(match.group(2) for match in _VITE_ENV_BRACKET_RE.finditer(view))
    for match in _VITE_ENV_DESTRUCTURE_RE.finditer(view):
        fields = tuple(part.strip() for part in match.group(1).split(","))
        for field in fields:
            if re.fullmatch(r"VITE_[A-Za-z0-9_]+", field) is None:
                return None
            names.add(field)
    return frozenset(names)


def vite_config_contract_supported(configs: Mapping[str, str]) -> bool:
    root = sorted(
        (path, text)
        for path, text in configs.items()
        if "/" not in path and path in _VITE_CONFIG_NAMES
    )
    if len(root) > 1:
        return False
    if not root:
        return True
    view = _vite_exported_config_view(root[0][1])
    if view is None:
        return False
    prefix_keys = _top_level_property_matches(view, "envPrefix")
    if len(prefix_keys) > 1:
        return False
    if prefix_keys:
        prefix = re.match(r"\s*(['\"])([^'\"]+)\1", view[prefix_keys[0].end() :])
        return prefix is not None and prefix.group(2) == "VITE_"
    return not prefix_keys


def prove_vite_output_dir(configs: Mapping[str, str]) -> str | None:
    """Exact root Vite output directory, or None when absent/conflicting/dynamic/unsafe."""
    root = sorted(
        (path, text)
        for path, text in configs.items()
        if "/" not in path and path in _VITE_CONFIG_NAMES
    )
    if len(root) > 1:
        return None
    if not root:
        return "dist"
    view = _vite_exported_config_view(root[0][1])
    if view is None:
        return None
    build = _object_property(view, "build")
    if build is None:
        return None if re.search(r"\bbuild\s*:", view) is not None else "dist"
    keys = _top_level_property_matches(build, "outDir")
    if len(keys) > 1:
        return None
    if not keys:
        return "dist"
    match = re.match(r"\s*(['\"])([^'\"]+)\1", build[keys[0].end() :])
    if match is None:
        return None
    output = match.group(2)
    try:
        check_workspace_rel_path(output, field="Vite output_dir")
    except ValueError:
        return None
    return output


def _vite_exported_config_view(text: str) -> str | None:
    """The one statically exported Vite config expression, excluding decoy objects."""
    view = js_executable_view(text)
    if view is None:
        return None
    exports = tuple(re.finditer(r"\bexport\s+default\b|\bmodule\.exports\s*=", view))
    if len(exports) != 1:
        return None
    exported = view[exports[0].end() :].strip()
    if not exported:
        return None
    # A named/dynamic export would require evaluation. The supported ceiling is a
    # literal object, optionally wrapped in Vite's pure defineConfig helper.
    if exported.startswith("defineConfig"):
        exported = exported[len("defineConfig") :].lstrip()
        if not exported.startswith("("):
            return None
        exported = exported[1:].lstrip()
        wrapped = True
    else:
        wrapped = False
    if not exported.startswith("{"):
        return None
    end = _matching_brace(exported, 0)
    if end is None:
        return None
    tail = exported[end + 1 :].strip()
    if wrapped:
        if not tail.startswith(")"):
            return None
        tail = tail[1:].strip()
    if tail not in {"", ";"}:
        return None
    object_view = exported[: end + 1]
    if "..." in object_view:
        return None
    return object_view


def _matching_brace(view: str, opening: int) -> int | None:
    depth = 0
    for index in range(opening, len(view)):
        if view[index] == "{":
            depth += 1
        elif view[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _object_property(view: str, name: str) -> str | None:
    matches = _top_level_property_matches(view, name)
    if len(matches) != 1:
        return None
    cursor = matches[0].end()
    if cursor >= len(view) or view[cursor] != "{":
        return None
    end = _matching_brace(view, cursor)
    return None if end is None else view[cursor : end + 1]


def _top_level_property_matches(view: str, name: str) -> tuple[re.Match[str], ...]:
    matches: list[re.Match[str]] = []
    for match in re.finditer(r"\b" + re.escape(name) + r"\s*:\s*", view):
        depth = 0
        for char in view[: match.start()]:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
        if depth == 1:
            matches.append(match)
    return tuple(matches)


def npmrc_requires_secret(text: str) -> bool:
    """Whether an active root npm config line carries/refers to a credential."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.partition("=")
        lowered = key.strip().lower()
        if separator and any(marker in lowered for marker in _NPM_CREDENTIAL_KEY_MARKERS):
            if value.strip():
                return True
        if re.search(r"://[^/@\s]+@", line) is not None:
            return True
    return False


def npmrc_omits_dev_dependencies(text: str) -> bool:
    """Whether active root npm config removes devDependencies from installation."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip().lower().replace("_", "-")
        normalized = {item.strip().lower() for item in value.split(",")}
        if key == "omit" and "dev" in normalized:
            return True
        if key == "production" and value.strip().lower() in {"true", "1", "yes"}:
            return True
    return False


__all__ = [
    "js_executable_view",
    "node_has_public_port_bind",
    "node_external_packages",
    "NodeSourceProof",
    "npmrc_omits_dev_dependencies",
    "npmrc_requires_secret",
    "prove_node_source",
    "prove_vite_output_dir",
    "vite_config_contract_supported",
    "vite_env_names",
]
