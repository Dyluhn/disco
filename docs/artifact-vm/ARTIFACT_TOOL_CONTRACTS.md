# Artifact VM — Tool Contracts (documentation-only schemas)

> **Status:** SPEC / DOCUMENTATION ONLY. No tool here is implemented by this pack. Each entry
> states its **concrete backing tool today** (a real name from
> [`_GROUNDING.md`](./_GROUNDING.md)) or **(GAP)** if it is proposed new work.
>
> **Read with:** [`DISCO_ARTIFACT_VM_SPEC.md`](./DISCO_ARTIFACT_VM_SPEC.md) (lifecycle, phases,
> rules) and [`_GROUNDING.md`](./_GROUNDING.md) (real names). Phase names below are the lifecycle
> phases defined in that spec §3: **create · edit · preview · verify · export**.

## Legend / house style

- Schemas are **frozen Pydantic v2** value objects, `model_config = ConfigDict(extra="forbid",
  frozen=True)`. JSON examples are what `model_dump(mode="json")` would emit.
- Field tables: `field | type | required | default | constraints`.
- `required = yes` means no default; `required = no` means the default applies.
- "Allowed phases" = phases in which the host phase guard accepts the call (rule §6.10 of the
  spec). "Forbidden phases" lists the rest with the reason.
- Failure codes are host-handled traps; the model sees the code, the host takes the action.
- **CD-TOOLS** = the shipped edit-discipline pack (fresh-read guard, atomic `exact_replace`,
  `safe_write_file` entrypoint guard, governed routing, verifier-only scope, elision discipline).

## Summary table (all 12 tools)

| # | Tool (conceptual) | Backing today | Allowed phases | Key failure codes |
| --- | --- | --- | --- | --- |
| 1 | `artifact_write` | `file_write` / `app_create` / `slides_generate` | create, edit | `ENTRYPOINT_PROTECTED`, `PHASE_FORBIDDEN`, `PARENT_MISSING` |
| 2 | `artifact_markup_str_replace` | `exact_replace` (CD-TOOLS) | edit | `OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE`, `ELISION_DETECTED` |
| 3 | `artifact_logic_str_replace` | `exact_replace` over JS / `app_set_tweak` | edit | `OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE`, `ELISION_DETECTED` |
| 4 | `artifact_set_props` | `app_set_tweak` (+`tweaks_io.py`) | create, edit | `TWEAK_LAW_VIOLATION`, `UNKNOWN_TWEAK`, `BAD_COLOR` |
| 5 | `artifact_read_parts` | `file_read`/`file_list` (+ appkit reads) | create, edit, preview, verify, export | `PART_NOT_FOUND`, `ARTIFACT_NOT_INDEXED` |
| 6 | `exact_replace` | `exact_replace` (**DONE**, CD-TOOLS) | edit | `OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE` |
| 7 | `safe_write_file` | `safe_write_file` (**DONE**, CD-TOOLS) | create, edit | `ENTRYPOINT_PROTECTED`, `PARENT_MISSING` |
| 8 | `show_artifact_to_agent` | **(GAP)** (`preview_start`+verifier screenshot today) | preview | `PREVIEW_DOWN`, `NOT_APP_DELIVERY` |
| 9 | `show_artifact_to_user` | **(GAP)** | preview | `PREVIEW_DOWN`, `NOT_PRESENTABLE` |
| 10 | `ready_for_artifact_verification` | `ready_for_<kind>_verification` (**DONE**, host route) | verify | `BAD_FINALIZER_NAME`, `REQUIRED_FILES_MISSING`, `NOT_SHOWN` |
| 11 | `present_artifact_for_download` | **(GAP)** (`DeliverableEvent`, P10b) | export | `NOT_VERIFIED`, `EXPORT_MISSING`, `EMPTY_DELIVERABLE` |
| 12 | `run_project_script` | `run_project_script` (**DONE**) | create, edit, verify | `SCRIPT_NOT_FOUND`, `NONZERO_EXIT`, `TIMEOUT` |

---

## 1. `artifact_write`

- **Purpose:** Create or overwrite a *non-entrypoint* artifact source file (or a brand-new file).
- **Backing today:** `file_write` (site/document/prototype); `app_create` / `app_update_content`
  (appkit); `slides_generate` (deck) — façade dispatches per `ContractKind`.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | project-relative; must not be a registered entrypoint unless `rewrite_allowed` |
| `content` | `str` | yes | — | full file body; no elision markers |
| `kind` | `ContractKind` | yes | — | one of grounding §1 ids |
| `create_parents` | `bool` | no | `false` | if false, parent dir must exist |

