"""`find_and_edit` — regex fan-out edits mediated by a driver sub-LLM.

The tool finds regex matches across selected files, asks the configured driver
LLM whether each exact matched span should be replaced, and applies accepted
replacements through `exact_replace` with optimistic concurrency.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Self

import httpx
from disco.core import SecurityRisk
from disco.core.effects import EffectCapability
from disco.core.think import strip_think_spans
from pydantic import BaseModel, Field, model_validator

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from ..sandbox.base import strip_redundant_workspace_prefix
from ._slides_pipeline import (
    _extract_json_object,
    _purpose_for_model_endpoint,
    _resolve_llm_key,
    _resolve_slides_llm,
)
from .files import (
    ExactReplaceArgs,
    ExactReplaceEdit,
    ExactReplaceTool,
    _all_occurrences,
    mark_read,
    record_read,
)

_FS = frozenset({Capability.FILESYSTEM})
_DEFAULT_MAX_FILES = 50
_DEFAULT_MAX_MATCHES = 200
_CONCURRENCY = 5
_CONTEXT_RADIUS_LINES = 20
_MAX_LLM_TOKENS = 4096
_MAX_TRUNCATION_RETRIES = 1
_GLOB_MAGIC = frozenset("*?[")


class FindAndEditArgs(BaseModel):
    pattern: str = Field(description="Python regular expression to find in selected files.")
    instruction: str = Field(
        description=(
            "Natural-language instruction for how each matched span should change. "
            "The sub-LLM may skip any ambiguous match."
        )
    )
    paths: list[str] | None = Field(
        default=None, description="Explicit workspace-relative files to scan."
    )
    glob: str | None = Field(
        default=None, description="Optional workspace-relative glob, e.g. `src/**/*.tsx`."
    )
    max_files: int = Field(
        default=_DEFAULT_MAX_FILES,
        ge=1,
        le=500,
        description="Maximum selected files to scan; the tool refuses when exceeded.",
    )
    max_matches: int = Field(
        default=_DEFAULT_MAX_MATCHES,
        ge=1,
        le=2000,
        description="Maximum regex matches to evaluate; the tool refuses when exceeded.",
    )

    @model_validator(mode="after")
    def _require_selector(self) -> Self:
        if not self.paths and not self.glob:
            raise ValueError("find_and_edit requires at least one of `paths` or `glob`.")
        return self


@dataclass(frozen=True)
class _Match:
    global_index: int
    file_index: int
    path: str
    start: int
    end: int
    line_start: int
    line_end: int
    text: str
    context: str


@dataclass(frozen=True)
class _ReadFile:
    path: str
    text: str
    sha256: str
    matches: list[_Match]
    result: dict[str, Any]


@dataclass(frozen=True)
class _LLMResponse:
    content: str
    finish_reason: str | None


type _LLMResponseLike = _LLMResponse | str


async def _call_llm(
    messages: list[dict[str, str]],
    llm_url: str,
    model: str,
    *,
    api_key: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = _MAX_LLM_TOKENS,
) -> _LLMResponse:
    """Call an OpenAI-compatible endpoint without discarding completion status."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0), trust_env=False, follow_redirects=False
    ) as client:
        resp = await client.post(
            f"{llm_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    choice = data["choices"][0]
    return _LLMResponse(
        content=choice["message"]["content"],
        finish_reason=choice.get("finish_reason"),
    )


def _normalize_path(path: str) -> str:
    normalized = posixpath.normpath(strip_redundant_workspace_prefix(path.strip()))
    return "." if normalized in ("", ".") else normalized


def _has_magic(pattern: str) -> bool:
    return any(ch in pattern for ch in _GLOB_MAGIC)


def _glob_root(pattern: str) -> str:
    parts = _normalize_path(pattern).split("/")
    root: list[str] = []
    for part in parts:
        if part == "**" or _has_magic(part):
            break
        root.append(part)
    return "/".join(root) if root else "."


def _glob_matches(path: str, pattern: str) -> bool:
    path_parts = [] if path in ("", ".") else path.split("/")
    pattern_parts = [] if pattern in ("", ".") else pattern.split("/")

    def match_parts(pp: list[str], gp: list[str]) -> bool:
        if not gp:
            return not pp
        head = gp[0]
        if head == "**":
            return match_parts(pp, gp[1:]) or bool(pp and match_parts(pp[1:], gp))
        return bool(pp and fnmatchcase(pp[0], head) and match_parts(pp[1:], gp[1:]))

    return match_parts(path_parts, pattern_parts)


async def _list_dir(ctx: ToolContext, path: str) -> list[str]:
    assert ctx.sandbox is not None
    return await ctx.sandbox.list_dir(path)


async def _glob_paths(
    ctx: ToolContext,
    root: str,
    pattern: str,
    *,
    max_results: int,
) -> list[str]:
    out: list[str] = []

    async def walk_dir(path: str, depth: int) -> None:
        if depth > 50 or len(out) > max_results:
            return
        try:
            entries = await _list_dir(ctx, path)
        except Exception:  # noqa: BLE001 - unreadable/non-directory roots simply yield no files.
            return
        for entry in entries:
            child = entry if path in ("", ".") else f"{path.rstrip('/')}/{entry}"
            if len(out) > max_results:
                return
            try:
                await _list_dir(ctx, child)
            except Exception:  # noqa: BLE001 - not a directory, or unreadable file.
                if _glob_matches(child, pattern):
                    out.append(child)
            else:
                await walk_dir(child, depth + 1)

    await walk_dir(root, 0)
    return out


async def _select_paths(args: FindAndEditArgs, ctx: ToolContext) -> tuple[list[str], list[str]]:
    selected: list[str] = []
    seen: set[str] = set()
    warnings: list[str] = []

    def add(path: str) -> None:
        normalized = _normalize_path(path)
        if normalized not in seen:
            seen.add(normalized)
            selected.append(normalized)

    for path in args.paths or []:
        add(path)

    if args.glob:
        pattern = _normalize_path(args.glob)
        if _has_magic(pattern):
            root = _glob_root(pattern)
            for path in await _glob_paths(
                ctx,
                root,
                pattern,
                max_results=args.max_files + 1,
            ):
                add(path)
                if len(selected) > args.max_files:
                    break
        else:
            add(pattern)

    if len(selected) > args.max_files:
        warnings.append(f"selected {len(selected)} files, exceeding max_files={args.max_files}")
    return selected, warnings


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _context_window(text: str, line_start: int, line_end: int) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(1, line_start - _CONTEXT_RADIUS_LINES)
    end = min(len(lines), line_end + _CONTEXT_RADIUS_LINES)
    width = len(str(end))
    return "\n".join(f"{line:>{width}}\t{lines[line - 1]}" for line in range(start, end + 1))


def _match_record(match: _Match) -> dict[str, Any]:
    return {
        "index": match.file_index,
        "global_index": match.global_index,
        "start": match.start,
        "end": match.end,
        "line_start": match.line_start,
        "line_end": match.line_end,
        "matched_text": match.text,
        "status": "pending",
    }


def _resolve_driver_llm(ctx: ToolContext) -> tuple[str, str, str | None, str | None]:
    if ctx.driver_llm is not None:
        base_url, model, api_key_env = ctx.driver_llm
        llm_url = base_url.rstrip("/")
        from disco.core.llm import ConfigStore
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

        store = ConfigStore()
        cfg = store.load()
        purpose = _purpose_for_model_endpoint(cfg, llm_url, api_key_env)
        if not store.origin_approved(llm_url, purpose, api_key_env):
            return "", model, None, "LLM origin not approved"
        if not secret_ref_allowed_for_origin(api_key_env, llm_url):
            return "", model, None, "LLM secret_ref not allowed"
        return llm_url, model, _resolve_llm_key(api_key_env), None
    llm_url, model, api_key = _resolve_slides_llm()
    if not llm_url:
        return "", model, None, "LLM origin not approved"
    return llm_url, model, api_key, None


def _decision_messages(args: FindAndEditArgs, match: _Match) -> list[dict[str, str]]:
    system = (
        "You are a precise code-editing assistant. Decide whether the exact regex "
        "match should be replaced to satisfy the instruction. Edit only the matched "
        'span, not the surrounding context. If uncertain, return {"action":"skip",'
        '"replacement":""}. Return ONLY a JSON object with keys "action" and '
        '"replacement".'
    )
    user = (
        f"Instruction:\n{args.instruction}\n\n"
        f"Regex pattern:\n{args.pattern}\n\n"
        f"File: {match.path}\n"
        f"Match lines: {match.line_start}-{match.line_end}\n\n"
        "Matched span as a JSON string:\n"
        f"{json.dumps(match.text)}\n\n"
        "Surrounding context with 1-based line numbers:\n"
        f"{match.context}\n\n"
        'Return {"action":"edit","replacement":"..."} to replace the matched span, '
        'or {"action":"skip","replacement":""} to leave it unchanged.'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _coerce_llm_response(value: _LLMResponseLike) -> _LLMResponse:
    """Keep legacy string-returning adapters honest about their unknown status."""

    if isinstance(value, _LLMResponse):
        return value
    if isinstance(value, str):
        return _LLMResponse(content=value, finish_reason=None)
    raise TypeError(f"LLM adapter returned unsupported response type {type(value).__name__}")


def _truncation_retry_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Request one fresh, complete decision without trusting cut JSON fragments."""

    return [
        *messages,
        {
            "role": "user",
            "content": (
                "The previous response was provider-truncated. Re-evaluate this exact "
                "match and return the complete JSON decision from the beginning. Return "
                "ONLY one complete JSON object; do not continue the cut fragment."
            ),
        },
    ]


def _parse_decision(raw: str, matched_text: str) -> tuple[str, str | None, str]:
    stripped = strip_think_spans(raw)
    if not stripped:
        return "skip", None, "empty_response"
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            data = json.loads(_extract_json_object(stripped))
        except json.JSONDecodeError:
            return "skip", None, "parse_failed"
    if not isinstance(data, dict):
        return "skip", None, "parse_failed"
    action = data.get("action")
    if action != "edit":
        return "skip", None, "action_skip" if action == "skip" else "action_not_edit"
    replacement = data.get("replacement")
    if not isinstance(replacement, str):
        return "skip", None, "missing_replacement"
    if replacement == "":
        return "skip", None, "empty_replacement"
    if replacement == matched_text:
        return "skip", None, "unchanged_replacement"
    return "edit", replacement, "accepted"


async def _decide_match(
    args: FindAndEditArgs,
    match: _Match,
    *,
    llm_url: str,
    model: str,
    api_key: str | None,
    sem: asyncio.Semaphore,
) -> tuple[int, dict[str, Any]]:
    messages = _decision_messages(args, match)
    saw_truncation = False
    async with sem:
        for attempt in range(_MAX_TRUNCATION_RETRIES + 1):
            try:
                response = _coerce_llm_response(
                    await _call_llm(
                        messages,
                        llm_url,
                        model,
                        api_key=api_key,
                        temperature=0.0,
                        max_tokens=_MAX_LLM_TOKENS,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - safe default is a per-match no-op.
                if saw_truncation:
                    return match.global_index, {
                        "status": "failed",
                        "reason": f"provider_truncation_retry_error:{type(exc).__name__}",
                        "finish_reason": "length",
                        "provider_calls": attempt + 1,
                        "retry_count": attempt,
                    }
                return match.global_index, {
                    "status": "skipped",
                    "reason": f"llm_error:{type(exc).__name__}",
                }

            finish_reason = (response.finish_reason or "").strip().lower()
            if finish_reason == "length":
                saw_truncation = True
                if attempt < _MAX_TRUNCATION_RETRIES:
                    messages = _truncation_retry_messages(messages)
                    continue
                return match.global_index, {
                    "status": "failed",
                    "reason": "provider_truncated",
                    "finish_reason": "length",
                    "provider_calls": attempt + 1,
                    "retry_count": attempt,
                }
            if finish_reason not in ("", "stop", "end_turn", "eos", "eos_token"):
                return match.global_index, {
                    "status": "failed",
                    "reason": f"provider_incomplete:{finish_reason}",
                    "finish_reason": finish_reason,
                    "provider_calls": attempt + 1,
                    "retry_count": attempt,
                }

            action, replacement, reason = _parse_decision(response.content, match.text)
            if saw_truncation and action != "edit" and reason != "action_skip":
                return match.global_index, {
                    "status": "failed",
                    "reason": f"provider_truncation_recovery_invalid:{reason}",
                    "finish_reason": finish_reason or None,
                    "provider_calls": attempt + 1,
                    "retry_count": attempt,
                }
            if action != "edit" or replacement is None:
                decision: dict[str, Any] = {"status": "skipped", "reason": reason}
            else:
                decision = {
                    "status": "accepted",
                    "reason": reason,
                    "replacement": replacement,
                }
            if saw_truncation:
                decision.update(
                    {
                        "recovered_from_truncation": True,
                        "provider_calls": attempt + 1,
                        "retry_count": attempt,
                    }
                )
            return match.global_index, decision

    raise AssertionError("bounded decision loop exhausted without a result")


def _mark_skipped(match_result: dict[str, Any], reason: str) -> None:
    match_result["status"] = "skipped"
    match_result["reason"] = reason
    match_result.pop("replacement", None)


def _prepare_exact_edits(
    file: _ReadFile,
    decisions: dict[int, dict[str, Any]],
) -> tuple[list[ExactReplaceEdit], bool]:
    matches_by_old: dict[str, list[_Match]] = {}
    edits_by_old: dict[str, list[_Match]] = {}
    for match in file.matches:
        matches_by_old.setdefault(match.text, []).append(match)
        decision = decisions.get(
            match.global_index,
            {"status": "skipped", "reason": "missing_decision"},
        )
        rec = file.result["matches"][match.file_index]
        rec.update(decision)
        if decision.get("status") == "accepted":
            edits_by_old.setdefault(match.text, []).append(match)

    # A provider-truncated decision makes the requested edit set incomplete. Keep
    # the whole file byte-identical instead of applying a silently partial subset.
    if any(match_result.get("status") == "failed" for match_result in file.result["matches"]):
        for match_result in file.result["matches"]:
            if match_result.get("status") == "accepted":
                _mark_skipped(match_result, "file_decision_failed")
        return [], False

    edits: list[ExactReplaceEdit] = []
    multi = False
    for old_string, accepted_matches in edits_by_old.items():
        replacements = {str(decisions[m.global_index].get("replacement")) for m in accepted_matches}
        occurrences = len(_all_occurrences(file.text, old_string))
        all_regex_matches = matches_by_old.get(old_string, [])
        if (
            len(replacements) != 1
            or occurrences != len(all_regex_matches)
            or len(accepted_matches) != len(all_regex_matches)
        ):
            for match in accepted_matches:
                _mark_skipped(
                    file.result["matches"][match.file_index],
                    "duplicate_match_ambiguous",
                )
            continue
        replacement = next(iter(replacements))
        edits.append(ExactReplaceEdit(old_string=old_string, new_string=replacement))
        if occurrences > 1:
            multi = True
    return edits, multi


async def _apply_file_edits(
    file: _ReadFile,
    ctx: ToolContext,
    decisions: dict[int, dict[str, Any]],
) -> int:
    edits, multi = _prepare_exact_edits(file, decisions)
    if not edits:
        if any(match.get("status") == "failed" for match in file.result["matches"]):
            file.result["status"] = "failed"
            file.result["reason"] = "decision_failed"
        else:
            file.result["status"] = "unchanged"
        return 0

    exact_args = ExactReplaceArgs(
        path=file.path,
        edits=edits,
        expected_sha256=file.sha256,
        multi=multi,
    )
    outcome = await ExactReplaceTool().run(exact_args, ctx)
    accepted_results = [m for m in file.result["matches"] if m.get("status") == "accepted"]
    if not outcome.success:
        reason = f"exact_replace_failed:{outcome.error or 'unknown'}"
        file.result["status"] = "skipped"
        file.result["apply_error"] = outcome.error or outcome.content
        for match_result in accepted_results:
            _mark_skipped(match_result, reason)
        return 0

    edited = 0
    for match_result in accepted_results:
        match_result["status"] = "edited"
        edited += 1
    file.result["status"] = "edited" if edited else "unchanged"
    file.result["applied_edits"] = len(edits)
    return edited


class FindAndEditTool:
    definition = ToolDef(
        name="find_and_edit",
        description=(
            "Find regex matches across selected workspace files and ask the driver LLM "
            "whether to replace each exact matched span. Ambiguous or unparsable decisions "
            "are safe no-ops; provider truncation is retried once and surfaced explicitly; "
            "accepted edits are applied atomically per file."
        ),
        args_model=FindAndEditArgs,
        needs=_FS,
        base_risk=SecurityRisk.MEDIUM,
        runs_in="sandbox",
        read_only=False,
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: FindAndEditArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            regex = re.compile(args.pattern)
        except re.error as exc:
            return ToolOutcome(
                success=False,
                error="FIND_AND_EDIT_INVALID_PATTERN",
                content=f"find_and_edit: invalid regex pattern: {exc}",
                structured={"kind": "invalid_pattern", "error": str(exc)},
            )

        selected, warnings = await _select_paths(args, ctx)
        if len(selected) > args.max_files:
            return ToolOutcome(
                success=False,
                error="FIND_AND_EDIT_MAX_FILES_EXCEEDED",
                content=(
                    f"find_and_edit refused: selected {len(selected)} files, "
                    f"exceeding max_files={args.max_files}."
                ),
                structured={
                    "kind": "max_files_exceeded",
                    "selected_files": len(selected),
                    "max_files": args.max_files,
                    "warnings": warnings,
                },
            )

        files: list[_ReadFile] = []
        file_results: list[dict[str, Any]] = []
        global_matches: list[_Match] = []
        for path in selected:
            result: dict[str, Any] = {"path": path, "status": "pending", "matches": []}
            file_results.append(result)
            try:
                raw = await ctx.sandbox.read_file(path)
            except Exception as exc:  # noqa: BLE001 - unreadable files are recorded skips.
                result["status"] = "skipped"
                result["reason"] = "read_error"
                result["error"] = f"{type(exc).__name__}: {exc}"
                continue
            sha = hashlib.sha256(raw).hexdigest()
            text = raw.decode("utf-8", errors="replace")
            total_lines = max(1, len(text.splitlines()))
            mark_read(ctx.conversation_id, path)
            record_read(
                ctx.conversation_id,
                path,
                sha=sha,
                start_line=1,
                end_line=total_lines,
                full=True,
            )
            file_matches: list[_Match] = []
            for re_match in regex.finditer(text):
                matched_text = re_match.group(0)
                result_index = len(result["matches"])
                if matched_text == "":
                    rec = {
                        "index": result_index,
                        "global_index": len(global_matches),
                        "start": re_match.start(),
                        "end": re_match.end(),
                        "line_start": _line_for_offset(text, re_match.start()),
                        "line_end": _line_for_offset(text, re_match.end()),
                        "matched_text": matched_text,
                        "status": "skipped",
                        "reason": "zero_length_match",
                    }
                    result["matches"].append(rec)
                    continue
                line_start = _line_for_offset(text, re_match.start())
                line_end = _line_for_offset(text, re_match.end())
                match = _Match(
                    global_index=len(global_matches),
                    file_index=result_index,
                    path=path,
                    start=re_match.start(),
                    end=re_match.end(),
                    line_start=line_start,
                    line_end=line_end,
                    text=matched_text,
                    context=_context_window(text, line_start, line_end),
                )
                file_matches.append(match)
                global_matches.append(match)
                result["matches"].append(_match_record(match))
            result["sha256"] = sha
            result["status"] = "matched" if file_matches else "unchanged"
            files.append(
                _ReadFile(
                    path=path,
                    text=text,
                    sha256=sha,
                    matches=file_matches,
                    result=result,
                )
            )

        if len(global_matches) > args.max_matches:
            return ToolOutcome(
                success=False,
                error="FIND_AND_EDIT_MAX_MATCHES_EXCEEDED",
                content=(
                    f"find_and_edit refused: found {len(global_matches)} matches, "
                    f"exceeding max_matches={args.max_matches}; no LLM calls were made."
                ),
                structured={
                    "kind": "max_matches_exceeded",
                    "scanned_files": len(selected),
                    "matches": len(global_matches),
                    "max_matches": args.max_matches,
                    "files": file_results,
                    "warnings": warnings,
                },
            )

        llm_url, model, api_key, llm_error = _resolve_driver_llm(ctx)
        if llm_error is not None:
            return ToolOutcome(
                success=False,
                error=llm_error,
                content=f"find_and_edit refused: {llm_error}; no LLM calls were made.",
                structured={
                    "kind": "llm_not_approved",
                    "scanned_files": len(selected),
                    "matches": len(global_matches),
                    "files": file_results,
                    "warnings": warnings,
                },
            )
        sem = asyncio.Semaphore(_CONCURRENCY)
        decision_pairs = await asyncio.gather(
            *(
                _decide_match(
                    args,
                    match,
                    llm_url=llm_url,
                    model=model,
                    api_key=api_key,
                    sem=sem,
                )
                for match in global_matches
            )
        )
        decisions = dict(decision_pairs)

        edited = 0
        artifacts: list[str] = []
        for file in files:
            file_edited = await _apply_file_edits(file, ctx, decisions)
            edited += file_edited
            if file_edited:
                artifacts.append(file.path)

        skipped = sum(
            1
            for file in file_results
            for match in file.get("matches", [])
            if match.get("status") == "skipped"
        )
        failed = sum(
            1
            for file in file_results
            for match in file.get("matches", [])
            if match.get("status") == "failed"
        )
        failure_label = "decision failure" if failed == 1 else "decision failures"
        content = (
            f"find_and_edit: scanned {len(selected)} files, {len(global_matches)} matches, "
            f"{edited} edited, {skipped} skipped, {failed} {failure_label}."
        )
        return ToolOutcome(
            success=True,
            content=content,
            artifacts=artifacts,
            structured={
                "kind": "completed_with_decision_failures" if failed else "completed",
                "scanned_files": len(selected),
                "read_files": len(files),
                "matches": len(global_matches),
                "edited": edited,
                "skipped": skipped,
                "decision_failures": failed,
                "files": file_results,
                "warnings": warnings,
            },
        )


__all__ = ["FindAndEditArgs", "FindAndEditTool"]
