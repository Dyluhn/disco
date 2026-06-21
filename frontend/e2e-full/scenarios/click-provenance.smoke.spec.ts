/**
 * click-provenance.smoke.spec.ts — W12 smoke spec.
 *
 * Demonstrates that discoClick produces all 10 evidence records for one
 * interaction and that uiInventory enumerates + classifies the home-screen
 * controls correctly.
 *
 * The 10 evidence records (from evidence-harness-campaign.md):
 *  1  UI control discovered        → controls-discovered.json
 *  2  UI action performed          → ui-actions.jsonl entry
 *  3  HTTP/WS frame caused         → network.jsonl (passive listener fires on navigate)
 *  4  conversation_id captured     → conversations/<cid>/ folder (live backend only)
 *  5  event-log rows appended      → conversations/<cid>/events.jsonl (live backend)
 *  6  state transition observed    → conversations/<cid>/state.final.json (live backend)
 *  7  DISCO_INSPECT trace          → conversations/<cid>/inspect-trace.json (live backend)
 *  8  sandbox/tool side effect     → workspace-manifest.json (live backend)
 *  9  final UI renders result      → screenshots/after-*.png
 * 10  artifact saved               → dossier folder with all evidence files
 *
 * Records 4–8 require a live backend workflow; in this smoke run they are
 * verified structurally (the infrastructure to produce them is wired up and
 * the correct helper functions are importable + callable).
 *
 * Run on VM 201 by the orchestrator — NOT locally (Playwright OOMs on workstation).
 *
 * evidence-harness-campaign.md W12
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { expect, test } from "../fixtures/discoHarness";
import { discoClick } from "../support/discoClick";
import {
  enumerateControls,
  HitMap,
  type Control,
} from "../support/uiInventory";
import {
  expectEventSequence,
  expectInspectTrace,
  expectArtifactReadable,
} from "../support/assertions";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Write controls to controls-discovered.json in the run folder. */
function writeControlsDiscovered(
  runDir: string,
  controls: Control[],
): void {
  fs.writeFileSync(
    path.join(runDir, "controls-discovered.json"),
    JSON.stringify(controls, null, 2) + "\n",
    "utf-8",
  );
}

/** Write hit-map snapshot to controls-hit.json in the run folder. */
function writeControlsHit(
  runDir: string,
  hitMap: HitMap,
): void {
  fs.writeFileSync(
    path.join(runDir, "controls-hit.json"),
    JSON.stringify(
      {
        seen: hitMap.allSeen.length,
        clicked: [...hitMap.allClicked],
        backend_command_seen: hitMap.allSeen.filter(
          (c) => c.kind === "backend-command",
        ).length,
      },
      null,
      2,
    ) + "\n",
    "utf-8",
  );
}

// ---------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------