```json
{ "path": "assets/app.css", "content": "body{margin:0}", "kind": "static.site", "create_parents": false }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | written path |
| `bytes_written` | `int` | length on disk |
| `created` | `bool` | true if new file |

```json
{ "path": "assets/app.css", "bytes_written": 14, "created": true }
```

- **Allowed phases:** create, edit.
- **Forbidden phases:** preview/verify/export — source mutation after the artifact is shown or
  being verified would invalidate the verifier's basis.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `ENTRYPOINT_PROTECTED` | `path` is a registered entrypoint and `rewrite_allowed=false` | yes | reject; route to edit tool |
| `PHASE_FORBIDDEN` | called in preview/verify/export | yes | reject; advise return to edit |
| `PARENT_MISSING` | parent dir absent and `create_parents=false` | yes | reject; retry with flag |

- **Tests required:** happy path (new + overwrite non-entrypoint); each failure code; idempotency
  (same content twice → `created=false`, identical bytes); phase-guard rejection in verify.
- **CD-TOOLS inheritance:** governed artifact routing + `safe_write_file` entrypoint guard — it
  cannot be used to clobber a protected entrypoint.

---

## 2. `artifact_markup_str_replace`

- **Purpose:** Apply one atomic old→new replacement to the markup entrypoint after a fresh read.
- **Backing today:** `exact_replace` (CD-TOOLS); fallbacks `file_str_replace` / `file_edit` /
  `file_replace_lines`.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | must be the markup entrypoint for the kind |
| `old` | `str` | yes | — | must match current source verbatim (fresh-read guard) |
| `new` | `str` | yes | — | no elision markers |
| `expect_count` | `int` | no | `1` | required occurrences of `old`; ≥1 |

```json
{ "path": "index.html", "old": "<h1>Hi</h1>", "new": "<h1>Welcome</h1>", "expect_count": 1 }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | edited file |
| `replacements` | `int` | count applied |

```json
{ "path": "index.html", "replacements": 1 }
```

- **Allowed phases:** edit.
- **Forbidden phases:** create (entrypoint not yet final), preview/verify/export (no post-show
  mutation).
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `OLD_NOT_FOUND` | `old` not present (stale memory) | yes | re-read source (fresh-read guard), retry |
| `OLD_NOT_UNIQUE` | occurrences ≠ `expect_count` | yes | widen `old` context, retry |
| `STALE_SOURCE` | source changed since last read | yes | re-read, retry |
| `ELISION_DETECTED` | `new` contains an elision marker | no | reject; require full replacement text |

- **Tests required:** happy path; each failure code; idempotency (re-applying when `new` already
  present → `OLD_NOT_FOUND`); phase-guard rejection in create.
- **CD-TOOLS inheritance:** the core anti-Mode-B primitive — fresh-read guard + atomic exact
  replacement + elision discipline.

---

## 3. `artifact_logic_str_replace`

- **Purpose:** Atomic replacement scoped to logic/JS regions of the artifact.
- **Backing today:** `exact_replace` over `<script>` / `*.js`; appkit behavior via `app_set_tweak`.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | logic file or entrypoint containing inline script |
| `old` | `str` | yes | — | verbatim match (fresh-read guard) |
| `new` | `str` | yes | — | no elision markers |
| `expect_count` | `int` | no | `1` | ≥1 |

```json
{ "path": "index.html", "old": "const N = 1;", "new": "const N = 2;", "expect_count": 1 }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | edited file |
| `replacements` | `int` | count applied |

```json
{ "path": "index.html", "replacements": 1 }
```

- **Allowed phases:** edit.
- **Forbidden phases:** create / preview / verify / export — same rationale as tool 2.
- **Failure codes:** identical set to tool 2 (`OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE`,
  `ELISION_DETECTED`).
- **Tests required:** happy path; each failure code; idempotency; phase-guard rejection; appkit
  path routes behavior change through `app_set_tweak` (controls_behavior).
- **CD-TOOLS inheritance:** same as tool 2 — fresh-read + atomic exact + elision discipline.

---

## 4. `artifact_set_props`

- **Purpose:** Set user-facing knobs/tweaks (props metadata) without touching markup.
- **Backing today:** `app_set_tweak` (+ `tweaks_io.py`); **(GAP)** as a generic façade for
  non-appkit kinds.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `key` | `str` | yes | — | must be a known `TweakField.key` |
| `value` | `str \| int \| float \| bool` | yes | — | type must match the field's `TweakEditor` |
| `editor` | `TweakEditor` | no | (field's) | `text\|color\|int\|float\|boolean\|enum\|palette` |

```json
{ "key": "accent", "value": "#3366ff", "editor": "color" }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `key` | `str` | tweak set |
| `value` | `str` | normalized value (colors → `#rrggbb`) |
| `persisted_to` | `str` | `.disco/tweaks.json` |

