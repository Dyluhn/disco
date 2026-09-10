"""Return a same-length JS/TS "executable tokens only" view of source text.

``_js_executable_view`` is a lexer, not a JavaScript grammar: it answers whether a
concrete property-access token occurs in executable source, masking comments,
quoted strings, regex literals, and template-literal text so a bounded signal
scan (e.g. ``import.meta.env.VITE_*`` recognition) never mistakes documentation
prose for a runtime read.

The scanner keeps one mutable ``_JsFrame`` per lexical nesting level (the outer
source, or a ``${...}`` expression inside a template literal, or a template
literal itself) instead of ten parallel stacks, and each phase of a single
character's dispatch is its own small function — a plain state-machine ceiling
imposed by keeping every step independently readable and independently bounded.
"""

from __future__ import annotations

_JS_EXPRESSION_PREFIXES = {
    "await",
    "break",
    "case",
    "continue",
    "debugger",
    "delete",
    "do",
    "else",
    "in",
    "instanceof",
    "new",
    "of",
    "return",
    "throw",
    "typeof",
    "void",
    "yield",
}
_JS_CONTROL_PARENS = {"catch", "for", "if", "switch", "while", "with"}
_JS_BLOCK_PREFIXES = {"do", "else", "finally", "try"}
_JS_OBJECT_PREFIXES = {
    "await",
    "case",
    "delete",
    "new",
    "return",
    "throw",
    "typeof",
    "void",
    "yield",
}


class _JsFrame:
    """One lexical scan frame: the outer source, or a nested template literal /
    ``${...}`` expression. Bundling the scanner's per-frame fields here (instead
    of ten parallel stacks kept in lockstep) is what lets each phase of the scan
    move into its own small, independently bounded helper below."""

    __slots__ = (
        "mode",
        "brace_depth",
        "regex_allowed",
        "object_allowed",
        "paren_controls",
        "brace_objects",
        "pending_control",
        "pending_block",
        "pending_value_body",
        "previous_token",
    )

    def __init__(self, mode: str, *, brace_depth: int = 0) -> None:
        self.mode = mode
        self.brace_depth = brace_depth
        self.regex_allowed = True
        self.object_allowed = False
        self.paren_controls: list[bool] = []
        self.brace_objects: list[bool] = []
        self.pending_control = False
        self.pending_block = False
        self.pending_value_body = False
        self.previous_token: str | None = None


def _copy(out: list[str], source: str, start: int, end: int) -> None:
    out[start:end] = source[start:end]


def _line_end(source: str, start: int, size: int) -> int:
    cr = source.find("\r", start)
    lf = source.find("\n", start)
    ends = [value for value in (cr, lf) if value != -1]
    return min(ends) if ends else size


def _skip_quoted(source: str, index: int, quote: str, size: int) -> int:
    index += 1
    while index < size:
        if source[index] == "\\":
            index = min(size, index + 2)
            continue
        index += 1
        if source[index - 1] == quote:
            break
    return index


def _skip_regex(source: str, index: int, size: int) -> int:
    index += 1
    in_class = False
    while index < size:
        char = source[index]
        if char == "\\":
            index = min(size, index + 2)
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < size and source[index].isalpha():
                index += 1
            return index
        elif char in "\r\n":
            return index
        index += 1
    return index


def _starts_anonymous_body(source: str, position: int, size: int) -> bool:
    """Whether a `class`/`function` token at `position` opens an anonymous value
    body (`class {`, `function(`) rather than a named declaration."""
    lookahead = position
    while lookahead < size and source[lookahead].isspace():
        lookahead += 1
    return lookahead < size and source[lookahead] in "({"


def _advance_template(source: str, index: int, size: int, stack: list[_JsFrame]) -> int:
    """Advance one step while the top frame is inside a template literal body."""
    char = source[index]
    if char == "\\":
        return min(size, index + 2)
    if char == "`":
        stack.pop()
        index += 1
        if stack and stack[-1].mode == "code":
            frame = stack[-1]
            frame.regex_allowed = False
            frame.object_allowed = False
            frame.previous_token = "literal"
        return index
    if source.startswith("${", index):
        stack.append(_JsFrame("code", brace_depth=1))
        return index + 2
    return index + 1


def _skip_trivia(source: str, index: int, size: int, out: list[str]) -> int | None:
    """Consume whitespace or a comment span, or ``None`` if this is neither."""
    char = source[index]
    if char.isspace():
        out[index] = char
        return index + 1
    if source.startswith("//", index):
        return _line_end(source, index + 2, size)
    if source.startswith("/*", index):
        end = source.find("*/", index + 2)
        return size if end == -1 else end + 2
    if source.startswith("<!--", index):
        end = source.find("-->", index + 4)
        return size if end == -1 else end + 3
    return None


