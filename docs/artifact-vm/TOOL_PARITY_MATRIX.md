# Tool Parity Matrix — Claude Design → Disco

> **Status: SPEC ONLY — no runtime code changed.**
> Source of truth for every real name below: [`_GROUNDING.md`](./_GROUNDING.md).
> This matrix maps each Claude Design ("dc_*") tool/pattern onto the **real** Disco
> tool/contract that already realizes it, or marks it **(GAP)** where no Disco surface
> exists yet. CD-TOOLS 1..10 are treated as **DONE + live-proven** (grounding §9) and are
> never re-specced as new work.

## Legend

- **Disco equivalent** — the concrete real tool/concept name from grounding §1–§8, or
  **(GAP)** for a proposed-new surface. Where Claude Design uses a `dc_*` artifact tool,
  the Disco column names the *concrete backing tool* and, where relevant, the proposed
  unifying **`artifact_*` façade** (grounding §10). The façade is a conceptual/thin-router
  wrapper, not new behavior.
- **Status assumption** —
  - `DONE via X` — already shipped; X names the mechanism (CD-TOOLS, files.py, registry,
    export pipeline, etc.).
  - `PARTIAL` — backing capability exists for *some* kinds/roles but not the full surface.
  - `GAP` — no Disco surface exists; proposed as new work.
- **Required tests** — minimum acceptance tests the future implementer must add. Per
  grounding §11.3 every tool section needs *Failure modes + Tests required*; this column is
  the Tests-required digest.
- **Risk if missing** — what breaks (or stays manual/thrash-prone) without it.
- **Priority** — P0 (blocks correct/safe artifact lifecycle), P1 (needed for parity, not
  safety-critical), P2 (polish / nice-to-have / Disco policy makes it low-value).

---

## Matrix

