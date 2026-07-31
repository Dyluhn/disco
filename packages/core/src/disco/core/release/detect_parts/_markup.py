"""Markup-aware JS/TS signal scanning: HTML/Vue/Svelte/Astro script extraction,
JSX tag/expression spans, Vue directive expressions, and the Vite
``import.meta.env.VITE_*`` recognizer that reads an already-masked view.

Each scanner below is a small orchestrator calling single-purpose helpers —
the same algorithm as before, laid out so every piece stays independently
checkable and bounded.
"""

from __future__ import annotations

import re
from bisect import bisect_right

from ._constants import _VITE_ENV_NAME_RE
from ._js_view import _js_executable_view
from ._text import _norm

_HTML_SCRIPT_TYPE_RE = re.compile(
    r"\stype\s*=\s*(?:(['\"])(.*?)\1|([^\s>]+))", re.IGNORECASE | re.DOTALL
)


_HTML_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def _span_is_excluded(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    index = bisect_right(spans, (start, 10**30)) - 1
    return index >= 0 and start >= spans[index][0] and end <= spans[index][1]


def _merged_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _balanced_markup_expressions(
    source: str, excluded: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Return outer brace-expression interiors with a single bounded scan."""
    regions: list[tuple[int, int]] = []
    lexical = _js_executable_view(source)
    excluded = sorted(excluded)
    excluded_index = 0
    depth = 0
    start = 0
    index = 0
    while index < len(source):
        while excluded_index < len(excluded) and index >= excluded[excluded_index][1]:
            excluded_index += 1
        if (
            depth == 0
            and excluded_index < len(excluded)
            and excluded[excluded_index][0] <= index < excluded[excluded_index][1]
        ):
            index = excluded[excluded_index][1]
            continue
        char = lexical[index]
        if char == "{":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                regions.append((start, index))
        index += 1
    return regions


# ---- _markup_regions: excluded comment/style spans + executable script bodies --


def _html_tag_end(source: str, start: int, size: int) -> int:
    quote: str | None = None
    cursor = start
    while cursor < size:
        char = source[cursor]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == ">":
            return cursor + 1
        cursor += 1
    return size


def _markup_comment_span(source: str, start: int, size: int) -> tuple[int, int]:
    end_marker = source.find("-->", start + 4)
    end = size if end_marker == -1 else end_marker + 3
    return start, end


def _markup_script_or_style_kind(lowered: str, source: str, start: int, size: int) -> str | None:
    for candidate in ("script", "style"):
        prefix = f"<{candidate}"
        if lowered.startswith(prefix, start):
            boundary = start + len(prefix)
            if boundary >= size or source[boundary].isspace() or source[boundary] in ">/":
                return candidate
    return None


def _markup_find_closing_tag(
    lowered: str, source: str, closing: str, body_start: int, size: int
) -> int:
    close_start = lowered.find(closing, body_start)
    while close_start != -1:
        boundary = close_start + len(closing)
        if boundary >= size or source[boundary].isspace() or source[boundary] in ">/":
            break
        close_start = lowered.find(closing, boundary)
    return close_start


def _markup_script_is_scannable(
    opening_tag: str, *, html_modules_only: bool, astro_processed_only: bool
) -> bool:
    type_match = _HTML_SCRIPT_TYPE_RE.search(opening_tag)
    script_type = (
        (type_match.group(2) or type_match.group(3) or "").strip().lower()
        if type_match is not None
        else ""
    )
    astro_attributes = opening_tag[len("<script") :].rstrip().removesuffix(">").strip()
    astro_processed = not astro_attributes or (
        re.fullmatch(
            r"src\s*=\s*(?:(['\"])[^'\"]*\1|[^\s>]+)",
            astro_attributes,
            re.IGNORECASE,
        )
        is not None
    )
    return (not html_modules_only or script_type == "module") and (
        not astro_processed_only or astro_processed
    )


def _markup_regions(
    source: str, *, html_modules_only: bool, astro_processed_only: bool
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Return excluded comment/style spans and executable script-body spans.

    The scan advances monotonically: a malformed repeated ``<script`` prefix is
    inspected once, never restarted at every ``<``."""
    lowered = source.lower()
    excluded: list[tuple[int, int]] = []
    scripts: list[tuple[int, int]] = []
    index = 0
    size = len(source)

    while index < size:
        start = source.find("<", index)
        if start == -1:
            break
        if source.startswith("<!--", start):
            span_start, end = _markup_comment_span(source, start, size)
            excluded.append((span_start, end))
            index = end
            continue
        kind = _markup_script_or_style_kind(lowered, source, start, size)
        if kind is None:
            index = start + 1
            continue
        body_start = _html_tag_end(source, start + len(kind) + 1, size)
        if body_start >= size and (not source or source[-1] != ">"):
            break
        closing = f"</{kind}"
        close_start = _markup_find_closing_tag(lowered, source, closing, body_start, size)
        if close_start == -1:
            break
        close_end = _html_tag_end(source, close_start + len(closing), size)
        if kind == "script":
            opening_tag = source[start:body_start]
            if _markup_script_is_scannable(
                opening_tag,
                html_modules_only=html_modules_only,
                astro_processed_only=astro_processed_only,
            ):
                scripts.append((body_start, close_start))
        else:
            excluded.append((start, close_end))
        index = close_end
    return excluded, scripts


# ---- _jsx_expression_end: one JSX `{...}` expression's closing brace ----------


def _jsx_closing_tag_end(fragment: str, tag_start: int) -> int:
    cursor = tag_start + 2
    if cursor < len(fragment) and fragment[cursor] == ">":
        return cursor + 1
    if cursor < len(fragment) and (fragment[cursor].isalpha() or fragment[cursor] in "_$"):
        cursor += 1
        while cursor < len(fragment) and (
            fragment[cursor].isalnum() or fragment[cursor] in "_$:.-"
        ):
            cursor += 1
        while cursor < len(fragment) and fragment[cursor].isspace():
            cursor += 1
        return cursor + 1 if cursor < len(fragment) and fragment[cursor] == ">" else -1
    return -1


def _mask_closing_jsx_tags(fragment: str) -> str:
    # Neutralize `</...>` tags so the JS lexer never reads their `/` as a regexp.
    tag_neutral = list(fragment)
    tag_index = 0
    while tag_index < len(fragment):
        tag_start = fragment.find("</", tag_index)
        if tag_start == -1:
            break
        tag_end = _jsx_closing_tag_end(fragment, tag_start)
        if tag_end == -1:
            tag_index = tag_start + 2
            continue
        for index in range(tag_start, tag_end):
            if fragment[index] != "\n":
                tag_neutral[index] = " "
        tag_index = tag_end
    return "".join(tag_neutral)


def _jsx_expression_end(source: str, start: int, limit: int) -> int | None:
    """Return the closing brace for one JSX expression without parsing JSX as JS.

    Grows the inspected window geometrically so many small expressions stay linear
    while one large expression is rescanned a constant number of times. ``None``
    means the expression is malformed within the caller's bound."""
    if start >= limit or source[start] != "{":
        return None
    width = 256
    while True:
        window_end = min(limit, start + 1 + width)
        fragment = source[start + 1 : window_end]
        # A closing JSX tag begins with ``</``.  Fed directly to a JavaScript lexer,
        # that slash can look like the start of a regexp and hide the outer JSX
        # expression's closing brace.  Closing tags contain no expressions of their
        # own, so mask just their bounded syntax before lexing the surrounding code.
        lexical = _js_executable_view(_mask_closing_jsx_tags(fragment))
        depth = 1
        for offset, char in enumerate(lexical, start=start + 1):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return offset
        if window_end >= limit:
            return None
        width *= 2


# ---- _jsx_tag_spans: locate JSX tags in linear time ----------------------------


def _scan_jsx_tag_from(
    source: str,
    start: int,
    cursor: int,
    size: int,
    closing: bool,
    tags: list[tuple[int, int, bool, bool]],
) -> int | None:
    # Appends the tag to `tags` and returns the next index, or `None` if malformed
    # (an unresolvable `{...}` expression, or one that never closes).
    quote: str | None = None
    escaped = False
    while cursor < size:
        char = source[cursor]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "{":
            expression_end = _jsx_expression_end(source, cursor, size)
            if expression_end is None:
                return None
            cursor = expression_end + 1
            continue
        elif char == ">":
            token = source[start : cursor + 1]
            tags.append((start, cursor + 1, closing, token.rstrip().endswith("/>")))
            return cursor + 1
        cursor += 1
    return None


def _jsx_tag_spans(source: str) -> list[tuple[int, int, bool, bool]]:
    """Locate JSX tags in linear time, including fragments and quoted attributes."""
    tags: list[tuple[int, int, bool, bool]] = []
    size = len(source)
    index = 0
    while index < size:
        start = source.find("<", index)
        if start == -1:
            break
        cursor = start + 1
        closing = cursor < size and source[cursor] == "/"
        if closing:
            cursor += 1
        if cursor < size and source[cursor] == ">":
            tags.append((start, cursor + 1, closing, False))
            index = cursor + 1
            continue
        if cursor >= size or not (source[cursor].isalpha() or source[cursor] in "_$"):
            index = start + 1
            continue
        next_index = _scan_jsx_tag_from(source, start, cursor, size, closing, tags)
        if next_index is None:
            break
        index = next_index
    return tags


# ---- opening-tag attribute scanning (shared by attribute-name / directive scans)


def _skip_tag_attribute_gap(source: str, index: int, end: int) -> int:
    while index < end and (source[index].isspace() or source[index] == "/"):
        index += 1
    return index


def _skip_attribute_value(source: str, index: int, end: int) -> int:
    if index < end and source[index] in "'\"":
        quote = source[index]
        index += 1
        while index < end and source[index] != quote:
            index += 1
        if index < end:
            index += 1
        return index
    while index < end and not source[index].isspace() and source[index] != ">":
        index += 1
    return index


def _consume_tag_attribute(source: str, index: int, end: int, names: set[str]) -> int | None:
    # Consume one `name`/`name=value` attribute; `None` at the tag's end.
    if index >= end or source[index] == ">":
        return None
    name_start = index
    while index < end and not source[index].isspace() and source[index] not in "=>":
        index += 1
    if index > name_start:
        names.add(source[name_start:index].lower())
    while index < end and source[index].isspace():
        index += 1
    if index >= end or source[index] != "=":
        return index
    index += 1
    while index < end and source[index].isspace():
        index += 1
    return _skip_attribute_value(source, index, end)


def _opening_tag_attribute_names(source: str, start: int, end: int) -> set[str]:
    names: set[str] = set()
    index = start + 1
    while index < end and not source[index].isspace() and source[index] not in ">/":
        index += 1
    while index < end:
        index = _skip_tag_attribute_gap(source, index, end)
        next_index = _consume_tag_attribute(source, index, end, names)
        if next_index is None:
            break
        index = next_index
    return names


# ---- _vue_v_pre_spans: complete element subtrees left uncompiled via v-pre ----


def _tag_name_at(source: str, start: int, end: int, closing: bool) -> str:
    cursor = start + 1 + int(closing)
    name_start = cursor
    while cursor < end and not source[cursor].isspace() and source[cursor] not in ">/":
        cursor += 1
    return source[name_start:cursor].lower()


def _close_v_pre_entries(
    stack: list[tuple[str, int | None, bool]],
    name: str,
    end: int,
    spans: list[tuple[int, int]],
) -> None:
    match_index = next(
        (index for index in range(len(stack) - 1, -1, -1) if stack[index][0] == name),
        None,
    )
    if match_index is None:
        return
    closing_entries = stack[match_index:]
    del stack[match_index:]
    for _tag, root_start, starts_here in closing_entries:
        if starts_here and root_start is not None:
            spans.append((root_start, end))


def _open_v_pre_tag(
    source: str,
    start: int,
    end: int,
    name: str,
    self_closing: bool,
    stack: list[tuple[str, int | None, bool]],
    spans: list[tuple[int, int]],
) -> None:
    parent_root = stack[-1][1] if stack else None
    starts_here = parent_root is None and "v-pre" in _opening_tag_attribute_names(
        source, start, end
    )
    root_start = start if starts_here else parent_root
    if self_closing or name in _HTML_VOID_TAGS:
        if starts_here:
            spans.append((start, end))
    else:
        stack.append((name, root_start, starts_here))


def _vue_v_pre_spans(source: str) -> list[tuple[int, int]]:
    """Return complete element subtrees Vue leaves uncompiled via ``v-pre``."""
    spans: list[tuple[int, int]] = []
    stack: list[tuple[str, int | None, bool]] = []
    for start, end, closing, self_closing in _jsx_tag_spans(source):
        name = _tag_name_at(source, start, end, closing)
        if not name:
            continue
        if closing:
            _close_v_pre_entries(stack, name, end, spans)
            continue
        _open_v_pre_tag(source, start, end, name, self_closing, stack, spans)
    return sorted(spans)


# ---- _vue_directive_expression_spans: directive expressions from real attrs ---


def _directive_bracket_arg_span(name: str, name_start: int) -> tuple[int, int] | None:
    bracket = name.find("[")
    if bracket == -1:
        return None
    close = name.find("]", bracket + 1)
    if close <= bracket + 1:
        return None
    return (name_start + bracket + 1, name_start + close)


def _directive_attribute_name(
    source: str, index: int, tag_end: int, expressions: list[tuple[int, int]]
) -> tuple[bool, int] | None:
    # Consume one attribute name (recording a bracket-arg span); `None` at tag end.
    if index >= tag_end or source[index] == ">":
        return None
    name_start = index
    while index < tag_end and not source[index].isspace() and source[index] not in "=>":
        index += 1
    name = source[name_start:index]
    directive = name.lower().startswith(("v-", ":", "@", "#"))
    if directive:
        span = _directive_bracket_arg_span(name, name_start)
        if span is not None:
            expressions.append(span)
    return directive, index


def _skip_ws_after_attribute_name(source: str, index: int, tag_end: int) -> tuple[int, bool]:
    while index < tag_end and source[index].isspace():
        index += 1
    has_equals = index < tag_end and source[index] == "="
    return index, has_equals


def _directive_attribute_value_span(source: str, index: int, tag_end: int) -> tuple[int, int, int]:
    # Return `(value_start, value_end, next_index)` for one attribute value.
    if index < tag_end and source[index] in "'\"":
        quote = source[index]
        value_start = index + 1
        index = value_start
        while index < tag_end and source[index] != quote:
            index += 1
        value_end = index
        if index < tag_end:
            index += 1
        return value_start, value_end, index
    value_start = index
    while index < tag_end and not source[index].isspace() and source[index] != ">":
        index += 1
    return value_start, index, index


def _directive_attribute_value(
    source: str, index: int, tag_end: int, directive: bool, expressions: list[tuple[int, int]]
) -> int | None:
    # Consume `=value` if present; `None` when the tag ended mid-value.
    index, has_equals = _skip_ws_after_attribute_name(source, index, tag_end)
    if not has_equals:
        return index
    index += 1
    while index < tag_end and source[index].isspace():
        index += 1
    if index >= tag_end:
        return None
    value_start, value_end, index = _directive_attribute_value_span(source, index, tag_end)
    if directive and value_end > value_start:
        expressions.append((value_start, value_end))
    return index


def _consume_directive_attribute(
    source: str, index: int, tag_end: int, expressions: list[tuple[int, int]]
) -> int | None:
    result = _directive_attribute_name(source, index, tag_end, expressions)
    if result is None:
        return None
    directive, index = result
    return _directive_attribute_value(source, index, tag_end, directive, expressions)


def _vue_directive_expression_spans(
    source: str, excluded: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Return Vue directive expressions from actual opening-tag attributes only."""
    expressions: list[tuple[int, int]] = []
    for tag_start, tag_end, closing, _self_closing in _jsx_tag_spans(source):
        if closing or _span_is_excluded(tag_start, tag_end, excluded):
            continue
        index = tag_start + 1
        while index < tag_end and not source[index].isspace() and source[index] not in ">/":
            index += 1
        while index < tag_end:
            index = _skip_tag_attribute_gap(source, index, tag_end)
            next_index = _consume_directive_attribute(source, index, tag_end, expressions)
            if next_index is None:
                break
            index = next_index
    return expressions


def _embedded_markup_code(path: str, source: str) -> str:
    """Expose executable regions of HTML/Vue/Svelte/Astro source at stable offsets."""
    visible = ["\n" if char == "\n" else " " for char in source]

    def reveal(start: int, end: int) -> None:
        visible[start:end] = source[start:end]

    suffix = path.lower()
    excluded, scripts = _markup_regions(
        source,
        html_modules_only=suffix.endswith(".html"),
        astro_processed_only=suffix.endswith(".astro"),
    )
    if suffix.endswith(".vue"):
        excluded.extend(_vue_v_pre_spans(source))
    excluded = _merged_spans(excluded)
    for start, end in scripts:
        if not _span_is_excluded(start, end, excluded):
            reveal(start, end)

    if suffix.endswith((".vue", ".svelte", ".astro")):
        # Framework templates evaluate brace-delimited JS expressions.  A balanced,
        # iterative scan conserves nested expressions without recursive parsing.
        for start, end in _balanced_markup_expressions(source, excluded):
            reveal(start, end)
        if suffix.endswith(".vue"):
            for start, end in _vue_directive_expression_spans(source, [*excluded, *scripts]):
                reveal(start, end)
    if suffix.endswith(".astro") and source.startswith("---"):
        end = source.find("\n---", 3)
        if end != -1:
            reveal(3, end)
    return "".join(visible)


# ---- _mask_jsx_text: hide JSX child prose, keep tags/attrs/{...} code ---------


def _mask_jsx_gap(source: str, out: list[str], previous_end: int, start: int) -> None:
    """Blank JSX child-text between two sibling tags, except `{...}` expressions."""
    index = previous_end
    while index < start:
        if source[index] == "{":
            expression_end = _jsx_expression_end(source, index, start)
            if expression_end is not None:
                index = expression_end + 1
                continue
        if source[index] != "\n":
            out[index] = " "
        index += 1


def _mask_jsx_tag_reveal_expressions(source: str, out: list[str], start: int, end: int) -> None:
    """Blank one JSX tag, then re-reveal its brace-delimited attribute expressions."""
    for index in range(start, end):
        if source[index] != "\n":
            out[index] = " "
    index = start
    quote: str | None = None
    escaped = False
    while index < end:
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "{":
            expression_end = _jsx_expression_end(source, index, end)
            if expression_end is None:
                break
            out[index : expression_end + 1] = source[index : expression_end + 1]
            index = expression_end + 1
            continue
        index += 1


def _mask_jsx_text(source: str) -> str:
    """Mask JSX child prose while retaining tags, attributes, and ``{...}`` code."""
    out = list(source)
    tags = _jsx_tag_spans(source)
    depth = 0
    previous_end = 0
    for start, end, closing, self_closing in tags:
        if depth > 0 and previous_end < start:
            _mask_jsx_gap(source, out, previous_end, start)
        # Tag syntax is not JavaScript. Leaving ``</A><B>`` in the lexical view lets
        # the slash look like a regexp opener and can hide the next sibling's real
        # expression. Mask the tag itself while retaining only brace-delimited JSX
        # attribute expressions; quoted attribute text remains inert.
        _mask_jsx_tag_reveal_expressions(source, out, start, end)
        if closing:
            depth = max(0, depth - 1)
        elif not self_closing:
            depth += 1
        previous_end = end
    return "".join(out)


def _vite_scannable_source(path: str, source: str) -> str:
    lowered = _norm(path).lower()
    if lowered.endswith((".d.ts", ".d.mts", ".d.cts")):
        return ""
    if lowered.endswith((".html", ".vue", ".svelte", ".astro")):
        return _embedded_markup_code(lowered, source)
    if lowered.endswith((".jsx", ".tsx")):
        return _mask_jsx_text(source)
    return source


def _js_identifier_start(char: str) -> bool:
    return char in "_$" or char.isalpha() or (ord(char) > 127 and char.isidentifier())


def _js_identifier_continue(char: str) -> bool:
    return (
        char in "_$"
        or char.isalnum()
        or (ord(char) > 127 and (char.isidentifier() or ("a" + char).isidentifier()))
    )


def _js_identifier_escape(source: str, index: int) -> tuple[str, int] | None:
    if not source.startswith("\\u", index):
        return None
    cursor = index + 2
    if cursor < len(source) and source[cursor] == "{":
        end = source.find("}", cursor + 1)
        if end == -1 or not 1 <= end - cursor - 1 <= 6:
            return None
        digits = source[cursor + 1 : end]
        next_index = end + 1
    else:
        digits = source[cursor : cursor + 4]
        if len(digits) != 4:
            return None
        next_index = cursor + 4
    if any(char not in "0123456789abcdefABCDEF" for char in digits):
        return None
    try:
        return chr(int(digits, 16)), next_index
    except ValueError:
        return None


def _consume_js_identifier(source: str, index: int) -> tuple[str, int]:
    value = [source[index]]
    cursor = index + 1
    while cursor < len(source):
        char = source[cursor]
        if _js_identifier_continue(char):
            value.append(char)
            cursor += 1
            continue
        escaped = _js_identifier_escape(source, cursor)
        if escaped is None or not _js_identifier_continue(escaped[0]):
            break
        value.append(escaped[0])
        cursor = escaped[1]
    return "".join(value), cursor


# ---- _vite_env_reads_from_view: import.meta.env.VITE_* chain recognition -----


def _tokenize_js_view(source: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if _js_identifier_start(char):
            token, end = _consume_js_identifier(source, index)
            tokens.append(token)
            index = end
            continue
        if source.startswith("?.", index):
            tokens.append("?.")
            index += 2
            continue
        if not char.isspace():
            tokens.append(char)
        index += 1
    return tokens


def _match_parens(tokens: list[str]) -> dict[int, int]:
    paren_stack: list[int] = []
    matching_open: dict[int, int] = {}
    for token_index, token in enumerate(tokens):
        if token == "(":
            paren_stack.append(token_index)
        elif token == ")" and paren_stack:
            matching_open[token_index] = paren_stack.pop()
    return matching_open


def _grouping_open(tokens: list[str], open_index: int) -> bool:
    if open_index == 0:
        return True
    previous = tokens[open_index - 1]
    if previous in {"return", "throw", "yield", "await", "case", "delete", "void"}:
        return True
    if _js_identifier_start(previous[0]) or previous in {")", "]", "}"}:
        return False
    return previous not in {".", "?."}


def _mark_import_start(
    tokens: list[str], token_index: int, recognized: dict[int, tuple[int, int]]
) -> None:
    # Property/call-qualified `root.import` is not the import-meta primitive.
    if token_index and tokens[token_index - 1] in {".", "?."}:
        return
    recognized[token_index + 1] = (token_index, 1)


def _propagate_through_call(
    tokens: list[str],
    token_index: int,
    matching_open: dict[int, int],
    recognized: dict[int, tuple[int, int]],
) -> None:
    open_index = matching_open[token_index]
    inside = recognized.get(token_index)
    if inside is not None and inside[0] == open_index + 1 and _grouping_open(tokens, open_index):
        recognized[token_index + 1] = (open_index, inside[1])


def _advance_vite_env_chain(
    token_index: int,
    token: str,
    recognized: dict[int, tuple[int, int]],
    names: set[str],
) -> None:
    base = recognized.get(token_index - 1)
    if base is None:
        return
    start, stage = base
    if stage == 1 and token == "meta":
        recognized[token_index + 1] = (start, 2)
    elif stage == 2 and token == "env":
        recognized[token_index + 1] = (start, 3)
    elif stage == 3 and _VITE_ENV_NAME_RE.fullmatch(token):
        names.add(token)


def _recognize_vite_env_tokens(tokens: list[str], matching_open: dict[int, int]) -> set[str]:
    # An entry at end index N describes an exact recognized chain ending just
    # before N. Stages: import=1, import.meta=2, import.meta.env=3.
    recognized: dict[int, tuple[int, int]] = {}
    names: set[str] = set()
    for token_index, token in enumerate(tokens):
        if token == "import":
            _mark_import_start(tokens, token_index, recognized)
            continue
        if token == ")" and token_index in matching_open:
            _propagate_through_call(tokens, token_index, matching_open, recognized)
            continue
        if token_index < 2 or tokens[token_index - 1] not in {".", "?."}:
            continue
        _advance_vite_env_chain(token_index, token, recognized, names)
    return names


def _vite_env_reads_from_view(source: str) -> set[str]:
    """Recognize exact property chains in an already-masked executable JS view.

    Token identity avoids substring/boundary mistakes; a tiny interval recognizer
    permits redundant grouping and optional-chaining without mistaking a call such
    as ``f(import.meta).env`` for the Vite meta object."""
    tokens = _tokenize_js_view(source)
    matching_open = _match_parens(tokens)
    return _recognize_vite_env_tokens(tokens, matching_open)