```json
{ "key": "accent", "value": "#3366ff", "persisted_to": ".disco/tweaks.json" }
```

- **Allowed phases:** create, edit.
- **Forbidden phases:** preview/verify/export — props are source; changing them after show
  invalidates verification.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `TWEAK_LAW_VIOLATION` | tweak doesn't control behavior or required #fields (text/color need ≥2) | no | reject; cite grounding §7 law |
| `UNKNOWN_TWEAK` | `key` not a defined `TweakField` | yes | reject; list valid keys |
| `BAD_COLOR` | color value not normalizable to `#rrggbb` | yes | reject; retry |

- **Tests required:** happy path (each editor type); each failure code; idempotency (same value →
  no-op write); color normalization; phase-guard rejection in verify.
- **CD-TOOLS inheritance:** governed routing (props are a non-entrypoint part; never a raw write).

---

## 5. `artifact_read_parts`

- **Purpose:** Read the indexed parts of the artifact (the fresh-read source for exact edits).
- **Backing today:** `file_read` / `file_list` (+ appkit `app_*` read paths); **(GAP)** as a
  unified parts reader.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `parts` | `tuple[str,...]` | no | `("markup",)` | subset of `markup\|logic\|props\|manifest\|semantic\|resources\|verification\|export` |
| `include_content` | `bool` | no | `true` | false → metadata only |

```json
{ "parts": ["markup", "props"], "include_content": true }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `parts` | `list[object]` | each: `{name, backing_file, present, content?}` |

```json
{ "parts": [ { "name": "markup", "backing_file": "index.html", "present": true, "content": "<!doctype html>…" },
             { "name": "props", "backing_file": ".disco/tweaks.json", "present": false } ] }
```

- **Allowed phases:** create, edit, preview, verify, export (read-only is always safe).
- **Forbidden phases:** none.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `PART_NOT_FOUND` | requested part name invalid | yes | reject; list valid part names |
| `ARTIFACT_NOT_INDEXED` | artifact not yet in EDIT (parts not indexed) | yes | run create/index first |

- **Tests required:** happy path (each part); missing part returns `present=false` not error;
  `PART_NOT_FOUND` for bad name; read allowed in every phase.
- **CD-TOOLS inheritance:** supplies the **fresh source** that the fresh-read guard requires
  before exact edits.

---

## 6. `exact_replace`

- **Purpose:** Atomic, fresh-read-guarded exact text replacement (the CD-TOOLS edit primitive).
- **Backing today:** `exact_replace` (**DONE**, CD-TOOLS).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | project-relative |
| `old` | `str` | yes | — | verbatim match required |
| `new` | `str` | yes | — | no elision markers |
| `expect_count` | `int` | no | `1` | ≥1 |

```json
{ "path": "report.md", "old": "## Intro", "new": "## Introduction", "expect_count": 1 }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | edited file |
| `replacements` | `int` | count applied |

```json
{ "path": "report.md", "replacements": 1 }
```

- **Allowed phases:** edit.
- **Forbidden phases:** create/preview/verify/export — no post-show source mutation; entrypoint
  not final during create.
- **Failure codes:** `OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE` (see tool 2 table).
- **Tests required:** happy path; each failure code; idempotency (`OLD_NOT_FOUND` on re-apply);
  multi-occurrence with `expect_count`; phase-guard rejection.
- **CD-TOOLS inheritance:** this *is* CD-TOOLS atomic exact replacement + fresh-read guard.

---

## 7. `safe_write_file`

- **Purpose:** Write a file but refuse to clobber a registered artifact entrypoint.
- **Backing today:** `safe_write_file` (**DONE**, CD-TOOLS).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | rejected if a registered entrypoint |
| `content` | `str` | yes | — | full body; no elision markers |
| `create_parents` | `bool` | no | `false` | parent dir must exist if false |

