/**
 * Disco skill-allowlist policy tests (EPIC G — PR G1/G2/G3, §1.1–§1.3 + §5.2).
 *
 * Proves the NON-NEGOTIABLE invariant at LOAD TIME (not by convention): Pi may
 * load ONLY reviewed, Disco-owned, allowlisted skills — never a project-local
 * `.pi/skills` skill, a user/global skill, or any arbitrary skill path; never a
 * skill reached via a symlink that escapes the controlled root; and only a skill
 * that declares a valid §5.2 governance manifest.
 *
 * The checks run against the REAL Pi SDK `DefaultResourceLoader` (with the exact
 * `noSkills` + `additionalSkillPaths` + `skillsOverride` posture the sidecar
 * uses) and the real `PiKernelRunner`, so they verify the actual wiring, not a
 * mock of it.
 */
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  DefaultResourceLoader,
  SettingsManager,
  type Skill,
} from "@earendil-works/pi-coding-agent";
import { afterEach, describe, expect, it } from "vitest";

import { PiKernelRunner } from "../src/runner.ts";
import type { KernelOutbound } from "../src/protocol.ts";
import {
  DISCO_SKILLS_ROOT,
  DISCO_SKILL_ALLOWLIST,
  SkillAllowlistViolation,
  assertOnlyAllowlistedSkills,
  buildSkillLoaderConfig,
  enforceAllowlist,
  isAllowlistedSkill,
  loadAllowlistedSkillManifests,
  readSkillManifest,
  resolveAllowlistedSkillPaths,
  type SkillSet,
} from "../src/skills.ts";
import { DISCO_TOOL_NAMES } from "../src/tools.ts";

// ---- temp-dir helpers --------------------------------------------------

const tmpDirs: string[] = [];

function mkTemp(prefix: string): string {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  tmpDirs.push(dir);
  return dir;
}

/**
 * Write a minimal but GOVERNANCE-VALID skill (`<dir>/<name>/SKILL.md`) and return
 * its dir. The frontmatter carries the §5.2 fields (id/version/surface/
 * allowed-tools/risk) the resolver validates for an accepted in-root skill.
 */
function writeSkill(root: string, name: string): string {
  const dir = join(root, name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, "SKILL.md"),
    `---\n` +
      `name: ${name}\n` +
      `id: ${name}\n` +
      `version: 1.0.0\n` +
      `surface: [build, agent]\n` +
      `allowed-tools: [think, file_read]\n` +
      `risk: low\n` +
      `description: test skill ${name}\n` +
      `---\n\n# ${name}\n`,
  );
  return dir;
}

/** A loaded-skill stub shaped like the SDK `Skill` (only fields the gate reads). */
function fakeSkill(name: string, filePath: string): Skill {
  const baseDir = filePath.replace(/\/SKILL\.md$/, "");
  return {
    name,
    description: "x",
    filePath,
    baseDir,
    sourceInfo: { path: filePath, source: "test", scope: "temporary", origin: "top-level" },
    disableModelInvocation: false,
  };
}

afterEach(() => {
  while (tmpDirs.length) {
    const d = tmpDirs.pop()!;
    try {
      rmSync(d, { recursive: true, force: true });
    } catch {
      /* best-effort */
    }
  }
});

// ---- the allowlist registry --------------------------------------------

describe("Disco skill allowlist registry (PR G1)", () => {
  it("ships an EMPTY default allowlist (safe default: zero skills exposed)", () => {
    expect(DISCO_SKILL_ALLOWLIST).toEqual([]);
  });

  it("resolves an empty allowlist to zero paths", () => {
    expect(resolveAllowlistedSkillPaths({ allowlist: [], skillsRoot: DISCO_SKILLS_ROOT })).toEqual(
      [],
    );
  });

  it("resolves a vetted in-repo Disco skill to its absolute dir", () => {
    const paths = resolveAllowlistedSkillPaths({ allowlist: ["build-basic"] });
    expect(paths).toHaveLength(1);
    expect(paths[0]).toContain(join("skills", "build-basic"));
  });

  it("throws loudly if a vetted skill has no SKILL.md on disk (misconfig)", () => {
    expect(() => resolveAllowlistedSkillPaths({ allowlist: ["does-not-exist"] })).toThrow(
      /missing its SKILL\.md/,
    );
  });

  it("rejects a traversing / unsafe allowlist entry", () => {
    expect(() => resolveAllowlistedSkillPaths({ allowlist: ["../escape"] })).toThrow(
      /invalid allowlisted skill name/,
    );
  });
});

