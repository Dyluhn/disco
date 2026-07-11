# AppKit campaign — make the track finish-reliable, or switch it off

**Goal:** an autonomous AppKit build reaches FINISHED/VERIFIED without human
rescue, proven by the soak lane — or, if the track can't get there, it is
removed from the product with one config change. The safety floor already
exists: `DISCO_APPKIT_ENABLED=0` (commit `e488404c`, live-proven) fully
separates the track. This campaign is the work that earns keeping it on.

**State (2026-07-10).** Landed and committed: the finish-deadlock fix
(`cbfec1fd`), vocabulary-in-schema + no-op exit paths (`12abd8df`), the engine
gate-order fix — barred tools can no longer park autonomous runs on a human
confirmation (`09a1f127`), CONTRACT-DURABILITY fold (`36bab3d1`), the soak lane
plumbing (`e2b9f021`), and the kill switch (`e488404c`). Blocking everything
downstream: the verifier, below.

---

## Wave 1 — `records_verify` (the blocker, correctly scoped)

### The proven chain

Reproduced 2026-07-10 against current code
(scratch repro: build a records app with one entity `shift` through the real
`prepare_records_app_spec` + `generate_records`, then run the verifier's
fallback bundle). Result: **5 of 6 checks FAIL, structurally — a records app
can never pass `verify_appkit_app`.** Control: the same bundle on a default
lead_gen app passes 6/6. The chain, link by link:

1. `app_create` offers `records` as one of three base kinds.
2. `records_primitive.py:1454` — `verify=None` ON PURPOSE; the comment blesses
   falling through to the lead-gen bundle and itself prescribes the fix
   ("Give records its own verify only with its own contract checks").
