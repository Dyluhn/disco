# Codex CODE Review — P4 / TOOL-1 (AppKit specialized mutation tools) — IMPLEMENTED

Inspect:
NEW: packages/core/src/disco/core/appkit/{__init__,models.py} (AppSpec/AppSection + pure mutations +
render_html); packages/tools/src/disco/tools/builtin/appkit.py (AppSpecStore + 8 tools);
tests test_appkit_models.py (core) + test_appkit_tools.py (tools).
CHANGED: builtin/__init__.py (register APP_TOOLS); registry.py (8 app_* in AGENT_TOOLS + ARTIFACT_TOOLS);
contract/registry.py (appkit.leadgen now scopes the semantic app_* tools, file_write→repair-only);
prompt_packs/build_appkit_leadgen.md (allowed-tools → the app_* tools).

Design: an app is a structured AppSpec (sections + design tokens + tweaks), edited by SMALL semantic tools
(app_update_content touches one field; app_add/remove/reorder_section; app_set_design/tweak;
app_snapshot_version), each re-rendering a self-contained index.html (inline CSS, data-disco-* anchors,
html-escaped). Raw file rewrite is NOT in this set (repair/custom only). Tools operate on .disco/appspec.json
via the sandbox FS; structured errors: no_app (edit before create), invalid_app_edit (bad section/kind),
corrupt_appspec (unparseable spec); app_create with a bad section kind persists NOTHING.

Tests: 70 passed across appkit tools+models + contract/scope/pack/skill regressions. Key: every tool round-
trips spec→html; update_content touches one field; section add/reorder/remove; design/tweak (bool coercion);
snapshot; registry+scope membership; the 3 error paths. basedpyright strict 0 errors. (The CONTRACT-2
tools-exist test now passes because app_* are registered; CONTRACT-3 compiler hard-allowlists them.)

Judge: (a) are the tools correct + safe (semantic edits only, structured errors, no raw-rewrite leak, html
escaping prevents injection)? (b) is the AppSpec model/renderer sound + deterministic? (c) is the
contract/pack/scope update coherent (appkit now genuinely uses app_* end-to-end)? (d) test sufficiency.
Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
