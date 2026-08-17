"""Actionless honest-unverifiable-static finish helpers — shell/verify-step
command classification. Extracted verbatim from `finish/common.py` (module
logical LOC reduction); pure, stdlib + imported types only.
"""

from __future__ import annotations

import re

from ....events import ActionEvent

# ---- Bug 6: actionless honest-unverifiable-static finish helpers ------------
# The existing honest-unverifiable finish (`_maybe_honest_unverifiable_static_finish`)
# only fires at the FINISH GATE. When the plan's verify step is unsatisfiable on a
# browserless backend the model never reaches that gate — it churns and the actionless
# valve PAUSES it. These pure helpers feed the SAME honest-finish concept at the
# actionless valve (RCA option 3a), under conservative guards so a missing/broken
# deliverable or a genuine web failure never converts to a success (W-45 preserved).

# Tools that mutate the static deliverable on disk (mirror of `_is_web_deliverable`).
_FILE_WRITE_TOOLS = frozenset(
    {"file_write", "file_edit", "file_append", "file_replace_lines", "file_insert_lines"}
)
# Shell tools that can run a non-browser (HTMLParser/static-parse) validation.
_SHELL_TOOLS = frozenset({"shell", "shell_exec"})
# Verify-only step classification — STRUCTURAL, not a word list. The recurring
# false-positive class is a verification WORD appearing as a CONTENT noun: test→
# testimonials, render→product renders, validation→input validation, lint→lint config,
# check→checkout. Whack-a-moling individual words never converges, so a step is
# "verify-only" iff BOTH hold:
#   (A) it has a verification-ACTION framing (a verify verb acting on the deliverable,
#       or a clear verification-outcome phrase) — `_VERIFY_ACTION_RE`; AND
#   (B) it has NO creation/content verb at all — `_CONTENT_VERB_RE` is a hard NEGATIVE
#       OVERRIDE: a step that adds/creates/builds/sets-up/etc. is content work, never
#       verification, even when it also contains a verify-ish word ("Add input
#       validation", "Set up linting").
# Bare nouns alone (`validation`, `lint`, `render`, `test`, `check`) never match — only
# the action framing in (A) does. When in doubt → NON-verify (stay paused, the safe
# choice that can never false-finish real work).
_CONTENT_VERB_RE = re.compile(
    r"\b(?:add(?:s|ed|ing)?|create(?:s|d)?|creating|build(?:s|ing)?|built"
    r"|implement(?:s|ed|ing)?|writ(?:e|es|ing)|wrote|design(?:s|ed|ing)?"
    r"|styl(?:e|es|ed|ing)|mak(?:e|es|ing)|made|configure(?:s|d)?|configuring|config"
    r"|install(?:s|ed|ing)?|includ(?:e|es|ed|ing)|insert(?:s|ed|ing)?"
    r"|append(?:s|ed|ing)?|generat(?:e|es|ed|ing)|develop(?:s|ed|ing)?"
    r"|scaffold(?:s|ed|ing)?|integrat(?:e|es|ed|ing)|updat(?:e|es|ed|ing)"
    r"|fix(?:es|ed|ing)?|refactor(?:s|ed|ing)?|polish(?:es|ed|ing)?"
    r"|setup|set[\s-]?up|wire[\s-]?up)\b",
    re.IGNORECASE,
)
_VERIFY_ACTION_RE = re.compile(
    # (A1) a verification VERB (precise stems — `check(?:s|ed|ing)?` won't match
    # "checkout"/"checkbox"; `test(?:s|ed|ing)?` won't match "testimonials") followed
    # by a verification TARGET ("that/the/it/…") — "Verify the page", "Check that
    # links work", "Validate the HTML", "Ensure it renders".
    r"\b(?:verif(?:y|ies|ied|ying)|validat(?:e|es|ed|ing)|check(?:s|ed|ing)?"
    r"|confirm(?:s|ed|ing)?|ensure(?:s|d)?|ensuring|test(?:s|ed|ing)?"
    r"|review(?:s|ed|ing)?|inspect(?:s|ed|ing)?)\s+"
    r"(?:that|the|it|its|all|each|every|whether|if|for|no|cross)\b"
    # (A2) recognized standalone verification tokens/actions.
    r"|\bqa\b"
    r"|\bsmoke[\s-]?tests?\b"
    r"|\brun(?:s|ning)? (?:the |a )?(?:linter|lint|tests?|test suite|checks?)\b"
    # (A3) verification-OUTCOME phrases (a state asserted, not content created).
    r"|\b(?:renders?|displays?) (?:correctly|properly|as expected|fine|cleanly|well)\b"
    r"|\b(?:the )?(?:page|site|app|layout|content|everything|it) (?:renders?|displays?)\b"
    r"|\btests? pass(?:es|ed)?\b"
    r"|\blint(?:er)? pass(?:es|ed)?\b"
    r"|\bno console errors?\b",
    re.IGNORECASE,
)
# A shell command counts as a REAL non-browser CONTENT/STRUCTURE validation only when
# it STRUCTURALLY invokes a markup parser/validator (the parser is the EXECUTABLE/module
# actually run) or a content grep that reads index.html — NOT merely because a word like
# "validate"/"markup" appears in the text (`echo validate index.html` must NOT count —
# the codex catch), and never a bare existence/dump (`ls`/`test -f`/`stat`/`cat`/`wc`).
# Commands whose first token is a no-op/echo are excluded outright; see
# `_is_real_validation_command`.
# First token = a dedicated markup validator/linter invoked directly.
_VALIDATOR_EXECUTABLES = frozenset(
    {"xmllint", "tidy", "html5validator", "html5check", "vnu", "htmlhint", "htmllint"}
)
# First token = a content-search tool (a grep/assertion that actually reads the file).
_GREP_EXECUTABLES = frozenset({"grep", "egrep", "fgrep", "rg", "ripgrep", "ag"})
# First token = a python interpreter (only a real parser-module invocation counts).
_PYTHON_EXECUTABLES = frozenset({"python", "python3", "py", "python2"})
# First token = an explicit no-op / text-echo / existence-or-dump → NEVER a validation.
_NONVALIDATION_EXECUTABLES = frozenset(
    {
        "echo",
        "printf",
        ":",
        "true",
        "false",
        "cat",
        "ls",
        "stat",
        "test",
        "[",
        "[[",
        "wc",
        "file",
        "head",
        "tail",
        "touch",
        "cp",
        "mv",
        "rm",
        "dd",
        "tee",
    }
)
# A python -c/-m body that actually IMPORTS/USES an HTML/XML parser or markup validator.
_PYTHON_PARSER_RE = re.compile(
    r"htmlparser|html\.parser|html5lib|html5validator|\blxml\b|beautifulsoup|\bbs4\b"
    r"|xml\.etree|elementtree|\betree\b|xmllint|markupsafe",
    re.IGNORECASE,
)