```json
{ "path": "data/seed.json", "content": "[]", "create_parents": true }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | written path |
| `bytes_written` | `int` | length on disk |
| `created` | `bool` | true if new |

```json
{ "path": "data/seed.json", "bytes_written": 2, "created": true }
```

- **Allowed phases:** create, edit.
- **Forbidden phases:** preview/verify/export — source mutation after show invalidates verifier.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `ENTRYPOINT_PROTECTED` | `path` is a registered entrypoint | yes | refuse; route to edit tool (rule §6.1) |
| `PARENT_MISSING` | parent absent and `create_parents=false` | yes | retry with flag |

- **Tests required:** happy path; `ENTRYPOINT_PROTECTED` for each kind's entrypoint; `PARENT_MISSING`;
  idempotency; phase-guard rejection in verify.
- **CD-TOOLS inheritance:** this *is* the CD-TOOLS safe-write entrypoint guard.

---

## 8. `show_artifact_to_agent`

- **Purpose:** Render an agent-private preview (screenshot/DOM) for the model to self-inspect.
- **Backing today:** **(GAP)** — today only `preview_start` + verifier `verify_web_app` screenshot
  exist; there is no agent-private inspection channel.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `route` | `str` | no | `"/"` | preview route to capture |
| `viewport` | `str` | no | `"desktop"` | `desktop\|mobile` (cf. `viewport` tool) |

```json
{ "route": "/", "viewport": "desktop" }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `screenshot_ref` | `str` | host ref to captured image (agent-private) |
| `console_errors` | `list[str]` | preview console errors, if any |

```json
{ "screenshot_ref": "host://preview/abc123.png", "console_errors": [] }
```

- **Allowed phases:** preview.
- **Forbidden phases:** create/edit (nothing running), verify (verifier owns diagnostics —
  rule §6.8), export.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `PREVIEW_DOWN` | preview server not healthy | yes | `preview_logs`, restart, retry |
| `NOT_APP_DELIVERY` | kind is files-delivery (deck/document/workflow/custom) | no | reject; use file inspection |

- **Tests required:** happy path (app kinds); `PREVIEW_DOWN`; `NOT_APP_DELIVERY` for each files kind;
  phase-guard rejection in verify (must not double as verifier diagnostic).
- **CD-TOOLS inheritance:** verifier-only read scope — this is **agent-private** and must NOT be
  the verifier's truth channel; it cannot satisfy verification.

---

## 9. `show_artifact_to_user`

- **Purpose:** Promote the artifact to an explicit user-visible state (distinct from verification).
- **Backing today:** **(GAP)** — today conflated with `preview_start`.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `route` | `str` | no | `"/"` | route the user lands on |
| `note` | `str` | no | `""` | optional caption shown with the preview |

```json
{ "route": "/", "note": "Draft landing page — review the hero copy." }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `shown` | `bool` | true once user-visible |
| `preview_url` | `str` | user-facing preview url |

```json
{ "shown": true, "preview_url": "https://preview.disco/abc123/" }
```

- **Allowed phases:** preview.
- **Forbidden phases:** create/edit (not presentable), verify/export (already past show).
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `PREVIEW_DOWN` | preview server unhealthy | yes | restart, retry |
| `NOT_PRESENTABLE` | `required_files` incomplete | yes | return to edit/create |

- **Tests required:** happy path; `PREVIEW_DOWN`; `NOT_PRESENTABLE`; **does not** set verification
  status (rule §6.7); idempotency (re-show = same url).
- **CD-TOOLS inheritance:** enforces the show-vs-verify separation that keeps verifier-only scope
  meaningful.

---

## 10. `ready_for_artifact_verification`

- **Purpose:** The finalizer the model calls to hand the artifact to the verifier (host-routed).
- **Backing today:** `ready_for_<kind>_verification` finalizer family (**DONE** as host route;
  e.g. `ready_for_static_site_verification`, `ready_for_app_verification`).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `finalizer` | `str` | yes | — | must match `^ready_for_[a-z0-9_]+_verification$` |
| `summary` | `str` | no | `""` | what changed since last verify |

```json
{ "finalizer": "ready_for_static_site_verification", "summary": "Hero copy + CTA wired." }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `accepted` | `bool` | finalizer accepted, routed to verifier |
| `level` | `VerificationLevel` | `load_only\|standard\|strict` from contract |

```json
{ "accepted": true, "level": "standard" }
```

- **Allowed phases:** verify (the call *enters* verify from show_to_user).
- **Forbidden phases:** create/edit (premature; artifact not shown), preview (must pass through
  show_to_user, rule §6.7), export (already verified).
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `BAD_FINALIZER_NAME` | string fails the regex | no | reject; cite naming law (grounding §2) |
| `REQUIRED_FILES_MISSING` | `required_files` incomplete | yes | return to create/edit |
| `NOT_SHOWN` | artifact never reached SHOW_TO_USER | yes | call `show_artifact_to_user` first |

