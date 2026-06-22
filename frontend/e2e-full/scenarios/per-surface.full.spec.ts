/**
 * per-surface.full.spec.ts — gaps #1, #2, #6.
 *
 * The headline blind spot (3-source consensus): the ONLY Playwright specs both
 * `page.goto("/")` and never navigate anywhere else, so History / Projects /
 * Settings / Activity / Build / Agent are entirely undriven, and
 * `HitMap.assertCoverage()` (the gate that fails on a declared-but-unclicked
 * backend handle) is called by ZERO specs.
 *
 * This spec closes all three:
 *   #1  Visit EACH primary route and enumerate its controls into one shared HitMap.
 *   #6  Write a coverage REPORT (every declared-but-unclicked backend handle) to
 *       the dossier so a human + the gate can see exactly what a run did not drive.
 *   #2  Call `HitMap.assertCoverage(ALLOWLIST)` — so a NEW always-visible, enabled
 *       backend-command handle that nobody drove (and nobody allowlisted) FAILS CI.
 *
 * REGRESSION net only (memory: feedback-live-model-proves-works): this proves the
 * routes render + the handle inventory is enforced. It does NOT prove any control
 * WORKS — the gated/destructive/persistence handles in ALLOWLIST below are exactly
 * the ones that need a LIVE-MODEL scenario to prove (the proof-tier follow-up).
 *
 * Runs on VM 201 with the dev server (:5173) + agent-server (:8000) up — NOT on
 * the workstation (Playwright OOMs under the memory cap).
 *
 * evidence-harness-campaign.md / disco-ui-control-gaps-6-21-26.md
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { expect, test } from "../fixtures/discoHarness";
import { enumerateControls, HitMap, type Control } from "../support/uiInventory";

/** Every primary route the app exposes (cid-less surfaces — resume/share routes
 *  need a seeded conversation id and are covered by the live scenario matrix). */
const ROUTES: ReadonlyArray<{ path: string; label: string }> = [
  { path: "/", label: "home" },
  { path: "/build", label: "build" },
  { path: "/agent", label: "agent" },
  { path: "/history", label: "history" },
  { path: "/projects", label: "projects" },
  { path: "/settings", label: "settings" },
  { path: "/activity", label: "activity" },
];

/**
 * The "needs a live model / live backend to exercise safely" tier (gap #2 + the
 * report's PROOF-tier note). Each entry is a stable `disco:` controlId → the
 * reason it is NOT driven by this regression spec. The coverage gate fails on any
 * OTHER always-visible enabled backend handle, so adding a new one forces a
 * choice: drive it, or allowlist it here with a reason.
 *
 * NOTE: gate handles (approve-action / answer-question / pick-alternative /
 * submit-clarify / reject-action) only RENDER when the live model emits the
 * triggering tool call, so on a fresh surface they are never enumerated and need
 * no allowlist entry — but they are listed here for documentation completeness.
 */
const ALLOWLIST: Record<string, string> = {
  "disco:send-message": "submitting a real prompt starts a live run — proof-tier",
  "disco:replan-send": "replan submit starts a live run — proof-tier",
  "disco:steer": "steering drives a live conversation — proof-tier",
  "disco:stop": "stop/kill act on a live run — proof-tier",
  "disco:kill": "stop/kill act on a live run — proof-tier",
  "disco:kill-confirm": "destructive, acts on a live run — proof-tier",
  "disco:resume": "resume acts on a live conversation — proof-tier",
  "disco:approve-plan": "plan approval needs a live planning turn — proof-tier",
  "disco:revise-plan": "plan revision needs a live planning turn — proof-tier",
  "disco:approve-action": "gate only renders on a live risky-action turn — proof-tier",
  "disco:reject-action": "gate only renders on a live risky-action turn — proof-tier",
  "disco:answer-question": "gate only renders on a live ask_user turn — proof-tier",
  "disco:submit-clarify": "gate only renders on a live clarify turn — proof-tier",
  // AlternativesGate uses per-option handles (alternative.<optionId>) + a
  // build.alternatives-continue escape; all only render after 4 live failures.
  "disco:build.alternatives-continue": "gate only renders after 4 live failures — proof-tier",
  "disco:upload-files": "drives an OS file picker + backend upload — proof-tier",
  "disco:download-artifact": "downloads a real produced artifact — proof-tier",
  "disco:export-manifest": "exports a real project manifest — proof-tier",
  "disco:agent.live-browser": "starts a real noVNC browser session — proof-tier",
  "disco:build.edit-apply": "applies a live steer to a running build — proof-tier",
  "disco:build.edit-toggle": "toggles the live click-to-edit overlay — proof-tier",
  "disco:build.preview-refresh": "refreshes a live preview server — proof-tier",
  "disco:build.preview-restart": "restarts a live preview server — proof-tier",
  "disco:confirm-dialog.confirm": "destructive delete — needs a seeded row + live backend",
  "disco:confirm-dialog.cancel": "paired with the destructive confirm flow — proof-tier",
};

test("per-surface — enumerate every route + enforce the coverage gate", async ({
  page,
  recorder,
}) => {
  const hitMap = new HitMap();
  const perRoute: Record<string, { total: number; by_kind: Record<string, number> }> = {};
  const allControls: Control[] = [];

  for (const route of ROUTES) {
    await page.goto(route.path);
    // Give the surface a beat to mount its controls (router + suspense).
    await page.waitForLoadState("networkidle").catch(() => {});

    const controls = await enumerateControls(page);
    hitMap.recordAll(controls);
    allControls.push(...controls);

    const byKind: Record<string, number> = {};
    for (const c of controls) byKind[c.kind] = (byKind[c.kind] ?? 0) + 1;
    perRoute[route.label] = { total: controls.length, by_kind: byKind };

    // Every surface must expose at least one control (a blank route = a routing bug).
    expect(controls.length, `route ${route.path} enumerated no controls`).toBeGreaterThan(0);

    recorder.write("ui-actions.jsonl", {
      schema: "per-surface-enumeration",
      route: page.url(),
      label: route.label,
      total: controls.length,
      by_kind: byKind,
      ts: new Date().toISOString(),
    });
  }

  // #1: persist the full multi-surface inventory.
  fs.writeFileSync(
    path.join(recorder.runDir, "controls-discovered.json"),
    JSON.stringify({ per_route: perRoute, controls: allControls }, null, 2) + "\n",
    "utf-8",
  );

  // #6: the coverage report — every declared-but-unclicked backend handle.
  const report = hitMap.coverageReport(ALLOWLIST);
  fs.writeFileSync(
    path.join(recorder.runDir, "coverage-report.json"),
    JSON.stringify(report, null, 2) + "\n",
    "utf-8",
  );
  recorder.dossier();

  // The report is structurally well-formed.
  expect(report.backend_command_seen).toBeGreaterThanOrEqual(0);
  expect(Array.isArray(report.unclicked)).toBe(true);

  // #2: ENFORCE the gate. Any enabled+visible backend-command handle that was
  // enumerated, never clicked, and NOT allowlisted → throws → fails the spec.
  hitMap.assertCoverage(ALLOWLIST);
});