// ---- governance manifest (campaign §5.2) -------------------------------

describe("skill governance manifest (PR G1, §5.2)", () => {
  it("parses version/surface/allowed-tools/risk for the reviewed in-repo skills", () => {
    const manifests = loadAllowlistedSkillManifests({
      allowlist: ["build-basic", "preview-repair"],
    });
    const byId = new Map(manifests.map((m) => [m.id, m]));

    const buildBasic = byId.get("build-basic");
    expect(buildBasic).toBeDefined();
    expect(buildBasic?.version).toBe("1.0.0");
    expect(buildBasic?.surface).toEqual(["build", "agent"]);
    expect(buildBasic?.risk).toBe("low");
    expect(buildBasic?.allowedTools).toContain("submit_plan");
    expect(buildBasic?.allowedTools).toContain("preview_start");

    const previewRepair = byId.get("preview-repair");
    expect(previewRepair).toBeDefined();
    expect(previewRepair?.surface).toEqual(["build", "agent"]);
    expect(previewRepair?.risk).toBe("medium");
    expect(previewRepair?.allowedTools).toContain("preview_logs");
    // preview-repair must NOT finish the run itself.
    expect(previewRepair?.allowedTools).not.toContain("finish");
  });

  it("REFUSES to resolve an allowlisted skill whose SKILL.md omits §5.2 fields", () => {
    // A skill dir that exists and is in-root but whose frontmatter has no
    // version/surface/allowed-tools/risk must be rejected at LOAD TIME.
    const root = mkTemp("disco-gov-bad-");
    const dir = join(root, "ungoverned");
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, "SKILL.md"),
      `---\nname: ungoverned\nid: ungoverned\ndescription: no governance fields\n---\n# x\n`,
    );
    expect(() =>
      resolveAllowlistedSkillPaths({ allowlist: ["ungoverned"], skillsRoot: root }),
    ).toThrow(/missing a 'version' field/);
  });

  it("readSkillManifest rejects a frontmatter id that disagrees with the dir id", () => {
    const root = mkTemp("disco-gov-idmismatch-");
    const dir = join(root, "real-name");
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, "SKILL.md"),
      `---\nname: real-name\nid: spoofed\nversion: 1.0.0\nsurface: [build]\n` +
        `allowed-tools: [think]\nrisk: low\ndescription: x\n---\n# x\n`,
    );
    expect(() => readSkillManifest("real-name", join(dir, "SKILL.md"))).toThrow(
      /frontmatter id must match the directory id/,
    );
  });

  it("readSkillManifest REJECTS a matching id but MISMATCHED name (P2 name binding)", () => {
    // The id equals the directory id (passes the id check), but the frontmatter
    // `name` — which the Pi SDK actually mounts the skill under — is arbitrary.
    // Governance must refuse it so the live mounted name can't differ from the id.
    const root = mkTemp("disco-gov-namebind-");
    const dir = join(root, "build-basic");
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, "SKILL.md"),
      `---\nname: arbitrary-skill\nid: build-basic\nversion: 1.0.0\nsurface: [build]\n` +
        `allowed-tools: [think]\nrisk: low\ndescription: x\n---\n# x\n`,
    );
    expect(() => readSkillManifest("build-basic", join(dir, "SKILL.md"))).toThrow(
      /frontmatter name must match the directory id/,
    );
  });

  it("readSkillManifest ACCEPTS a manifest whose id AND name both match the dir id", () => {
    const root = mkTemp("disco-gov-nameok-");
    const dir = writeSkill(root, "build-basic"); // writes matching name+id
    const m = readSkillManifest("build-basic", join(dir, "SKILL.md"));
    expect(m.id).toBe("build-basic");
    expect(m.name).toBe("build-basic");
  });

  it("readSkillManifest REJECTS an unknown allowed-tool (P2 fail-open fix)", () => {
    // `allowed-tools` is non-empty but names a tool outside the fixed Disco set —
    // must be refused, not silently passed.
    const root = mkTemp("disco-gov-badtool-");
    const dir = join(root, "toolsy");
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, "SKILL.md"),
      `---\nname: toolsy\nid: toolsy\nversion: 1.0.0\nsurface: [build]\n` +
        `allowed-tools: [think, totally_not_a_disco_tool]\nrisk: low\ndescription: x\n---\n# x\n`,
    );
    expect(() => readSkillManifest("toolsy", join(dir, "SKILL.md"))).toThrow(
      /allowed-tool 'totally_not_a_disco_tool', which is not a Disco tool/,
    );
  });

  it("readSkillManifest ACCEPTS a manifest whose allowed-tools are all real Disco tools", () => {
    const root = mkTemp("disco-gov-goodtool-");
    const dir = join(root, "toolsy-ok");
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, "SKILL.md"),
      `---\nname: toolsy-ok\nid: toolsy-ok\nversion: 1.0.0\nsurface: [build]\n` +
        `allowed-tools: [think, file_read, shell_exec, preview_start, finish]\nrisk: low\n` +
        `description: x\n---\n# x\n`,
    );
    const m = readSkillManifest("toolsy-ok", join(dir, "SKILL.md"));
    expect(m.allowedTools).toEqual(["think", "file_read", "shell_exec", "preview_start", "finish"]);
  });

  it("the real in-repo skills declare ONLY tools from the fixed Disco set", () => {
    const manifests = loadAllowlistedSkillManifests({
      allowlist: ["build-basic", "preview-repair"],
    });
    for (const m of manifests) {
      for (const tool of m.allowedTools) {
        expect(DISCO_TOOL_NAMES).toContain(tool);
      }
    }
  });

  it("readSkillManifest rejects an invalid risk tier", () => {
    const root = mkTemp("disco-gov-risk-");
    const dir = join(root, "riskyskill");
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, "SKILL.md"),
      `---\nname: riskyskill\nid: riskyskill\nversion: 1.0.0\nsurface: [build]\n` +
        `allowed-tools: [think]\nrisk: catastrophic\ndescription: x\n---\n# x\n`,
    );
    expect(() => readSkillManifest("riskyskill", join(dir, "SKILL.md"))).toThrow(/invalid risk/);
  });
});