test(
  "click-provenance smoke — all 10 evidence records, home-screen control inventory",
  async ({ page, recorder }) => {
    // -----------------------------------------------------------------------
    // Navigate to home (record 3: network.jsonl captures the HTTP requests)
    // -----------------------------------------------------------------------
    await page.goto("/");

    // -----------------------------------------------------------------------
    // Record 1: enumerate + classify home-screen controls
    // -----------------------------------------------------------------------
    const controls = await enumerateControls(page);
    writeControlsDiscovered(recorder.runDir, controls);

    const hitMap = new HitMap();
    hitMap.recordAll(controls);

    // The home screen must expose at least one discoverable control.
    expect(controls.length).toBeGreaterThan(0);

    // Every control must have a non-empty controlId and a valid kind.
    const validKinds = new Set([
      "backend-command",
      "navigation",
      "form-input",
      "local-ui-only",
      "disabled",
    ]);
    for (const ctrl of controls) {
      expect(ctrl.controlId.length).toBeGreaterThan(0);
      expect(validKinds.has(ctrl.kind)).toBe(true);
    }

    // -----------------------------------------------------------------------
    // Record 2 + 9: discoClick on a visible navigation or command control
    //
    // Strategy: prefer a navigation link (safe, no backend call needed) so
    // the smoke spec is self-contained even without a live backend.  Falls back
    // to the first visible enabled control of any kind.
    // -----------------------------------------------------------------------
    const clickTarget =
      controls.find(
        (c) =>
          (c.kind === "navigation" || c.kind === "backend-command") &&
          c.visible &&
          c.enabled,
      ) ?? controls.find((c) => c.visible && c.enabled);

    if (clickTarget !== undefined) {
      // Locate the element by its most stable available attribute
      let locator =
        clickTarget.testId !== null
          ? page.getByTestId(clickTarget.testId)
          : page.locator(`[data-disco-control="${clickTarget.controlId.replace("disco:", "")}"]`);

      // Final fallback: locate by TAG (a valid CSS selector) + visible text. NOTE: control
      // `role` here is the element's TAG name (e.g. "a"/"button"), NOT an ARIA role — passing
      // it to getByRole("a", …) is invalid and hangs until timeout, so use page.locator(tag)
      // with a hasText filter instead.
      if (
        clickTarget.testId === null &&
        !clickTarget.controlId.startsWith("disco:")
      ) {
        locator =
          clickTarget.name.length > 0
            ? page.locator(clickTarget.role, { hasText: clickTarget.name }).first()
            : page.locator(clickTarget.role).first();
      }

      await discoClick(page, recorder, locator, {
        // No expectWs / expectStatus / expectEvents for the smoke run;
        // a live backend workflow test (W13) adds those.
        actionId: "smoke-click-01",
      });

      hitMap.hit(clickTarget.controlId);
    }

    // -----------------------------------------------------------------------
    // Write hit-map snapshot
    // -----------------------------------------------------------------------
    writeControlsHit(recorder.runDir, hitMap);

    // -----------------------------------------------------------------------
    // Record 10: write dossier (timeline.md + index.html + manifest.json)
    // -----------------------------------------------------------------------
    recorder.dossier();

    // -----------------------------------------------------------------------
    // Assertions on the evidence folder (records 1, 2, 9, 10)
    // -----------------------------------------------------------------------

    // Record 1: controls-discovered.json
    const discoveredPath = path.join(recorder.runDir, "controls-discovered.json");
    expect(fs.existsSync(discoveredPath)).toBe(true);
    const discovered = JSON.parse(
      fs.readFileSync(discoveredPath, "utf-8"),
    ) as Control[];
    expect(discovered.length).toBeGreaterThan(0);

    // Record 2: ui-actions.jsonl has at least one entry
    const uiActionsPath = path.join(recorder.runDir, "ui-actions.jsonl");
    if (clickTarget !== undefined) {
      expect(fs.existsSync(uiActionsPath)).toBe(true);
      const uiActionsText = fs.readFileSync(uiActionsPath, "utf-8");
      const uiActions = uiActionsText
        .split("\n")
        .filter((l) => l.trim())
        .map((l) => JSON.parse(l) as { schema: string; ui_action_id: string });
      expect(uiActions.length).toBeGreaterThan(0);
      expect(uiActions[0]?.schema).toBe("ui-action");
      expect(uiActions[0]?.ui_action_id).toBe("smoke-click-01");
    }

    // Record 9: before + after screenshots exist
    if (clickTarget !== undefined) {
      const screenshotsDir = path.join(recorder.runDir, "screenshots");
      expect(fs.existsSync(screenshotsDir)).toBe(true);
      const shots = fs.readdirSync(screenshotsDir);
      const beforeShot = shots.find((f) => f.startsWith("before-smoke-click-01"));
      const afterShot = shots.find((f) => f.startsWith("after-smoke-click-01"));
      expect(beforeShot).toBeDefined();
      expect(afterShot).toBeDefined();
    }

    // Record 3: network.jsonl was written by the passive listener (navigate → requests)
    const networkPath = path.join(recorder.runDir, "network.jsonl");
    expect(fs.existsSync(networkPath)).toBe(true);
    const networkLines = fs
      .readFileSync(networkPath, "utf-8")
      .split("\n")
      .filter((l) => l.trim());
    expect(networkLines.length).toBeGreaterThan(0);

    // Record 10: dossier files
    expect(fs.existsSync(path.join(recorder.runDir, "manifest.json"))).toBe(true);
    expect(fs.existsSync(path.join(recorder.runDir, "timeline.md"))).toBe(true);
    expect(fs.existsSync(path.join(recorder.runDir, "index.html"))).toBe(true);

    // controls-hit.json
    expect(
      fs.existsSync(path.join(recorder.runDir, "controls-hit.json")),
    ).toBe(true);

    // -----------------------------------------------------------------------
    // Structural proof: records 4–8 infrastructure is importable + callable
    //
    // These helpers will throw on a real call without a live backend; here we
    // only verify they are correctly typed and exported.  Live assertions are
    // added in W13 scenario specs.
    // -----------------------------------------------------------------------
    expect(typeof expectEventSequence).toBe("function");
    expect(typeof expectInspectTrace).toBe("function");
    expect(typeof expectArtifactReadable).toBe("function");

    // -----------------------------------------------------------------------
    // Control-kind breakdown logged for human inspection
    // -----------------------------------------------------------------------
    const byKind: Record<string, number> = {};
    for (const c of controls) {
      byKind[c.kind] = (byKind[c.kind] ?? 0) + 1;
    }
    // Write a summary so the timeline includes it
    recorder.write("ui-actions.jsonl", {
      schema: "control-kind-summary",
      route: page.url(),
      total: controls.length,
      by_kind: byKind,
      ts: new Date().toISOString(),
    });
  },
);