def _scan_literal(
    source: str, index: int, size: int, stack: list[_JsFrame], out: list[str]
) -> int | None:
    """Consume a quoted string, a template-literal open, or a regex literal."""
    frame = stack[-1]
    char = source[index]
    if char in "'\"":
        new_index = _skip_quoted(source, index, char, size)
        frame.regex_allowed = False
        frame.object_allowed = False
        frame.pending_control = False
        frame.pending_block = False
        frame.previous_token = "literal"
        return new_index
    if char == "`":
        frame.regex_allowed = False
        frame.pending_control = False
        stack.append(_JsFrame("template"))
        return index + 1
    if char == "/" and frame.regex_allowed:
        new_index = _skip_regex(source, index, size)
        frame.regex_allowed = False
        frame.object_allowed = False
        frame.pending_control = False
        frame.pending_block = False
        frame.previous_token = "literal"
        return new_index
    return None


def _scan_identifier(
    source: str, index: int, size: int, stack: list[_JsFrame], out: list[str]
) -> int:
    frame = stack[-1]
    end = index + 1
    while end < size and (source[end].isalnum() or source[end] in "_$"):
        end += 1
    token = source[index:end]
    _copy(out, source, index, end)
    property_name = frame.previous_token in {".", "?."}
    was_object_allowed = frame.object_allowed
    if not property_name:
        frame.pending_control = token in _JS_CONTROL_PARENS
        frame.pending_block = token in _JS_BLOCK_PREFIXES
        frame.regex_allowed = token in _JS_EXPRESSION_PREFIXES
        frame.object_allowed = token in _JS_OBJECT_PREFIXES
        if token in {"class", "function"}:
            frame.pending_value_body = (
                was_object_allowed
                or _starts_anonymous_body(source, end, size)
                or frame.previous_token == "default"
            )
    else:
        frame.pending_control = False
        frame.pending_block = False
        frame.regex_allowed = False
        frame.object_allowed = False
    frame.previous_token = token
    return end


def _scan_number(source: str, index: int, size: int, stack: list[_JsFrame], out: list[str]) -> int:
    frame = stack[-1]
    end = index + 1
    while end < size and (source[end].isalnum() or source[end] in "._"):
        end += 1
    _copy(out, source, index, end)
    frame.regex_allowed = False
    frame.object_allowed = False
    frame.pending_control = False
    frame.pending_block = False
    frame.previous_token = "literal"
    return end


def _scan_word_or_number(
    source: str, index: int, size: int, stack: list[_JsFrame], out: list[str]
) -> int | None:
    char = source[index]
    if char.isalpha() or char in "_$":
        return _scan_identifier(source, index, size, stack, out)
    if char.isdigit():
        return _scan_number(source, index, size, stack, out)
    return None


def _scan_paren(source: str, index: int, stack: list[_JsFrame], out: list[str]) -> int | None:
    frame = stack[-1]
    char = source[index]
    if char == "(":
        out[index] = char
        frame.paren_controls.append(frame.pending_control)
        frame.pending_control = False
        frame.pending_block = False
        frame.regex_allowed = True
        frame.object_allowed = True
        frame.previous_token = "("
        return index + 1
    if char == ")":
        out[index] = char
        was_control = frame.paren_controls.pop() if frame.paren_controls else False
        frame.pending_control = False
        frame.pending_block = was_control
        frame.regex_allowed = was_control
        frame.object_allowed = False
        frame.previous_token = ")"
        return index + 1
    return None


def _track_template_brace(char: str, stack: list[_JsFrame]) -> bool:
    """Update `${...}` brace-depth bookkeeping for `char`.

    Returns ``True`` when this closed the innermost template expression (the
    caller must advance past `char` and stop — the closing brace itself stays
    unrevealed, exactly like the rest of the surrounding template text)."""
    frame = stack[-1]
    if char == "{" and frame.brace_depth > 0:
        frame.brace_depth += 1
        frame.brace_objects.append(
            (frame.object_allowed or frame.pending_value_body) and not frame.pending_block
        )
        frame.pending_value_body = False
        return False
    if char == "}" and frame.brace_depth > 0:
        frame.brace_depth -= 1
        if frame.brace_depth == 0:
            stack.pop()
            return True
        return False
    if char == "{":
        frame.brace_objects.append(
            (frame.object_allowed or frame.pending_value_body) and not frame.pending_block
        )
        frame.pending_value_body = False
    return False