// ---- symlink confinement (P1: allowlisted dir must not escape the root) ----

describe("allowlisted skill-dir symlink confinement (P1)", () => {
  it("REJECTS an allowlisted skill DIR that is a symlink escaping the root", () => {
    // `<root>/build-basic` is a symlink to an out-of-root dir that has a SKILL.md.
    // The single-segment name check passes and SKILL.md exists, but the resolved
    // real dir lands outside the controlled root → must be refused outright.
    const root = mkTemp("disco-symroot-");
    const outside = mkTemp("disco-evil-");
    writeFileSync(
      join(outside, "SKILL.md"),
      `---\nname: build-basic\ndescription: evil\n---\n# evil\n`,
    );
    symlinkSync(outside, join(root, "build-basic"), "dir");

    expect(() =>
      resolveAllowlistedSkillPaths({ allowlist: ["build-basic"], skillsRoot: root }),
    ).toThrow(SkillAllowlistViolation);
    expect(() =>
      resolveAllowlistedSkillPaths({ allowlist: ["build-basic"], skillsRoot: root }),
    ).toThrow(/escapes the controlled skills root/);
  });

  it("REJECTS an allowlisted skill whose SKILL.md is a symlink escaping the root", () => {
    // The dir is genuinely in-root, but its SKILL.md is a symlink to an outside
    // file — the same escape one level down → refused.
    const root = mkTemp("disco-symmd-root-");
    const dir = join(root, "build-basic");
    mkdirSync(dir, { recursive: true });
    const outside = mkTemp("disco-symmd-evil-");
    const outsideMd = join(outside, "EVIL.md");
    writeFileSync(outsideMd, `---\nname: build-basic\ndescription: evil\n---\n# evil\n`);
    symlinkSync(outsideMd, join(dir, "SKILL.md"), "file");

    expect(() =>
      resolveAllowlistedSkillPaths({ allowlist: ["build-basic"], skillsRoot: root }),
    ).toThrow(/SKILL\.md that resolves to .*outside its controlled directory/s);
  });

  it("ACCEPTS a normal in-root skill (canonicalizes to its own real dir)", () => {
    const root = mkTemp("disco-symok-root-");
    writeSkill(root, "build-basic");
    const paths = resolveAllowlistedSkillPaths({ allowlist: ["build-basic"], skillsRoot: root });
    expect(paths).toHaveLength(1);
    expect(paths[0]).toContain(join("build-basic"));
  });

  it("ACCEPTS a symlinked skill dir that stays WITHIN the root", () => {
    // `<root>/aliased` → `<root>/real-build` (both under the root) is allowed: the
    // real dir is still contained, so confinement is preserved. The manifest id
    // matches the allowlisted name (`aliased`) so the §5.2 governance check that
    // frontmatter identity == allowlist identity also passes.
    const root = mkTemp("disco-syminroot-");
    const real = join(root, "real-build");
    mkdirSync(real, { recursive: true });
    writeFileSync(
      join(real, "SKILL.md"),
      `---\nname: aliased\nid: aliased\nversion: 1.0.0\nsurface: [build]\n` +
        `allowed-tools: [think]\nrisk: low\ndescription: aliased skill\n---\n# aliased\n`,
    );
    symlinkSync(real, join(root, "aliased"), "dir");
    const paths = resolveAllowlistedSkillPaths({ allowlist: ["aliased"], skillsRoot: root });
    expect(paths).toHaveLength(1);
    expect(paths[0]).toContain(join("real-build"));
  });

  it("boot assertion REJECTS an allowedDir that escapes the root (backstop)", () => {
    // Even if an escaped real-path ever reached `allowedDirs`, the boot assertion
    // re-runs the canonicalized-containment check against the real root.
    const root = mkTemp("disco-bootroot-");
    const outside = mkTemp("disco-bootevil-");
    expect(() => assertOnlyAllowlistedSkills([], [outside], root)).toThrow(
      /allowlisted dir\(s\) escape the controlled skills root/,
    );
  });
});

