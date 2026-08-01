"""Dedicated file tools — tool-sandbox-contract.md §9 [OH: file_rules]. — compatibility facade.

Dedicated file tools, NOT shell redirection — this sidesteps the string-escaping
failures of piping model output through bash. All operate within the sandbox
instance's jailed workspace (the instance rejects path escapes).

The cohesive implementation lives in the private ``files_parts`` package: shared
state/canonicalization/guard helpers, and one module per tool (``file_read``,
``file_write``, ``file_append``, ``file_list``, ``file_edit``,
``file_replace_lines``, ``file_insert_lines``, ``file_str_replace``,
``exact_replace``, ``safe_write_file``). This module is a state-free
compatibility/export facade: it re-imports every public AND private name this
module exposed before the split, so every existing import path (production
code — including ``disco.tools.builtin.files.clear_conversation_read_state``,
``disco.tools.builtin.files._canonical``, and
``disco.tools.builtin.files._atomic_write`` — and the test suite, some of which
reach private helpers through this module object) keeps working unchanged.
"""

from __future__ import annotations

import hashlib as hashlib
import json as json
import re as re
from typing import Any as Any

from disco.core.effects import (
    CoverageSpan as CoverageSpan,
)
from disco.core.effects import (
    CoverageUnit as CoverageUnit,
)
from disco.core.effects import (
    EffectCapability as EffectCapability,
)
from disco.core.effects import (
    MutationReceipt as MutationReceipt,
)
from disco.core.effects import (
    ObservationReceipt as ObservationReceipt,
)
from disco.core.effects import (
    ResourceCoverage as ResourceCoverage,
)
from disco.core.effects import (
    ResourceKey as ResourceKey,
)
from disco.core.effects import (
    ResourceRevision as ResourceRevision,
)
from disco.core.effects import (
    ToolBehavior as ToolBehavior,
)
from pydantic import BaseModel as BaseModel
from pydantic import Field as Field

from ..anatomy import Capability as Capability
from ..anatomy import ToolContext as ToolContext
from ..anatomy import ToolDef as ToolDef
from ..anatomy import ToolOutcome as ToolOutcome
from ..behavior import declares as declares
from ..sandbox.base import (
    strip_redundant_workspace_prefix as strip_redundant_workspace_prefix,
)

# --- per-tool modules (Args model + Tool class, and any tool-local helper) ---
from .files_parts._append_tool import FileAppendArgs as FileAppendArgs
from .files_parts._append_tool import FileAppendTool as FileAppendTool

