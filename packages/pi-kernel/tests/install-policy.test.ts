/**
 * Install-path policy assertions (P2: --ignore-scripts must be enforced on the
 * actual install command, not just package-local .npmrc). Verifies that:
 *  - .npmrc pins `ignore-scripts=true`;
 *  - the documented `setup` script forces `--ignore-scripts` regardless of cwd;
 *  - a `preinstall` guard fails loudly when ignore-scripts is not set, so a
 *    locked dependency's lifecycle script can never silently run.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG_DIR = resolve(HERE, "..");

function read(rel: string): string {
  return readFileSync(resolve(PKG_DIR, rel), "utf8");
}

describe("Pi sidecar install policy (P2: ignore-scripts enforced on the install path)", () => {
  it(".npmrc pins ignore-scripts=true", () => {
    const npmrc = read(".npmrc");
    expect(/^\s*ignore-scripts\s*=\s*true\s*$/m.test(npmrc)).toBe(true);
  });

  it("the documented setup script forces --ignore-scripts regardless of cwd", () => {
    const pkg = JSON.parse(read("package.json")) as { scripts?: Record<string, string> };
    const setup = pkg.scripts?.setup ?? "";
    expect(setup).toContain("npm install");
    expect(setup).toContain("--ignore-scripts");
  });

  it("a preinstall guard fails loudly when ignore-scripts is not set", () => {
    const pkg = JSON.parse(read("package.json")) as { scripts?: Record<string, string> };
    const preinstall = pkg.scripts?.preinstall ?? "";
    // The guard inspects npm's ignore-scripts config and exits non-zero when it
    // is anything other than true.
    expect(preinstall).toContain("npm_config_ignore_scripts");
    expect(preinstall).toContain("process.exit(1)");
  });
});