- **Tests required:** happy path (each kind's finalizer); `BAD_FINALIZER_NAME` (regex fail);
  `REQUIRED_FILES_MISSING`; `NOT_SHOWN`; correct `level` returned per contract; the finalizer
  routes to the verifier, not to a main-agent tool.
- **CD-TOOLS inheritance:** verifier-only read scope — the finalizer is the *only* bridge from the
  main agent to verifier truth; the agent cannot self-verify.

---

## 11. `present_artifact_for_download`

- **Purpose:** Deliver exported bytes to the user as a downloadable artifact.
- **Backing today:** **(GAP)** — backed by `DeliverableEvent` (P10b proved bytes flow).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `export_name` | `str` | yes | — | must match the contract's `ExportContract.name` |
| `filename` | `str` | no | (export default) | suggested download filename |

```json
{ "export_name": "static_standalone", "filename": "site.zip" }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `delivered` | `bool` | true once bytes emitted |
| `bytes` | `int` | deliverable size |
| `event` | `str` | `DeliverableEvent` ref |

```json
{ "delivered": true, "bytes": 20480, "event": "deliverable://abc123" }
```

- **Allowed phases:** export.
- **Forbidden phases:** create/edit/preview/verify — nothing to deliver until the pipeline ran.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `NOT_VERIFIED` | artifact not VERIFY-passed | yes | run finalizer + pass verify first |
| `EXPORT_MISSING` | kind has no `ExportContract` (GAP kinds) | no | reject; handoff as preview per spec §7.4 |
| `EMPTY_DELIVERABLE` | pipeline produced no bytes | yes | re-run export pipeline |

- **Tests required:** happy path (each existing pipeline: `static_standalone`, `cloudflare_project`,
  `deck_export`, `document_pdf`) → non-empty bytes; `NOT_VERIFIED`; `EXPORT_MISSING` for
  `interactive.prototype`/`workflow.output`/`custom`; `EMPTY_DELIVERABLE`; phase-guard rejection
  before export.
- **CD-TOOLS inheritance:** host-owned export — model never assembles bytes; consistent with
  governed routing and host-owned pipelines (spec rule §6.9).

---

## 12. `run_project_script`

- **Purpose:** Run a project-defined script (build/lint/dev task) in the sandbox.
- **Backing today:** `run_project_script` (**DONE**).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `script` | `str` | yes | — | must be a declared project script name |
| `args` | `tuple[str,...]` | no | `()` | passthrough args |
| `timeout_s` | `int` | no | `120` | 1–600 |

```json
{ "script": "build", "args": [], "timeout_s": 120 }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `exit_code` | `int` | process exit code |
| `stdout` | `str` | captured stdout (truncated) |
| `stderr` | `str` | captured stderr (truncated) |

```json
{ "exit_code": 0, "stdout": "built in 1.2s", "stderr": "" }
```

- **Allowed phases:** create, edit, verify (build/lint as a verify aid).
- **Forbidden phases:** preview (use preview tools), export (host owns the pipeline — rule §6.9).
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `SCRIPT_NOT_FOUND` | script name not declared | yes | reject; list scripts |
| `NONZERO_EXIT` | script exits non-zero | yes | surface stderr; return to edit |
| `TIMEOUT` | exceeds `timeout_s` | yes | kill; raise/adjust timeout |

- **Tests required:** happy path; `SCRIPT_NOT_FOUND`; `NONZERO_EXIT`; `TIMEOUT`; idempotency for
  pure scripts; phase-guard rejection in export.
- **CD-TOOLS inheritance:** does not mutate artifact source directly, so it is outside the edit
  guards; it must never be used to bypass governed routing (e.g. a script that rewrites an
  entrypoint is still subject to the entrypoint protection in spirit — flag in review).

---

## Cross-references

- Lifecycle / phases / rules: [`DISCO_ARTIFACT_VM_SPEC.md`](./DISCO_ARTIFACT_VM_SPEC.md)
- Real Disco names, registries, GAP list: [`_GROUNDING.md`](./_GROUNDING.md)
- `data-disco-*` semantic markup: [`DIRECT_MANIPULATION_SPEC.md`](./DIRECT_MANIPULATION_SPEC.md)
- Resource manifest + provenance: [`RESOURCE_AND_PROVENANCE_SPEC.md`](./RESOURCE_AND_PROVENANCE_SPEC.md)