# ---------------------------------------------------------------------------
# Compatibility re-exports — every name below is implemented in `files_parts`
# and re-imported here so this module keeps re-exporting the exact same
# surface it always has, for both external importers and internal call sites
# that resolve a moved helper by its bare name (an unqualified name resolves
# via the CALLING function's own module globals, so a `files_parts` module
# calling e.g. `guard_fresh_edit` keeps working because that name is bound
# into ITS OWN module namespace by its own import — not by this facade).
# ---------------------------------------------------------------------------
from .files_parts._canonical import _canonical as _canonical
from .files_parts._constants import (
    _BINARY_DELIVERABLE_EXTS as _BINARY_DELIVERABLE_EXTS,
)
from .files_parts._constants import (
    _EDIT_ELISION_RE as _EDIT_ELISION_RE,
)
from .files_parts._constants import (
    _FILE_WRITE_SUCCESS_HEAD_LINES as _FILE_WRITE_SUCCESS_HEAD_LINES,
)
from .files_parts._constants import (
    _FS as _FS,
)
from .files_parts._constants import (
    _GUARD_FRESH_READ_MIN_BYTES as _GUARD_FRESH_READ_MIN_BYTES,
)
from .files_parts._constants import (
    _LINE_REFUSAL_WINDOW_RADIUS as _LINE_REFUSAL_WINDOW_RADIUS,
)
from .files_parts._constants import (
    _LINENO_PREFIX as _LINENO_PREFIX,
)
from .files_parts._constants import (
    _PRESSURE_DIRECTIVE as _PRESSURE_DIRECTIVE,
)
from .files_parts._constants import (
    _PRESSURE_FILE_THRESHOLD as _PRESSURE_FILE_THRESHOLD,
)
from .files_parts._constants import (
    _PRESSURE_HEAD_BUDGET as _PRESSURE_HEAD_BUDGET,
)
from .files_parts._constants import (
    _READ_CHAR_BUDGET as _READ_CHAR_BUDGET,
)
from .files_parts._constants import (
    _REFUSAL_READ_FULL_MAX_BYTES as _REFUSAL_READ_FULL_MAX_BYTES,
)
from .files_parts._constants import (
    _UPDATED_REGION_WINDOW_RADIUS as _UPDATED_REGION_WINDOW_RADIUS,
)
from .files_parts._edit_tool import FileEditArgs as FileEditArgs
from .files_parts._edit_tool import FileEditTool as FileEditTool
from .files_parts._edit_tool import _forgiving_replace as _forgiving_replace
from .files_parts._edit_tool import _nearest_anchor as _nearest_anchor
from .files_parts._elision import _has_elision_marker as _has_elision_marker
from .files_parts._exact_replace_tool import ExactReplaceArgs as ExactReplaceArgs
from .files_parts._exact_replace_tool import ExactReplaceEdit as ExactReplaceEdit
from .files_parts._exact_replace_tool import ExactReplaceTool as ExactReplaceTool
from .files_parts._exact_replace_tool import _all_occurrences as _all_occurrences
from .files_parts._fresh_edit_guard import guard_fresh_edit as guard_fresh_edit
from .files_parts._fuzzy_match import (
    _best_fuzzy_old_match_lines as _best_fuzzy_old_match_lines,
)
from .files_parts._fuzzy_match import (
    _find_text_lines as _find_text_lines,
)
from .files_parts._fuzzy_match import (
    _matched_old_lines as _matched_old_lines,
)
from .files_parts._governed import _GOVERNED_ROUTING as _GOVERNED_ROUTING
from .files_parts._governed import (
    _HARNESS_BOOKKEEPING_MESSAGE as _HARNESS_BOOKKEEPING_MESSAGE,
)
from .files_parts._governed import _governed_guard as _governed_guard
from .files_parts._governed import _governed_relpath as _governed_relpath
from .files_parts._governed import _governed_route_text as _governed_route_text
from .files_parts._governed import _is_governed_artifact as _is_governed_artifact
from .files_parts._governed import _route_for_governed as _route_for_governed
from .files_parts._insert_lines_tool import FileInsertLinesArgs as FileInsertLinesArgs
from .files_parts._insert_lines_tool import FileInsertLinesTool as FileInsertLinesTool
from .files_parts._list_tool import FileListArgs as FileListArgs
from .files_parts._list_tool import FileListTool as FileListTool
from .files_parts._mutation import _atomic_write as _atomic_write
from .files_parts._mutation import _commit_file_deletion as _commit_file_deletion
from .files_parts._mutation import _commit_file_mutation as _commit_file_mutation
from .files_parts._mutation import _file_mutation_receipt as _file_mutation_receipt
from .files_parts._mutation import _gated_write as _gated_write
from .files_parts._mutation import _resolved_workspace_file as _resolved_workspace_file
from .files_parts._mutation import _syntax_errors as _syntax_errors
from .files_parts._mutation import _workspace_file_revision as _workspace_file_revision
from .files_parts._mutation import (
    _write_artifact_structured as _write_artifact_structured,
)
from .files_parts._read_state import _clear_grounding as _clear_grounding
from .files_parts._read_state import _conv_state as _conv_state
from .files_parts._read_state import _covers as _covers
from .files_parts._read_state import (
    _increment_no_op_edit_count as _increment_no_op_edit_count,
)
from .files_parts._read_state import (
    _increment_old_text_not_found_count as _increment_old_text_not_found_count,
)
from .files_parts._read_state import _read_state as _read_state
from .files_parts._read_state import (
    clear_conversation_read_state as clear_conversation_read_state,
)
from .files_parts._read_state import mark_read as mark_read
from .files_parts._read_state import record_read as record_read
from .files_parts._read_state import reset_read_tracker as reset_read_tracker
from .files_parts._read_tool import FileReadArgs as FileReadArgs
from .files_parts._read_tool import FileReadTool as FileReadTool
from .files_parts._read_tool import _file_read_receipt as _file_read_receipt
from .files_parts._refusal_views import _fresh_read_required as _fresh_read_required
from .files_parts._refusal_views import _line_refusal_read as _line_refusal_read
from .files_parts._refusal_views import _line_span as _line_span
from .files_parts._refusal_views import _no_op_edit_refusal as _no_op_edit_refusal
from .files_parts._refusal_views import _no_op_write_refusal as _no_op_write_refusal
from .files_parts._refusal_views import _numbered_line_window as _numbered_line_window
from .files_parts._refusal_views import (
    _old_text_not_found_refusal as _old_text_not_found_refusal,
)
from .files_parts._refusal_views import (
    _with_line_refusal_read as _with_line_refusal_read,
)
from .files_parts._replace_lines_tool import (
    FileReplaceLinesArgs as FileReplaceLinesArgs,
)
from .files_parts._replace_lines_tool import (
    FileReplaceLinesTool as FileReplaceLinesTool,
)
from .files_parts._safe_write_tool import SafeWriteFileArgs as SafeWriteFileArgs
from .files_parts._safe_write_tool import SafeWriteFileTool as SafeWriteFileTool
from .files_parts._str_replace_tool import FileStrReplaceArgs as FileStrReplaceArgs
from .files_parts._str_replace_tool import FileStrReplaceTool as FileStrReplaceTool
from .files_parts._str_replace_tool import _occurrence_lines as _occurrence_lines
from .files_parts._success_view import (
    _bounded_updated_region_view as _bounded_updated_region_view,
)
from .files_parts._success_view import _post_change_line_span as _post_change_line_span
from .files_parts._success_view import (
    _record_success_grounding as _record_success_grounding,
)
from .files_parts._success_view import (
    _updated_region_success_content as _updated_region_success_content,
)
from .files_parts._text_norm import _norm_ws as _norm_ws
from .files_parts._text_norm import _number_lines as _number_lines
from .files_parts._text_norm import _strip_line_numbers as _strip_line_numbers
from .files_parts._write_tool import FileWriteArgs as FileWriteArgs
from .files_parts._write_tool import FileWriteTool as FileWriteTool