// ---- the load-time gate (pure) -----------------------------------------

describe("allowlist load-time gate (PR G3)", () => {
  it("passes a skill loaded from within an allowlisted dir", () => {
    const root = mkTemp("disco-skills-ok-");
    const dir = writeSkill(root, "build-basic");
    const base: SkillSet = { skills: [fakeSkill("build-basic", join(dir, "SKILL.md"))], diagnostics: [] };
    expect(() => enforceAllowlist(base, [dir])).not.toThrow();
    expect(enforceAllowlist(base, [dir]).skills).toHaveLength(1);
  });

  it("THROWS on a skill that is not under any allowlisted dir", () => {
    const evil = mkTemp("disco-skills-evil-");
    const dir = writeSkill(evil, "malicious");
    const base: SkillSet = { skills: [fakeSkill("malicious", join(dir, "SKILL.md"))], diagnostics: [] };
    expect(() => enforceAllowlist(base, [])).toThrow(SkillAllowlistViolation);
    expect(() => enforceAllowlist(base, [])).toThrow(/non-allowlisted skill/);
  });

  it("isAllowlistedSkill is path-containment based, not frontmatter-name based", () => {
    const root = mkTemp("disco-skills-name-");
    const dir = writeSkill(root, "real");
    // A skill that LIES about its name but lives outside the allowlisted dir.
    const liar = fakeSkill("build-basic", join(mkTemp("disco-skills-liar-"), "SKILL.md"));
    expect(isAllowlistedSkill(liar, [dir])).toBe(false);
    expect(isAllowlistedSkill(fakeSkill("real", join(dir, "SKILL.md")), [dir])).toBe(true);
  });

  it("enforceAllowlist REJECTS a skill whose LIVE name differs from the reviewed id (P2)", () => {
    // The skill loads from inside the reviewed `build-basic` dir (path gate passes)
    // but mounts under an arbitrary name — the live name binding must refuse it.
    const root = mkTemp("disco-namebind-gate-");
    const dir = writeSkill(root, "build-basic");
    const base: SkillSet = {
      skills: [fakeSkill("arbitrary-skill", join(dir, "SKILL.md"))],
      diagnostics: [],
    };
    expect(() => enforceAllowlist(base, [dir], [{ name: "build-basic", dir }])).toThrow(
      SkillAllowlistViolation,
    );
    expect(() => enforceAllowlist(base, [dir], [{ name: "build-basic", dir }])).toThrow(
      /live mounted name differs from the reviewed allowlist id/,
    );
  });

  it("enforceAllowlist ACCEPTS a skill whose live name matches the reviewed id", () => {
    const root = mkTemp("disco-namebind-ok-");
    const dir = writeSkill(root, "build-basic");
    const base: SkillSet = {
      skills: [fakeSkill("build-basic", join(dir, "SKILL.md"))],
      diagnostics: [],
    };
    expect(() => enforceAllowlist(base, [dir], [{ name: "build-basic", dir }])).not.toThrow();
  });

  it("boot assertion backstops the live name binding (mismatched name in final set)", () => {
    const root = mkTemp("disco-namebind-boot-");
    const dir = writeSkill(root, "build-basic");
    expect(() =>
      assertOnlyAllowlistedSkills(
        [fakeSkill("arbitrary-skill", join(dir, "SKILL.md"))],
        [dir],
        undefined,
        [{ name: "build-basic", dir }],
      ),
    ).toThrow(/under a name differing from their reviewed allowlist id/);
  });

  it("boot assertion fires loudly on a non-allowlisted skill in the final set", () => {
    const evil = mkTemp("disco-skills-boot-");
    const dir = writeSkill(evil, "sneaky");
    expect(() =>
      assertOnlyAllowlistedSkills([fakeSkill("sneaky", join(dir, "SKILL.md"))], []),
    ).toThrow(/boot assertion failed/);
  });
});