def _scan_operator(source: str, index: int, stack: list[_JsFrame], out: list[str]) -> int | None:
    frame = stack[-1]
    if source.startswith("=>", index):
        _copy(out, source, index, index + 2)
        frame.pending_control = False
        frame.pending_block = True
        frame.regex_allowed = True
        frame.object_allowed = False
        frame.previous_token = "=>"
        return index + 2
    if source.startswith("++", index) or source.startswith("--", index):
        _copy(out, source, index, index + 2)
        frame.regex_allowed = False
        frame.object_allowed = False
        frame.pending_control = False
        frame.pending_block = False
        frame.previous_token = "postfix"
        return index + 2
    if source.startswith("?.", index):
        _copy(out, source, index, index + 2)
        frame.regex_allowed = False
        frame.object_allowed = False
        frame.pending_control = False
        frame.pending_block = False
        frame.previous_token = "?."
        return index + 2
    return None


def _apply_fallback_char_state(char: str, frame: _JsFrame, was_pending_block: bool) -> None:
    if char in "]":
        frame.regex_allowed = False
        frame.object_allowed = False
    elif char == ".":
        frame.regex_allowed = False
        frame.object_allowed = False
    elif char == "}":
        # Object literals are values (so a following slash is division); statement
        # blocks end a statement and may be followed by a regexp expression.
        was_object = frame.brace_objects.pop() if frame.brace_objects else False
        frame.regex_allowed = not was_object
        frame.object_allowed = False
    elif char == "{":
        frame.regex_allowed = True
        frame.object_allowed = False
    elif was_pending_block:
        frame.regex_allowed = True
        frame.object_allowed = False
    elif char in "=,:?[!~+-*%&|^<>":
        frame.regex_allowed = True
        frame.object_allowed = True
    elif char == ";":
        frame.regex_allowed = True
        frame.object_allowed = False
        frame.pending_value_body = False
    else:
        frame.regex_allowed = True
        frame.object_allowed = True


def _consume_fallback_char(source: str, index: int, stack: list[_JsFrame], out: list[str]) -> int:
    frame = stack[-1]
    char = source[index]
    out[index] = char
    frame.pending_control = False
    was_pending_block = frame.pending_block
    frame.pending_block = False
    _apply_fallback_char_state(char, frame, was_pending_block)
    frame.previous_token = char
    return index + 1


def _advance_code(source: str, index: int, size: int, stack: list[_JsFrame], out: list[str]) -> int:
    """Advance one step while the top frame is ordinary (non-template) code."""
    result = _skip_trivia(source, index, size, out)
    if result is not None:
        return result
    result = _scan_literal(source, index, size, stack, out)
    if result is not None:
        return result
    result = _scan_word_or_number(source, index, size, stack, out)
    if result is not None:
        return result
    result = _scan_paren(source, index, stack, out)
    if result is not None:
        return result
    char = source[index]
    if _track_template_brace(char, stack):
        return index + 1
    result = _scan_operator(source, index, stack, out)
    if result is not None:
        return result
    return _consume_fallback_char(source, index, stack, out)


def _js_executable_view(source: str) -> str:
    """Return a same-length view containing JS/TS executable tokens only.

    Comments, quoted strings, regular-expression literals, and the text portions of
    template literals are replaced with spaces.  Expressions inside ``${...}`` remain
    visible.  Keeping offsets and newlines stable makes this useful for bounded signal
    scans without mistaking documentation such as
    ``"import.meta.env.VITE_example"`` for a runtime read.

    This is deliberately a lexer, not a JavaScript grammar.  Its only job is to answer
    whether a concrete property-access token occurs in executable source; malformed or
    ambiguous source remains the responsibility of the real build/boot gates.

    Declared conservative ceiling: regex literals immediately after a labeled
    ``break``/``continue`` or labeled block are not fully disambiguated. If such a regex
    literally spells an import-meta Vite read, it can add a false required build input
    and send that unusual project to review. It cannot hide a real input or create a
    runnable false candidate. Full label/ASI disambiguation belongs in a real JS parser,
    not this bounded pre-filter.
    """
    out = ["\n" if ch == "\n" else " " for ch in source]
    size = len(source)
    # Iterative frames avoid a recursion limit on valid, deeply nested template
    # expressions.  A code-frame depth of zero is ordinary source; a positive depth is
    # a `${...}` expression whose matching brace returns to its template frame.
    stack: list[_JsFrame] = [_JsFrame("code")]
    index = 0
    while index < size and stack:
        if stack[-1].mode == "template":
            index = _advance_template(source, index, size, stack)
        else:
            index = _advance_code(source, index, size, stack, out)
    return "".join(out)
