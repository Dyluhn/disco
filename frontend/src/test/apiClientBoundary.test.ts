/**
 * Ratchet guard for the api/client funnel allowlist (Amendment A3).
 *
 * `eslint.config.js` carries the api/client import-boundary rule plus the list
 * of files that violated it when the rule landed. A3 requires that list to
 * SHRINK to zero by Epic 12 close. A comment saying so is not an invariant, and
 * the campaign's recurring failure mode is precisely a rule that is written
 * down but never mechanically held — so these tests hold it.
 *
 * Two prongs, both fail-closed:
 *
 *  1. The list may never grow past the ceiling measured at Epic 12-A open.
 *  2. Every allowlisted file must STILL import `@/api/client`. Migrating a file
 *     without deleting its entry would leave a dead exemption behind that
 *     silently re-permits the import later; this makes the ratchet actually
 *     turn, because a migration is only complete once the entry is gone.
 *
 * Lowering `CEILING` as the list shrinks is intentional manual work: it is the
 * one line that records progress against A3.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { describe, expect, it } from "vitest";

import config from "../../eslint.config.js";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

/**
 * The allowlist ceiling. May only decrease.
 *
 * 36 at Epic 12-A open (the measured drift when the rule landed); 33 at the
 * Epic 12-B seal, which migrated the three files it decomposed —
 * `BuildSurface.tsx`, `build/AgentCanvas.tsx` and `build/DeliverablePanel.tsx`
 * — onto `api/` modules and deleted their entries.
 */
const CEILING = 33;

type FlatConfigBlock = {
  files?: string[];
  ignores?: string[];
  rules?: Record<string, unknown>;
};

/** Narrow the config array by inspection rather than by assertion. */
function isBoundaryBlock(value: unknown): value is FlatConfigBlock {
  if (typeof value !== "object" || value === null) return false;
  const rules = (value as { rules?: unknown }).rules;
  if (typeof rules !== "object" || rules === null) return false;
  return Object.prototype.hasOwnProperty.call(rules, "no-restricted-imports");
}

function boundaryBlock(): FlatConfigBlock {
  expect(Array.isArray(config), "eslint.config.js should export an array").toBe(true);
  const found = (config as readonly unknown[]).filter(isBoundaryBlock);
  expect(
    found.length,
    "exactly one flat-config block should carry the api/client boundary rule",
  ).toBe(1);
  return found[0];
}

describe("api/client import boundary (Amendment A3)", () => {
  it("guards components/ and views/", () => {
    const block = boundaryBlock();
    expect(block.files).toEqual([
      "src/components/**/*.{ts,tsx}",
      "src/views/**/*.{ts,tsx}",
    ]);
  });

  it("has an allowlist that never grows", () => {
    const allowlist = boundaryBlock().ignores ?? [];
    expect(allowlist.length).toBeLessThanOrEqual(CEILING);
    expect(new Set(allowlist).size, "duplicate allowlist entries").toBe(allowlist.length);
    expect([...allowlist].sort()).toEqual(allowlist);
  });

  it("carries no entry that has already been migrated", () => {
    const allowlist = boundaryBlock().ignores ?? [];
    // Denominator control: an empty allowlist would pass this vacuously, and
    // that is a legitimate end state — but only once A3 is actually closed.
    // Until then, assert we are still measuring something.
    expect(allowlist.length, "allowlist is empty — close A3 and delete this test")
      .toBeGreaterThan(0);

    const stale = allowlist.filter((rel) => {
      const source = readFileSync(path.join(FRONTEND, rel), "utf8");
      return !/from\s+["']@\/api\/client["']/.test(source);
    });
    expect(
      stale,
      "these files no longer import @/api/client — delete their allowlist " +
        "entries and lower CEILING, or the ratchet never turns",
    ).toEqual([]);
  });
});
