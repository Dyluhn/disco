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

import { readFileSync, readdirSync } from "node:fs";
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
 * — onto `api/` modules and deleted their entries; 28 at the Epic 12-C seal
 * (`canvas/PreviewPane.tsx` + its versions spec, `research/NeedMoreCard.tsx`
 * and two research specs, behind `api/preview.ts` and `api/deepResearch.ts`);
 * **0 at the Epic 12-D seal, which closes A3's last clause.**
 *
 * 12-D migrated all 28: the 18 in its FE-SETTINGS/FE-SHELL family behind
 * `api/errors.ts`, `api/liveness.ts` and `api/schedules.ts`, and the 10 that no
 * decomposition boundary would ever have touched (`DemoDataBadge`,
 * `PairingGate`, `PreviewLaunchFrame` and seven under `components/build/`)
 * behind `api/session.ts`, `api/artifacts.ts`, `api/deck.ts` and
 * `api/preview.ts`. The "migrate the files you touch" rule could not reach
 * those ten, so they were done as deliberate, separately-committed work.
 */
const CEILING = 0;

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

  it("permits no exemption, and holds the invariant against a real denominator", () => {
    // A3 is closed: the ratchet reached zero at the Epic 12-D seal. The old
    // prong here asserted "no allowlisted file has already been migrated",
    // which was the right invariant while the list was shrinking. With the
    // list empty that question is vacuous, so it is replaced by the strictly
    // stronger one: there may be no exemption, and re-adding one is a
    // regression rather than a migration step.
    expect(
      boundaryBlock().ignores ?? [],
      "the api/client funnel admits no exemptions — an entry here is a " +
        "regression; route the import through an api/ module or a hook",
    ).toEqual([]);

    // Both assertions live in ONE test id deliberately: the campaign's test
    // inventory is pinned by id, and a decomposition boundary should not spend
    // an inventory transition on reporting granularity. Coverage is identical
    // — both prongs still run and still fail closed.
    //
    // The rule and its (now empty) allowlist are ESLint configuration; this
    // asserts the property they exist to produce, so the guard cannot pass by
    // the rule silently ceasing to apply. Counting the files scanned is the
    // denominator control — a diff of two empty sets passes cheerfully.
    const roots = ["src/components", "src/views"];
    const offenders: string[] = [];
    let scanned = 0;

    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
        } else if (/\.tsx?$/.test(entry.name)) {
          scanned += 1;
          const source = readFileSync(full, "utf8");
          if (/from\s+["']@\/api\/client["']/.test(source)) {
            offenders.push(path.relative(FRONTEND, full));
          }
        }
      }
    };
    for (const root of roots) walk(path.join(FRONTEND, root));

    expect(scanned, "scanned no files — the denominator is empty").toBeGreaterThan(100);
    expect(
      offenders.sort(),
      "these files under components/ or views/ statically import @/api/client",
    ).toEqual([]);
  });
});