// ---- against the REAL Pi SDK loader ------------------------------------

/** Build a loader with the EXACT posture the sidecar uses (EPIC G + §5.1). */
function makeLoader(opts: {
  cwd: string;
  allowlist: readonly string[];
  skillsRoot: string;
}): DefaultResourceLoader {
  const agentDir = mkTemp("disco-agent-");
  const settingsManager = SettingsManager.create(opts.cwd, agentDir);
  const cfg = buildSkillLoaderConfig({ allowlist: opts.allowlist, skillsRoot: opts.skillsRoot });
  return new DefaultResourceLoader({
    cwd: opts.cwd,
    agentDir,
    settingsManager,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    additionalSkillPaths: cfg.additionalSkillPaths,
    skillsOverride: cfg.skillsOverride,
  });
}

describe("real DefaultResourceLoader under the Disco allowlist", () => {
  it("loads ONLY allowlisted Disco skills (one opted in, the other ignored)", async () => {
    const root = mkTemp("disco-skills-root-");
    writeSkill(root, "build-basic");
    writeSkill(root, "preview-repair"); // present on disk but NOT allowlisted
    const cwd = mkTemp("disco-cwd-");
    const loader = makeLoader({ cwd, allowlist: ["build-basic"], skillsRoot: root });

    await loader.reload({ resolveProjectTrust: async () => false });

    const names = loader.getSkills().skills.map((s) => s.name);
    expect(names).toEqual(["build-basic"]);
    expect(names).not.toContain("preview-repair");
  });

  it("does NOT load a project-local `.pi/skills` skill (discovery disabled)", async () => {
    const cwd = mkTemp("disco-cwd-local-");
    // Plant the exact resources PR G3 enumerates as forbidden.
    const custom = join(cwd, ".pi", "skills", "custom");
    mkdirSync(custom, { recursive: true });
    writeFileSync(
      join(custom, "SKILL.md"),
      `---\nname: custom\ndescription: project-local skill\n---\n# custom\n`,
    );
    mkdirSync(join(cwd, ".pi", "extensions"), { recursive: true });
    writeFileSync(join(cwd, ".pi", "extensions", "malicious.ts"), "export default {}\n");
    writeFileSync(join(cwd, ".pi", "settings.json"), "{}\n");

    const root = mkTemp("disco-skills-root2-");
    const loader = makeLoader({ cwd, allowlist: [], skillsRoot: root });
    await loader.reload({ resolveProjectTrust: async () => false });

    expect(loader.getSkills().skills).toEqual([]);
  });

  it("REJECTS an arbitrary/user skill force-injected into the loader paths", async () => {
    // Simulate a skill path that slipped into the loader from outside the
    // controlled dir: the gate must throw rather than mount it.
    const userRoot = mkTemp("disco-user-");
    const userSkill = writeSkill(userRoot, "user-tool");
    const cwd = mkTemp("disco-cwd-inject-");
    const agentDir = mkTemp("disco-agent-inject-");
    const settingsManager = SettingsManager.create(cwd, agentDir);
    // Allowlist is EMPTY (allowedDirs = []), but we inject a path anyway.
    const cfg = buildSkillLoaderConfig({ allowlist: [], skillsRoot: mkTemp("disco-empty-root-") });
    const loader = new DefaultResourceLoader({
      cwd,
      agentDir,
      settingsManager,
      noExtensions: true,
      noSkills: true,
      additionalSkillPaths: [userSkill], // arbitrary, non-allowlisted
      skillsOverride: cfg.skillsOverride,
    });

    await expect(loader.reload({ resolveProjectTrust: async () => false })).rejects.toThrow(
      SkillAllowlistViolation,
    );
  });

  it("empty allowlist → zero skills loaded (safe default)", async () => {
    const cwd = mkTemp("disco-cwd-empty-");
    const loader = makeLoader({ cwd, allowlist: [], skillsRoot: mkTemp("disco-empty-") });
    await loader.reload({ resolveProjectTrust: async () => false });
    expect(loader.getSkills().skills).toEqual([]);
  });
});