| # | Claude Design role | Disco equivalent (real name or (GAP)) | Status assumption | Required tests | Risk if missing | Priority |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `dc_write` — create/overwrite artifact source | `file_write` / `safe_write_file` (sites/docs/prototype/custom); `app_create` (appkit); governed artifact routing sends entrypoints through edit tools | **DONE via CD-TOOLS + files.py** (grounding §4, §9) | entrypoint-clobber refusal; fresh full-file write; routing sends `index.html`/`appspec.json` through governed path | Raw write clobbers a live artifact entrypoint mid-build | P0 |
| 2 | `dc_html_str_replace` — semantic replace in HTML artifact | `file_str_replace` / `file_edit` (backing); proposed `artifact_markup_str_replace` **(GAP-façade)** | **DONE via files.py**; façade **GAP** | exact-match replace; ambiguous-match rejection; no-elision-marker guard; routes through CD-TOOLS governance | Edits bypass governed routing / silent multi-match clobber | P1 |
| 3 | `dc_js_str_replace` — semantic replace in JS artifact | `file_str_replace` / `file_edit` (backing); proposed `artifact_script_str_replace` **(GAP-façade)** | **DONE via files.py**; façade **GAP** | exact-match replace; ambiguous-match rejection; elision-marker guard | Same as #2 for script source | P1 |
| 4 | `dc_set_props` — declarative component/prop set | `app_set_design`, `app_set_tweak`, `app_update_content` (AppKit semantic ops, grounding §4) | **PARTIAL** (appkit kinds only; no declarative prop edit for site/prototype/custom) | tweak grounding-law enforcement (§7); design set persists to `.disco/tweaks.json`; content update is targeted | Non-appkit kinds have no declarative prop edit → fall back to raw text edits | P1 |
| 5 | `str_replace_edit` — generic exact replacement | `exact_replace` | **DONE via CD-TOOLS** (atomic exact replacement + fresh-read guard, §9) | stale-read rejection (fresh-read guard); atomic single replace; reject on 0 or >1 matches; no elision markers | Stale clobber / edit-elision thrash (the Mode-B failure CD-TOOLS killed) | P0 |
| 6 | `write_file` — write file bytes | `file_write` / `safe_write_file` | **DONE via files.py + CD-TOOLS** (safe write refuses entrypoint clobber, §9) | byte-faithful write; `safe_write_file` refuses to clobber declared `required_files` entrypoints | Unsafe overwrite of contract-required files | P0 |
| 7 | `read_file` — read file bytes | `file_read` (+ `file_list`, verifier-only read scope) | **DONE via files.py + CD-TOOLS** (verifier-only read scope, §9) | read returns current bytes; verifier role read-scope enforced; build role cannot read outside artifact root | Verifier read-scope leak / model reads outside workspace | P1 |
| 8 | `run_script` — execute build/dev script | `run_project_script` / `code_exec` / `shell` family (`shell_exec`, `shell_view`, `shell_wait`, `shell_kill_process`) | **DONE via files.py/tools** (grounding §4) | sandboxed exec; timeout/kill; stdout+stderr capture; non-zero exit surfaced | Unsandboxed or hanging script exec | P1 |
| 9 | `copy_starter_component` — scaffold from a starter | `scaffold_starter` (+ kit data in `kits/starter.py`) | **PARTIAL** — tool exists but only `app_shell` + `lead_form` kits are real; 12 catalog kits **(GAP)** (grounding §6) | scaffold each registered kit produces its `required_files`; unknown-kit error; idempotent re-scaffold guard | Missing kits → model hand-writes scaffolding → rewrite thrash | P1 |
| 10 | `show_html` — agent-private render inspection | `show_artifact_to_agent` **(GAP)** — today only `preview_start` + `verify_web_app` screenshot exist | **GAP** | agent-private preview returns render/screenshot WITHOUT changing user-visible state; no DeliverableEvent emitted | Agent edits blind; cannot self-inspect render before showing user | P2 |
| 11 | `show_to_user` — present artifact to user | `show_artifact_to_user` **(GAP)** — today implicit via `preview_start`/`live_view` | **GAP** | explicit user-visible preview state event; distinct from agent-private show (#10) | No explicit "present to user" affordance → ambiguous preview ownership | P2 |
| 12 | `ready_for_verification` — finalize for verification | `ready_for_*_verification` host-finalizer family (regex `^ready_for_[a-z0-9_]+_verification$`), per-kind in registry | **DONE via VerificationContract** (host-routed strings, grounding §3/§4) | regex contract holds; per-kind finalizer routes correctly; `VerificationLevel` (load_only/standard/strict) enforced; not exposed as a model-free builtin | Finalizer bypass → unverified artifact ships | P0 |
| 13 | `eval_js` / `screenshot` (verifier-only) | `verify_web_app` / `verify_app` screenshot (backing); in-page `eval_js` probe **(GAP)**; both under verifier-only read scope | **PARTIAL** — screenshot DONE; scripted in-page assertion GAP | screenshot capture at viewport; verifier-only scope; (GAP) eval_js returns serialized result; sandboxed | Verifier can capture pixels but cannot assert DOM/runtime state | P2 |
| 14 | `questions_v2` — structured clarifying questions | **(GAP)** — no ask mechanism in grounding; Disco policy is *defer-don't-block* in autonomous runs (reason through, never block) | **GAP** (intentional / low-value) | if ever built: questions are non-blocking + resumable; autonomous runs never hard-block | Low — Disco deliberately avoids blocking questions; absence is by design | P2 |
| 15 | `read_skill_prompt` — load a skill prompt at build time | `BuildContract.skills` mounting (the `skills` tuple, e.g. `appkit.leadgen`/`cloudflare_export`/`design_recipe`) | **PARTIAL** — contract declares skills, but no runtime tool/loader proven to inject them into prompt assembly | declared `skills` are resolved + injected into prompt-pack assembly; missing skill errors; ordering deterministic | Declared skills silently not injected → contract lies about context | P1 |
| 16 | `image_metadata` — probe image dims/format | **(GAP)** — `image_generate` exists; no metadata probe | **GAP** | returns width/height/format/bytes for a workspace image; missing-file error | Layout decisions made blind to real image dimensions | P2 |
| 17 | `view_image` — inspect an image in-context | **(GAP)** — only `verify_web_app` screenshot surfaces pixels | **GAP** | model/verifier can view a provided or generated image; scope-checked | Model cannot inspect provided/generated images before placing them | P2 |
| 18 | `image_slot` — declarative image placeholder | `image_slot` starter kit **(GAP)** (grounding §6); for decks, C7 slide image-embed is **DONE** (reference) | **GAP** (starter kit) | slot renders placeholder; accepts a generated/provided asset by id; preserved on edit | Ad-hoc image embedding instead of governed slots | P2 |
| 19 | `copy_files` — bulk copy assets | **(GAP)** — no bulk-copy tool; `file_read`+`file_write` cover single-file | **GAP** (single-file PARTIAL via file ops) | bulk copy preserves bytes + relative paths; overwrite policy honored | Multi-asset bundling is manual + error-prone | P2 |
| 20 | `gen_pptx` — generate a PPTX deck | `slides_generate` + `_pptx_render` + `deck_patch`, exported via `deck_export` | **DONE via slides pipeline** (grounding §4/§8; C7 image-embed + branded template landed) | pptx renders; images embed (C7); branded template applied when `theme.branded`; `deck_export` emits bytes | n/a — DONE | P0 (done) |
| 21 | `super_inline_html` — single-file standalone HTML | `static_standalone` export pipeline (`bundle, validate, deliver`) | **DONE via export pipeline** (grounding §8) | all CSS/JS/assets inlined to one file; validates; delivers bytes | n/a — DONE | P1 (done) |
| 22 | `present_fs_item_for_download` — download affordance | `present_artifact_for_download` **(GAP)**; backing delivery already proven via `DeliverableEvent` bytes (P10b) | **GAP** (backing delivery DONE) | download affordance emits a `DeliverableEvent`; per-file selection; filename + mime correct | No explicit per-file download surface; relies on whole-export delivery | P1 |
| 23 | `open_for_print` — print/PDF view | `document_pdf` export (DONE for documents); print-view for app/site kinds **(GAP)** | **PARTIAL** — `document_pdf` DONE; print path for non-document kinds GAP | print CSS applied; PDF bytes produced; pagination correct | No print/PDF path for non-document artifacts | P2 |
| 24 | `get_public_file_url` — mint a public URL | `cloudflare_project` export deploy URL (DONE for appkit.leadgen); general public-URL minting **(GAP)** | **PARTIAL** — appkit deploy URL DONE; general minting GAP | appkit deploy returns reachable public URL; (GAP) general file → signed/public URL | No general public-URL surface outside the cloudflare appkit path | P2 |
| 25 | handoff to Claude Code — escape to full-code agent | **(GAP)** — no handoff package; closest today is export bundle + the build harness | **GAP** | handoff package bundles source + `BuildContract` + provenance + verification verdicts; re-openable | No clean escape hatch from constrained artifact build to full-code editing | P2 |

---

## Priority rollup (open work = GAP or PARTIAL)

Fully **DONE** (8): `dc_write` (1), `str_replace_edit` (5), `write_file` (6), `read_file`
(7), `run_script` (8), `ready_for_verification` (12), `gen_pptx` (20), `super_inline_html`
(21). These map onto CD-TOOLS / files.py / registry / export pipelines and are referenced,
not re-specced.

Open items needing new surface — **17 total**:

| Priority | Count | Items |
| --- | --- | --- |
| **P0 gaps** | **0** | — (all P0 lifecycle/safety tools are DONE via CD-TOOLS) |
| **P1 gaps** | **6** | `artifact_markup_str_replace` façade (2), `artifact_script_str_replace` façade (3), `dc_set_props` non-appkit (4), `copy_starter_component` 12 missing kits (9), `read_skill_prompt` skills mounting (15), `present_artifact_for_download` (22) |
| **P2 gaps** | **11** | `show_artifact_to_agent` (10), `show_artifact_to_user` (11), verifier `eval_js` (13), `questions_v2` (14, intentional), `image_metadata` (16), `view_image` (17), `image_slot` kit (18), `copy_files` (19), `open_for_print` non-document (23), `get_public_file_url` general (24), handoff-to-Claude-Code (25) |

**Reading of the rollup:** the safety-critical lifecycle (create / exact-edit / safe-write /
finalize / verify) is already closed by CD-TOOLS — **zero P0 gaps**. Parity work is
concentrated in (a) the `artifact_*` unifying façade + non-appkit declarative edits, (b)
the starter-kit catalog (12 missing kits), (c) skills mounting, and (d) the show/present/
download/handoff surfaces. None of these are blockers for safe builds; they are parity and
ergonomics. See [`MERGE_PLAN.md`](./MERGE_PLAN.md) for sequencing and
[`README.md`](./README.md) for the full pack index.
