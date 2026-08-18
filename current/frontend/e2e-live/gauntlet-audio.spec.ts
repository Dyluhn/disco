import { test, expect } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Close-out gauntlet — Pillar A audio: generate an audio overview from an
 * EXISTING finished deep-research report through the real UI (the report-bottom
 * "Generate audio overview" → single-voice mode), and require the real done
 * state (AudioPlayer + Export MP3) — never a fake affordance.
 *
 *   AUDIO_CID    the finished DR conversation id (required)
 *   AUDIO_BUDGET wait budget ms for synthesis (default 15 min — kokoro on CPU)
 */

const CID = process.env.AUDIO_CID ?? "";
const BUDGET = Number(process.env.AUDIO_BUDGET ?? 900_000);
// DISCO_E2E_EVIDENCE_DIR overrides the default; falls back to a repo-relative
// path (like the other e2e-live specs) so this runs on any machine unconfigured.
const EVID = process.env.DISCO_E2E_EVIDENCE_DIR
  ? path.join(process.env.DISCO_E2E_EVIDENCE_DIR, "audio")
  : path.resolve(__dirname, "../../test-record/gauntlet/audio");

test(`gauntlet AUDIO: report → single-voice audio overview with real player/export`, async ({ page }) => {
  test.setTimeout(BUDGET + 300_000);
  expect(CID, "AUDIO_CID env is required").toBeTruthy();
  fs.mkdirSync(EVID, { recursive: true });
  const shot = (n: string) => page.screenshot({ path: `${EVID}/${n}.png`, fullPage: true });

  await page.goto(`/deep/${CID}`);
  // Report loaded oracle — the Cited tab mounts only with a finished report.
  await expect(page.getByRole("tab", { name: /cited/i })).toBeVisible({ timeout: 120_000 });
  await shot("01-report");

  // The audio entry point at the report bottom. Label differs by view: a live
  // session renders "Generate audio overview", a RESUMED conversation renders
  // "Audio Overview" (probe 2026-07-07) — match the stable common substring.
  // FAIL FAST on the lookup (30s): the first run burned 13 MINUTES hanging on a
  // wrong label via an un-capped scrollIntoViewIfNeeded — a dead locator must
  // never masquerade as slow synthesis (audio is FAST: ~45s for 11 turns).
  const gen = page.getByRole("button", { name: /audio overview/i }).first();
  await gen.waitFor({ state: "visible", timeout: 30_000 });
  await gen.scrollIntoViewIfNeeded();
  await gen.click();
  await shot("02-audio-modes");

  // Single-voice mode (dr.audio.single) — same fail-fast bound.
  const single = page.locator('[data-disco-control="dr.audio.single"]').first();
  await single.waitFor({ state: "visible", timeout: 30_000 });
  await single.click();
  await shot("03-audio-requested");

  // DONE oracle: the export control renders ONLY with a real audio URL. This is
  // the one legitimately time-bound wait (synthesis) — but kokoro does a full
  // report in under a minute, so 3 min is already generous headroom.
  const exportBtn = page.locator('[data-disco-control="dr.audio.export"]').first();
  await exportBtn.waitFor({ state: "visible", timeout: Math.min(BUDGET, 180_000) });
  await shot("04-audio-done");

  // No error/degraded text on the card.
  const body = await page.locator("body").innerText();
  expect(body).not.toMatch(/audio (generation )?(failed|error)/i);
  fs.writeFileSync(`${EVID}/RESULT.txt`, `cid=${CID}\naudio=done\n`);
});
