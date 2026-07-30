"""
toolcall_recovery.py
Recovery of tool calls from weak models.

Prior art citation:
This module incorporates design adaptations harvested from SmallCode
(https://github.com/Doorman11991/smallcode).

> Copyright (c) 2026 Doorman11991
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of
> this software and associated documentation files (the "Software"), to deal in the
> Software without restriction, including without limitation the rights to use, copy,
> modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
> and to permit persons to whom the Software is furnished to do so, subject to the
> following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
> INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
> PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
> HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
> CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
> OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import json
import re
from typing import Any

from disco.core.llm.types import ProposedToolCall


def _fix_trailing_comma(text: str) -> str:
    text = re.sub(r",\s*\}", "}", text)
    text = re.sub(r",\s*\]", "]", text)
    return text


def _tool_call_from_name_args(name: Any, args: Any) -> ProposedToolCall | None:
    """Build a ProposedToolCall when ``name`` is a str and ``args`` is a dict."""
    if isinstance(name, str) and isinstance(args, dict):
        return ProposedToolCall(tool_name=name, arguments=args)
    return None


def _parse_tool_json(data: Any) -> list[ProposedToolCall]:
    if isinstance(data, list):
        calls = []
        for item in data:
            calls.extend(_parse_tool_json(item))
        return calls

    if not isinstance(data, dict):
        return []

    # {"name": "foo", "arguments": {"x": 1}}
    if "name" in data and "arguments" in data:
        call = _tool_call_from_name_args(data["name"], data["arguments"])
        if call is not None:
            return [call]

    # {"function": {"name": "foo", "arguments": {"x": 1}}}
    if "function" in data and isinstance(data["function"], dict):
        f = data["function"]
        if "name" in f and "arguments" in f:
            call = _tool_call_from_name_args(f["name"], f["arguments"])
            if call is not None:
                return [call]

    # {"tool": "foo", "args": {"x": 1}}
    if "tool" in data and "args" in data:
        call = _tool_call_from_name_args(data["tool"], data["args"])
        if call is not None:
            return [call]

    return []


def _extract_json_strings(text: str) -> list[str]:
    hermes = []
    for m in re.finditer(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL):
        inner = m.group(1).strip()
        fence_match = re.search(r"^```(?:json|tool_call)?\s*(.*?)\s*```$", inner, re.DOTALL)
        if fence_match:
            inner = fence_match.group(1).strip()
        hermes.append(inner)
    if hermes:
        return hermes

    fences = []
    for m in re.finditer(r"```(?:json|tool_call)\s*(.*?)\s*```", text, re.DOTALL):
        fences.append(m.group(1).strip())
    if fences:
        return fences

    s = text.strip()
    if s.startswith("{") or s.startswith("["):
        return [s]

    return []


def recover_tool_calls(
    content: str | None, reasoning_content: str | None
) -> list[ProposedToolCall]:
    calls = []

    for text in [reasoning_content, content]:
        if not text:
            continue

        json_strings = _extract_json_strings(text)

        for js_str in json_strings:
            js_str = _fix_trailing_comma(js_str)
            try:
                decoder = json.JSONDecoder()
                js_str = js_str.strip()
                obj, idx = decoder.raw_decode(js_str)
                calls.extend(_parse_tool_json(obj))
            except json.JSONDecodeError:
                pass

    return calls
