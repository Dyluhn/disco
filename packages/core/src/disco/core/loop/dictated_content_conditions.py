"""Dictated-content condition extraction from the append-only event log.

Extracted from ``plan_conditions.py`` as bounded pure collaborators.  These
helpers bind quoted USER literals to the PlanEvent revision that serves them.
One implementation owner (this module); ``plan_conditions.py`` re-exports the
public names.  No duplicated policy.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from ..events import (
    Event,
    EventSource,
    MessageEvent,
    PlanEvent,
    StatusEvent,
)
from ..selection_edit import is_scoped_edit_directive


@dataclass(frozen=True)
class DictatedContentCondition:
    """A quoted user literal that must survive into the final deliverable.

    This is intentionally derived from the event log instead of persisted as a
    separate mutable store row: replaying USER MessageEvents + PlanEvents
    reconstructs the same requirements after resume/condensation.
    """

    revision: int
    literal: str
    source_event_id: str
    source_seq: int | None = None
    requirement_slot: str | None = None
    supersedes_source_event_id: str | None = None
    document_artifact: str | None = None


@dataclass(frozen=True)
class _DictatedContentLiteral:
    literal: str
    quote_start: int
    requirement_slot: str | None
    replaces_slot: bool
    document_artifact: str | None = None


_QUOTED_LITERAL_RE = re.compile(r"(?<!\w)'([^'\n]{2,80})'(?!\w)|(?<!\w)\"([^\"\n]{2,80})\"(?!\w)")
_FILE_LIKE_LITERAL_RE = re.compile(
    r"^(?:[\w.-]+\.(?:html?|css|mjs|cjs|jsx?|tsx?|py|md|json|ya?ml|txt|csv|"
    r"png|jpe?g|gif|webp|svg|pdf|docx?|xlsx?|pptx?)|\.env(?:\.[\w.-]+)?)$",
    re.IGNORECASE,
)
_SHELL_COMMAND_WORDS = frozenset(
    {
        "ag", "bun", "cat", "cd", "chmod", "chown", "cp", "curl", "deno",
        "docker", "echo", "git", "grep", "head", "ls", "make", "mkdir",
        "mv", "node", "npm", "npx", "pnpm", "pytest", "python", "python3",
        "rm", "ruff", "sed", "sh", "tail", "tox", "uv", "vite", "yarn",
    }
)
_SHELL_OPERATOR_RE = re.compile(r"(?:^|\s)(?:&&|\|\||[|;<>])(?:\s|$)|`|\$\(")
_SERVE_METADATA_PREFIX_RE = re.compile(
    r"\b(?:call|invoke|use)\b[^.!?]{0,240}\bserve\b[^.!?]{0,160}"
    r"\b(?:path|title)\s*$",
    re.IGNORECASE,
)
_DIAGNOSTIC_PROTOCOL_HEADER_RE = re.compile(r"(?im)^\s*DIAGNOSTIC\s+PROTOCOL\b")
_DIAGNOSTIC_PROTOCOL_TASK_RE = re.compile(r"(?im)^\s*TASK\s*:")
_DIAGNOSTIC_PROTOCOL_LITERAL_RE = re.compile(r"^(?:PREDICT|OBSERVED)\s*:", re.IGNORECASE)
_APPLICATION_TITLE_DECLARATION_RE = re.compile(
    r"\b(?:app(?:lication)?|site)\s+"
    r"(?:(?:is|should\s+be|must\s+be)\s+)?"
    r"(?:titled|named|called)\s+(?:exactly\s+)?$",
    re.IGNORECASE,
)
_BUILT_TARGET_TITLE_DECLARATION_RE = re.compile(
    r"\b(?:build|create|make)\s+"
    r"(?!(?:[^.!?]{0,160})\b(?:with|containing|including|having|whose|where)\b)"
    r"[^.!?]{1,160}\b(?:titled|named|called)\s+(?:exactly\s+)?$",
    re.IGNORECASE,
)
_APPLICATION_TITLE_REPLACEMENT_RE = re.compile(
    r"(?:"
    r"\b(?:change|set|update)\s+(?:the\s+)?"
    r"(?:(?:app(?:lication)?|site)\s+)?(?:title|name)\s+(?:to|as)"
    r"|\b(?:rename|retitle)\s+(?:the\s+)?(?:app(?:lication)?|site)(?:\s+(?:to|as))?"
    r")\s+(?:exactly\s+)?$",
    re.IGNORECASE,
)
_NAMED_CONTENT_COMPONENT_RE = re.compile(
    r"\b(?P<name>[a-z][\w-]{1,48})\s+"
    r"(?P<kind>panel|banner|heading|button|card|section|badge|label)\b",
    re.IGNORECASE,
)
_CONTENT_ASSIGNMENT_RE = re.compile(
    r"\b(?:containing|with|showing|saying|say|text|labelled|labeled)\s+"
    r"(?:exactly\s+)?$",
    re.IGNORECASE,
)
_DOCUMENT_ARTIFACT_RE = re.compile(
    r"\b([\w./-]+\.(?:md|markdown|txt|rst|csv|tsv|json|ya?ml|log|ini|toml))\b",
    re.IGNORECASE,
)


def _has_unspaced_slash(text: str) -> bool:
    for idx, ch in enumerate(text):
        if ch != "/":
            continue
        before = text[idx - 1] if idx > 0 else ""
        after = text[idx + 1] if idx + 1 < len(text) else ""
        if not (before.isspace() and after.isspace()):
            return True
    return False


def _looks_like_shell_command(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if s.startswith("$ "):
        return True
    if _SHELL_OPERATOR_RE.search(s):
        return True
    try:
        parts = shlex.split(s)
    except ValueError:
        parts = s.split()
    if not parts:
        return False
    first = parts[0].rsplit("/", 1)[-1].lower()
    return first in _SHELL_COMMAND_WORDS


def _skip_dictated_literal(text: str) -> bool:
    s = text.strip()
    if len(s) != len(text) or not (2 <= len(s) <= 80):
        return True
    if _has_unspaced_slash(s):
        return True
    if _FILE_LIKE_LITERAL_RE.match(s):
        return True
    return _looks_like_shell_command(s)


def _mask_quoted_spans(text: str) -> str:
    """Blank out quoted spans, preserving length so offsets stay comparable."""
    return re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", lambda match: " " * len(match.group(0)), text)


_CALL_PREFIX_RE = re.compile(r"[\w.\]\)]\($")


def _is_code_expression_literal(instruction: str, quote_start: int) -> bool:
    """A quoted literal that is an ARGUMENT of a code call the user dictated.

    Counted wave-1 FAIL 2026-08-06 (`diag_devserver` seed 8,
    TOOL_CALL_THRASH_IDENTICAL_STREAK). The prompt dictated the server's port
    lookup verbatim — ``int(os.environ.get('PORT', '8000'))`` — and both
    arguments were mined as REQUIRED visible-text claims on the rendered page.
    A page that says 'Live Server Up' can never also show 'PORT', so the host
    verifier failed a claim no correct implementation could satisfy, refused
    identically, and the model's re-probing was graded as thrash.

    Same family as the `application.title` slot collapse (seed 406431) and the
    `document_artifact` guard (seed 460009): a literal the user assigned to
    something OTHER than the served page must not become page content. Here the
    "something other" is a code expression, and the syntactic evidence is exact
    — the literal sits inside an unclosed call parenthesis whose opener is bound
    tight to an identifier (``get(``), never an English parenthetical (``page (``).
    """

    window = instruction[max(0, quote_start - 240) : quote_start]
    # Mask sibling literals so an earlier argument cannot lend this one its
    # parenthesis: in `get('PORT', '8000')` the evidence for '8000' must come
    # from `get(`, not from the quotes of 'PORT'.
    masked = _mask_quoted_spans(window)
    depth = 0
    for idx in range(len(masked) - 1, -1, -1):
        char = masked[idx]
        if char == "\n":
            break
        if char == ")":
            depth += 1
        elif char == "(":
            if depth:
                depth -= 1
                continue
            return _CALL_PREFIX_RE.search(masked[: idx + 1]) is not None
    return False


def _is_serve_metadata_literal(instruction: str, quote_start: int) -> bool:
    prefix = instruction[max(0, quote_start - 480) : quote_start]
    return _SERVE_METADATA_PREFIX_RE.search(_mask_quoted_spans(prefix)) is not None


def _is_diagnostic_protocol_metadata_literal(
    instruction: str, quote_start: int, literal: str,
) -> bool:
    if _DIAGNOSTIC_PROTOCOL_LITERAL_RE.match(literal) is None:
        return False
    headers = list(_DIAGNOSTIC_PROTOCOL_HEADER_RE.finditer(instruction, 0, quote_start))
    if not headers:
        return False
    section_start = headers[-1].start()
    task = _DIAGNOSTIC_PROTOCOL_TASK_RE.search(instruction, section_start)
    return task is not None and quote_start < task.start()


def _dictated_content_literal_slot(text: str, quote_start: int) -> tuple[str | None, bool]:
    prefix = text[max(0, quote_start - 320) : quote_start]
    sentence = re.split(r"[.!?]", prefix)[-1]
    assignment = _CONTENT_ASSIGNMENT_RE.search(sentence)
    components = (
        list(_NAMED_CONTENT_COMPONENT_RE.finditer(sentence, 0, assignment.start()))
        if assignment is not None
        else []
    )
    named_slot = components[-1] if components else None
    if named_slot is not None:
        # Explicitly named UI/content slots are singletons. A later revision
        # that dictates the same slot replaces its prior copy; distinct named
        # slots continue to accumulate as independent requirements.
        slot = f"component:{named_slot['name'].lower()}:{named_slot['kind'].lower()}"
        return slot, True
    if _APPLICATION_TITLE_REPLACEMENT_RE.search(sentence):
        return "application.title", True
    if _APPLICATION_TITLE_DECLARATION_RE.search(
        sentence
    ) or _BUILT_TARGET_TITLE_DECLARATION_RE.search(sentence):
        return "application.title", False
    return None, False


def _dictated_content_literal_document(text: str, quote_start: int) -> str | None:
    prefix = text[max(0, quote_start - 320) : quote_start]
    clause = re.split(r"[.!?]\s|;\s*|\s+and\s+|\s+then\s+|\s+but\s+", prefix)[-1]
    matches = _DOCUMENT_ARTIFACT_RE.findall(clause)
    return matches[-1] if matches else None


def _dictated_content_literals(text: str) -> list[_DictatedContentLiteral]:
    out: list[_DictatedContentLiteral] = []
    seen: set[str] = set()
    for mt in _QUOTED_LITERAL_RE.finditer(text or ""):
        literal = mt.group(1) if mt.group(1) is not None else mt.group(2)
        if (
            literal is None
            or _skip_dictated_literal(literal)
            or _is_serve_metadata_literal(text, mt.start())
            or _is_diagnostic_protocol_metadata_literal(text, mt.start(), literal)
            or _is_code_expression_literal(text, mt.start())
        ):
            continue
        if literal in seen:
            continue
        seen.add(literal)
        slot, replaces = _dictated_content_literal_slot(text, mt.start())
        out.append(
            _DictatedContentLiteral(
                literal=literal,
                quote_start=mt.start(),
                requirement_slot=slot,
                replaces_slot=replaces,
                document_artifact=_dictated_content_literal_document(text, mt.start()),
            )
        )
    return out


def extract_dictated_content_literals(text: str) -> list[str]:
    """Extract quoted user-authored content literals from a build instruction."""
    return [candidate.literal for candidate in _dictated_content_literals(text)]


def _scoped_edit_user_messages(events: list[Event]) -> list[MessageEvent]:
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and is_scoped_edit_directive(e.message.content or "")
    ]


def _superseded_by_scoped_edit(
    condition: DictatedContentCondition, scoped_edits: list[MessageEvent]
) -> bool:
    if condition.source_seq is None:
        return False
    for edit in scoped_edits:
        if edit.seq is None or edit.seq <= condition.source_seq:
            continue
        if condition.literal in (edit.message.content or ""):
            return True
    return False


def _drop_scoped_edit_superseded_conditions(
    conditions: list[DictatedContentCondition], events: list[Event]
) -> list[DictatedContentCondition]:
    scoped_edits = _scoped_edit_user_messages(events)
    if not scoped_edits:
        return conditions
    return [c for c in conditions if not _superseded_by_scoped_edit(c, scoped_edits)]


def _planning_idx_for_plan(
    indexed: list[tuple[int, Event]], prev_plan_idx: int, plan_idx: int
) -> int | None:
    """Find the planning StatusEvent index for a plan window."""
    for idx, e in indexed:
        if idx <= prev_plan_idx or idx > plan_idx:
            continue
        if isinstance(e, StatusEvent) and e.detail == "planning":
            return idx
    return None


def _user_messages_for_plan(
    indexed: list[tuple[int, Event]], prev_plan_idx: int, upper_idx: int, revision: int
) -> list[MessageEvent]:
    """Collect USER messages in the plan window, filtered for revision > 1."""
    users = [
        e
        for idx, e in indexed
        if prev_plan_idx < idx <= upper_idx
        and isinstance(e, MessageEvent)
        and e.source == EventSource.USER
    ]
    if revision > 1:
        from . import signals

        users = [e for e in users if signals.is_revision_intent(e.message.content or "")]
    return users


def _collect_replacement_counts(
    candidates: list[_DictatedContentLiteral],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.replaces_slot and candidate.requirement_slot is not None:
            counts[candidate.requirement_slot] = (
                counts.get(candidate.requirement_slot, 0) + 1
            )
    return counts


def _build_condition_from_candidate(
    candidate: _DictatedContentLiteral,
    plan: PlanEvent,
    user: MessageEvent,
    out: list[DictatedContentCondition],
    replacement_counts: dict[str, int],
) -> DictatedContentCondition:
    """Build a DictatedContentCondition, handling slot replacement."""
    supersedes_source_event_id: str | None = None
    if (
        candidate.replaces_slot
        and candidate.requirement_slot is not None
        and replacement_counts.get(candidate.requirement_slot) == 1
    ):
        prior = [
            (index, condition)
            for index, condition in enumerate(out)
            if condition.requirement_slot == candidate.requirement_slot
        ]
        if len(prior) == 1:
            prior_index, prior_condition = prior[0]
            supersedes_source_event_id = prior_condition.source_event_id
            out.pop(prior_index)
    return DictatedContentCondition(
        revision=plan.revision,
        literal=candidate.literal,
        source_event_id=user.id,
        source_seq=user.seq,
        requirement_slot=candidate.requirement_slot,
        supersedes_source_event_id=supersedes_source_event_id,
        document_artifact=candidate.document_artifact,
    )


def dictated_content_conditions_from_events(
    events: list[Event],
) -> list[DictatedContentCondition]:
    """Bind quoted USER literals to the PlanEvent revision that serves them.

    For the initial plan, every USER instruction before the first plan can
    contribute literals. For later revisions, only mutating/revision-intent USER
    messages in the revision window contribute, which avoids turning a quoted
    Q&A phrase into a deliverable requirement for a later change.
    """
    indexed = list(enumerate(events))
    plans = [(idx, e) for idx, e in indexed if isinstance(e, PlanEvent)]
    out: list[DictatedContentCondition] = []
    seen: set[tuple[int, str]] = set()
    prev_plan_idx = -1

    for plan_idx, plan in plans:
        planning_idx = _planning_idx_for_plan(indexed, prev_plan_idx, plan_idx)
        upper_idx = planning_idx if planning_idx is not None else plan_idx
        users = _user_messages_for_plan(indexed, prev_plan_idx, upper_idx, plan.revision)
        for user in users:
            content = user.message.content or ""
            if is_scoped_edit_directive(content):
                continue
            candidates = _dictated_content_literals(content)
            replacement_counts = _collect_replacement_counts(candidates)
            for candidate in candidates:
                key = (plan.revision, candidate.literal)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    _build_condition_from_candidate(
                        candidate, plan, user, out, replacement_counts
                    )
                )
        prev_plan_idx = plan_idx
    return _drop_scoped_edit_superseded_conditions(out, events)
