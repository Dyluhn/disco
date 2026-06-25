/**
 * Install-path policy assertions (round-2 P2 #3: the SANCTIONED install command
 * is the enforcement point, not the preinstall guard). Verifies that:
 *  - .npmrc pins `ignore-scripts=true` as the package-local default;
 *  - the documented `setup` script forces `--ignore-scripts` regardless of cwd
 *    (THIS is the enforced path — the real backstop);
 *  - the policy + enforcement point is spelled out explicitly (an `_install_policy`
 *    note in package.json);
 *  - the `preinstall` guard is downgraded to a clearly-labeled BEST-EFFORT
 *    advisory warning — it does NOT hard-fail and is NOT presented as a guarantee
 *    (it cannot reliably prevent a dependency script that already ran).
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

describe("Pi sidecar install policy (P2 #3: sanctioned install path is the enforcement)", () => {
  it(".npmrc pins ignore-scripts=true (package-local default)", () => {
    const npmrc = read(".npmrc");
    expect(/^\s*ignore-scripts\s*=\s*true\s*$/m.test(npmrc)).toBe(true);
  });

  it("the sanctioned setup script (the ENFORCEMENT point) forces --ignore-scripts regardless of cwd", () => {
    const pkg = JSON.parse(read("package.json")) as { scripts?: Record<string, string> };
    const setup = pkg.scripts?.setup ?? "";
    expect(setup).toContain("npm install");
    expect(setup).toContain("--ignore-scripts");
  });

  it("the policy + enforcement point is stated explicitly in package.json", () => {
    const pkg = JSON.parse(read("package.json")) as { scripts?: Record<string, string> };
    const note = pkg.scripts?._install_policy ?? "";
    expect(note).toContain("--ignore-scripts");
    expect(note.toLowerCase()).toContain("enforcement");
    // The note must name the sanctioned command as the real path.
    expect(note).toContain("npm run setup");
  });

  it("the preinstall guard is a clearly-labeled best-effort warning, NOT a hard-fail guarantee", () => {
    const pkg = JSON.parse(read("package.json")) as { scripts?: Record<string, string> };
    const preinstall = pkg.scripts?.preinstall ?? "";
    // It still inspects npm's ignore-scripts config to warn on a misconfigured install...
    expect(preinstall).toContain("npm_config_ignore_scripts");
    // ...but it is DOWNGRADED: no hard failure and not presented as a guarantee.
    expect(preinstall).not.toContain("process.exit");
    expect(preinstall.toLowerCase()).toContain("best-effort");
    // It points operators at the actually-enforced path.
    expect(preinstall).toContain("--ignore-scripts");
  });
});

describe("Pi sidecar engines (P2 #4: declared node range matches the dependency)", () => {
  it("engines.node is >=22.19.0 to match pi-coding-agent's requirement", () => {
    const pkg = JSON.parse(read("package.json")) as { engines?: { node?: string } };
    expect(pkg.engines?.node).toBe(">=22.19.0");
  });

  it("the declared node range is not LESS than the pi-coding-agent dependency requires", () => {
    const pkg = JSON.parse(read("package.json")) as { engines?: { node?: string } };
    const depPkg = JSON.parse(
      read("node_modules/@earendil-works/pi-coding-agent/package.json"),
    ) as { engines?: { node?: string } };

    const parse = (range: string | undefined): [number, number, number] => {
      const m = /(\d+)\.(\d+)\.(\d+)/.exec(range ?? "");
      if (!m) throw new Error(`unparseable node range: ${range}`);
      return [Number(m[1]), Number(m[2]), Number(m[3])];
    };

    const ours = parse(pkg.engines?.node);
    const theirs = parse(depPkg.engines?.node);
    // Our floor must be >= the dependency's floor, component-wise.
    const cmp = ours[0] - theirs[0] || ours[1] - theirs[1] || ours[2] - theirs[2];
    expect(cmp).toBeGreaterThanOrEqual(0);
  });
});