3. `verify_appkit_app.py` WO-A3 dispatch: `verify is None → lead_gen_verify`.
4. `lead_gen_verify` resolves its target via `resolve_lead_entity`
   (`generator.py:120`): a records app has no submit action and no entity id
   `lead`, so it returns the **synthesized** name/email/message entity — which
   for records apps is never written into the spec (`ensure_lead_entity` is
   lead_gen's prepare hook only, `generator.py:2319`).
5. `check_schema_sql` + `_table_name` (`local_verify.py:104-117`): the
   synthesized lead's table isn't in the schema → alphabetically-first REAL
   table fallback (`shift`) → INSERT of name/email/message → *"table shift has
   no column named name"*.
6. Four more checks are hard-coded lead-shaped and also fail:
   `drizzle_schema_valid` demands `export const leads = sqliteTable(...)`;
   `worker_contract` demands a public `POST /api/leads`; `lead_form_posts`
   demands `useSubmit("/api/leads")`; `local_api_roundtrip` demands a
   reachable lead insert. The records generator emits `/api/shift`, a `shift`
   table, per-entity drizzle consts. Only `cloudflare_export_ready` (entity-
   agnostic) passes.
7. Autonomous consequence: the finish gate consumes the verdict → REPAIR → the
   model retries everything (observed wave 4: `app_create` ×11,
   `server_status` ×46, vite exit 130) → inactivity cap. **A deadlock, not a
   flake** — no model action short of mutating the app into a lead-gen app can
   satisfy the gate.
8. Why it stayed latent: every earlier lane scenario was lead-gen-shaped;
   wave 4's notes-app prompt produced the first records app under the strict
   verifier.

Two corollaries. The previously recorded fix ("derive the representative row
from the actual entity fields") clears only 1 of the 5 false-fails — the
correct scope is the full bundle. And until Wave 1 lands, `records` in the
`app_create` catalog is a **false affordance in autonomous appkit mode**:
every records app is unfinishable.

### The fix (re-aim, don't weaken)

Write `records_verify(app, design, tree)` in `records_primitive.py` and
register it (`verify=records_verify`) — the WO-A3 dispatch needs zero changes.
The bundle mirrors lead_gen's rigor, derived per-entity from the generator's
OWN naming helpers (`_records_table_name`, `_ts_const_names`) so checks can
never drift from emission — the same source-of-truth doctrine as the
trusted-components probes:

- `schema_sql_valid` — for EVERY records entity: its table exists in the
  executed schema; insert a representative row from **that entity's fields**
  (reuse `_representative_value`); read back; required columns reject NULL.
- `drizzle_schema_valid` — per entity: its `_ts_const_names` export is a
  `sqliteTable(...)` whose columns match schema.sql.
- `worker_contract` — per entity: the `/api/<table>` route entry exists;
  guard-first 401 on unauthenticated reads (same ADMIN_TOKEN Bearer doctrine
  the records worker already emits).
- `local_api_roundtrip` — per-entity POST → authed read via the local D1 shim.
- Auth variant: when the spec carries auth (users/sessions emission,
  `/api/register`), assert those tables + the session guard exist.
- `cloudflare_export_ready` — reuse as-is.

Form-target entities stay out of scope here: the form primitive's own
`form_verify` covers them through the applied-primitive provenance dispatch.

**DoD:** the scratch repro (records app, entity `shift`) reports all checks
PASS; a broken records app (dropped table / unguarded read) reports the right
FAIL with entity-named evidence; unit fixtures for both; existing lead_gen /
directory / hello verdicts byte-identical.

## Wave 2 — one sequential lane proof run

`appkit_finish_autonomous` (already committed), run ONE at a time — parallel
runs contend on npm installs and the cold 300s `_VITE_BUILD_TIMEOUT_S`.
Runbook:

- Env: `DISCO_RELAY_LOG=/tmp/disco-provider-ledger.jsonl`; `--hard-cap 3600`.
- Restart `disco-app.service` / `disco-agent.service` first (package edits).
- Kill = `POST /conversations/{cid}/kill` with `X-Disco-CSRF` after a mint with
  a localhost Origin. `TaskStop` kills the runner, NOT the server-side build.
- Sidecar evidence slice must be up or classification returns INVALID_RUN
  (wave-4 lesson: `MINIMAX_RELAY_LOG` + container probe).

**DoD:** a green classified run with `.disco/appspec.json` +
`designspec.json` asserted and terminal FINISHED/VERIFIED.

## Wave 3 — funnel grinding, measured by the lane

Evidence-ranked backlog (each item has an observed failure behind it; the lane
provides before/after):

1. Plan-time appkit sequence recipe (strongest lever; starter-catalog precedent).
2. Next-step guidance in the `app_create` SUCCESS message.
3. Advertise ONLY `verify_appkit_app` in appkit scope (run 3 used
   `verify_web_app` ×2).
4. `variant_id` catalog in the `app_add_section` schema (`'form.standard'`
   was refused with no list of valid ids).
5. `server_status` poll damping (46 polls in one run).

## Wave 4 — honesty + policy

- Until Wave 1 lands: `records` in the catalog is enforced-broken for
  autonomous appkit. Either land Wave 1 first (preferred — it unblocks Wave 2
  on any prompt shape) or label/bar records in autonomous mode. Do not leave
  the false affordance.
- v0.1 borrow from the trusted-components design: the hand-rolled-auth honesty
  label (model-generated auth/payment code flagged unverified in deliverables).
- Kill-switch policy: if after Waves 1-3 the lane still can't hold a green
  streak, flip `DISCO_APPKIT_ENABLED=0` on deployments and park the track —
  the switch exists precisely so this campaign can fail safely.

---

*Wave order is strict: 1 → 2 → 3 → 4-policy; wave 4's honesty items may land
any time. Trace evidence: `test-record/appkit-lane/` (wave logs, ledger).
Related docs: `docs/trusted-components-design.md` (+spec) for the tier that
inherits this verifier lesson; `docs/disco-status-and-remaining.md` (handover
queue supersede: its "verifier re-aim" entry is this campaign's Wave 1, at the
corrected 5-of-6 scope).*