// ---- end-to-end through the PiKernelRunner -----------------------------

describe("PiKernelRunner skill posture (EPIC G end-to-end)", () => {
  let runner: PiKernelRunner | undefined;

  afterEach(async () => {
    if (runner) await runner.shutdown(0);
    runner = undefined;
  });

  it("default kernel loads ZERO skills (empty allowlist, no error frames)", async () => {
    const frames: KernelOutbound[] = [];
    runner = new PiKernelRunner({ emit: (e) => frames.push(e), heartbeatMs: 50 });
    await runner.handleCommand({ type: "init" });

    expect(frames.some((f) => f.type === "ready")).toBe(true);
    expect(frames.filter((f) => f.type === "error")).toEqual([]);
    expect(runner.getLoadedSkillNames()).toEqual([]);
  });

  it("mounts ONLY an opted-in Disco skill when the allowlist names it", async () => {
    const frames: KernelOutbound[] = [];
    runner = new PiKernelRunner({
      emit: (e) => frames.push(e),
      heartbeatMs: 50,
      skillAllowlist: { allowlist: ["build-basic"] },
    });
    await runner.handleCommand({ type: "init" });

    expect(frames.filter((f) => f.type === "error")).toEqual([]);
    expect(runner.getLoadedSkillNames()).toEqual(["build-basic"]);
  });

  it("refuses to start (fatal) when the allowlist names a non-existent skill", async () => {
    const frames: KernelOutbound[] = [];
    const exits: number[] = [];
    runner = new PiKernelRunner({
      emit: (e) => frames.push(e),
      heartbeatMs: 50,
      onExit: (c) => exits.push(c),
      // A vetted name with no SKILL.md on disk is a misconfiguration that must
      // fail loudly at boot — never silently skip the skill that was required.
      skillAllowlist: { allowlist: ["ghost-skill"] },
    });
    await runner.handleCommand({ type: "init" });

    const fatal = frames.find((f) => f.type === "error" && f.fatal === true);
    expect(fatal, "expected a fatal error frame").toBeDefined();
    expect((fatal as { message: string }).message).toMatch(/failed to start Pi session/);
    expect(exits).toContain(1);
    expect(frames.some((f) => f.type === "ready")).toBe(false);
    runner = undefined; // already shut down
  });
});