def _is_real_validation_command(cmd: str) -> bool:
    """True iff `cmd` STRUCTURALLY runs a content/structure validation against
    index.html — the invoked tool is a markup parser/validator (or a grep that reads
    the file), not a word echoed in text. Rejects `echo validate index.html`,
    `printf "markup" index.html`, no-ops, and bare existence/dump commands."""
    cmd = cmd.strip()
    if "index.html" not in cmd:
        return False
    tokens = cmd.split()
    if not tokens:
        return False
    first = tokens[0].rsplit("/", 1)[-1]  # strip any leading path
    if first.startswith("#") or first in _NONVALIDATION_EXECUTABLES:
        return False
    if first in _VALIDATOR_EXECUTABLES or first in _GREP_EXECUTABLES:
        return True
    if first in _PYTHON_EXECUTABLES:
        # Must be a -c/-m invocation (actually executing code) AND name a real parser
        # module — `python -c "print('validate')"` must NOT pass.
        ran_code = bool(re.search(r"(?:^|\s)-[cm]\b", cmd))
        return ran_code and bool(_PYTHON_PARSER_RE.search(cmd))
    return False


def _shell_command_text(action: ActionEvent) -> str:
    """The command string a shell action ran — preferring the canonical command arg
    (so the first-token executable analysis is reliable), else the joined values."""
    args = action.tool_call.arguments or {}
    for key in ("command", "cmd", "script", "code"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return " ".join(str(v) for v in args.values())
