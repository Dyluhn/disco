/**
 * Disco-owned skill allowlist (Disco Pi Build Kernel Campaign, EPIC G — §1.1–§1.3).
 *
 * NON-NEGOTIABLE INVARIANT: Pi may load ONLY Disco-OWNED, reviewed skills. It may
 * NEVER load a user skill, a project-local `.pi/skills` skill, an arbitrary
 * extension, or any other skill source. This module is the single source of truth
 * for WHICH skills the kernel exposes to Pi and the ENFORCEMENT that makes it so.
 *
 * The enforcement is layered (defense in depth), all at LOAD TIME — never by
 * convention:
 *  1. The sidecar builds its `DefaultResourceLoader` with `noSkills: true`, which
 *     disables discovery of project-local (`.pi/skills`), user, and global skill
 *     directories entirely. Only CLI-enabled skills (the kernel passes none) and
 *     `additionalSkillPaths` (which we populate ONLY from this allowlist) are
 *     even considered.
 *  2. {@link buildSkillLoaderConfig} resolves the allowlist to absolute,
 *     in-repo, reviewed skill directories and feeds them as `additionalSkillPaths`.
 *     Resolution ALSO canonicalizes each path (rejecting a symlinked skill dir or
 *     `SKILL.md` that escapes the controlled root) and validates each skill's
 *     governance manifest (campaign §5.2 — id/version/surface/allowed-tools/risk).
 *  3. The same config installs {@link enforceAllowlist} as the loader's
 *     `skillsOverride`, a load-time gate that THROWS if any skill the loader
 *     resolved is not contained within a reviewed allowlisted directory.
 *  4. After reload, the sidecar calls {@link assertOnlyAllowlistedSkills} on the
 *     loader's FINAL skill set as a boot assertion — a backstop that fires loudly
 *     even if the override were ever bypassed or the SDK changed under us.
 *
 * The allowlist starts EMPTY: a kernel with no opted-in skills loads ZERO skills,
 * the safe default. The campaign opts in a reviewed Disco skill by adding its
 * directory name (under {@link DISCO_SKILLS_ROOT}) to {@link DISCO_SKILL_ALLOWLIST}.
 */
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import type { ResourceDiagnostic, Skill } from "@earendil-works/pi-coding-agent";

import { DISCO_TOOL_NAMES } from "./tools.ts";

/**
 * The single source of truth for the fixed Disco tool set (D3 / §7.6), reused
 * here so a reviewed skill's `allowed-tools` can declare ONLY real Disco tools.
 * Importing {@link DISCO_TOOL_NAMES} (not a hand-copied list) keeps governance and
 * the tool bridge from ever diverging.
 */
const DISCO_TOOL_SET: ReadonlySet<string> = new Set<string>(DISCO_TOOL_NAMES);

/**
 * The controlled, in-repo directory that holds the reviewed Disco-owned skills.
 * Resolved relative to this module so it points at `packages/pi-kernel/skills`
 * whether running from `src/` (vitest/tsx) or compiled `dist/` — both sit one
 * level under the package root. Nothing OUTSIDE this directory may ever be
 * mounted as a skill.
 */
export const DISCO_SKILLS_ROOT: string = fileURLToPath(new URL("../skills", import.meta.url));

/**
 * The ALLOWLIST: the explicit, vetted set of skill DIRECTORY NAMES (each a child
 * of {@link DISCO_SKILLS_ROOT} containing a `SKILL.md`) that the kernel exposes to
 * Pi. Identity is the on-disk reviewed location, NOT the frontmatter `name`
 * (which is attacker-influenceable), so adding a name here is an explicit review
 * decision tied to a path in this repo.
 *
 * STARTS EMPTY — zero skills loaded by default. The campaign adds reviewed skill
 * directory names here as they are vetted.
 */
export const DISCO_SKILL_ALLOWLIST: readonly string[] = [];

/** Surfaces a skill may be exposed on (campaign §7.8). */
export type SkillSurface = "build" | "agent";

/** Risk tier per campaign §5.2 — drives review/audit attention. */
export type SkillRisk = "low" | "medium" | "high";

const VALID_SURFACES: ReadonlySet<string> = new Set<SkillSurface>(["build", "agent"]);
const VALID_RISKS: ReadonlySet<string> = new Set<SkillRisk>(["low", "medium", "high"]);

/**
 * Governance metadata parsed + validated from a reviewed skill's `SKILL.md`
 * frontmatter (campaign §5.2). Pi's own loader only reads
 * `name`/`description`/`disable-model-invocation`; these extra governance fields
 * are parsed by {@link readSkillManifest} so a reviewed skill that fails to
 * declare a valid manifest is refused at LOAD TIME rather than mounted.
 */
export interface InternalSkillManifest {
  id: string;
  /**
   * The frontmatter `name` — the identity the Pi SDK actually MOUNTS the skill
   * under. Governance requires it to equal {@link InternalSkillManifest.id} (the
   * controlled directory id) so the live mounted skill can never differ from the
   * reviewed id (campaign §5.2).
   */
  name: string;
  version: string;
  surface: SkillSurface[];
  allowedTools: string[];
  risk: SkillRisk;
}

/** Raised when a non-allowlisted / non-Disco-owned skill would be loaded. */
export class SkillAllowlistViolation extends Error {
  readonly violations: string[];
  constructor(message: string, violations: string[]) {
    super(message);
    this.name = "SkillAllowlistViolation";
    this.violations = violations;
  }
}

export interface SkillAllowlistOptions {
  /** Vetted skill directory names. Defaults to {@link DISCO_SKILL_ALLOWLIST}. */
  allowlist?: readonly string[];
  /** Controlled skills root. Defaults to {@link DISCO_SKILLS_ROOT}. Tests override it. */
  skillsRoot?: string;
}

export interface SkillSet {
  skills: Skill[];
  diagnostics: ResourceDiagnostic[];
}

/**
 * A live name-binding for one allowlisted skill: the reviewed identity (`name` ==
 * the controlled directory id) bound to the canonicalized directory it must load
 * from. The allowlist gate uses this to require that the FINAL loaded `Skill.name`
 * for a skill mounted from `dir` equals `name`, so the live mounted name can never
 * differ from the reviewed id (campaign §5.2, defense in depth on top of the
 * {@link readSkillManifest} name==id check).
 */
export interface AllowlistedSkillBinding {
  /** Reviewed allowlist id == controlled directory name == required live `Skill.name`. */
  name: string;
  /** Canonicalized (real-path) reviewed directory the skill must load from. */
  dir: string;
}

export interface SkillLoaderConfig {
  /** Absolute, reviewed skill directories to hand to `additionalSkillPaths`. */
  additionalSkillPaths: string[];
  /** `skillsOverride` for the loader: the load-time allowlist gate. */
  skillsOverride: (base: SkillSet) => SkillSet;
  /** The resolved (real-path) allowlisted directories, for the boot assertion. */
  allowedDirs: string[];
  /**
   * The CANONICALIZED (real-path) controlled skills root. Passed to the boot
   * assertion so it can independently re-verify that every `allowedDir` is still
   * contained within the real root — a backstop against an escaped realpath ever
   * reaching the allowlist.
   */
  skillsRoot: string;
  /**
   * The parsed governance manifests (campaign §5.2) for every allowlisted skill,
   * in allowlist order. Empty when the allowlist is empty.
   */
  manifests: InternalSkillManifest[];
  /**
   * Live name-bindings (id ↔ real dir) for every allowlisted skill, fed to the
   * gate so the FINAL mounted `Skill.name` is checked against the reviewed id for
   * the directory it loaded from. Empty when the allowlist is empty.
   */
  bindings: AllowlistedSkillBinding[];
}

/** One fully-resolved, vetted allowlist entry: its real dir + governance manifest. */
interface ResolvedSkill {
  name: string;
  dir: string;
  manifest: InternalSkillManifest;
}

/** A skill name must be a single, non-traversing path segment. */
function isSafeSkillName(name: string): boolean {
  return (
    name.length > 0 &&
    name !== "." &&
    name !== ".." &&
    !name.includes("/") &&
    !name.includes("\\") &&
    !name.includes("\0")
  );
}

/** Resolve a path's real (symlink-collapsed) form, or its absolute form if absent. */
function realPathOrSelf(p: string): string {
  try {
    return realpathSync(p);
  } catch {
    return resolve(p);
  }
}

/**
 * Pure path-prefix containment on ALREADY-canonicalized paths: true iff `child`
 * is `parent` itself or strictly nested under it. The `sep` boundary is what
 * stops a sibling like `/root-evil` from matching the root `/root`.
 */
function containsCanonical(parent: string, child: string): boolean {
  if (child === parent) return true;
  return child.startsWith(parent.endsWith(sep) ? parent : parent + sep);
}

/** True iff `child` is `parent` itself or strictly nested under it (real paths). */
function isWithin(child: string, parent: string): boolean {
  return containsCanonical(realPathOrSelf(parent), realPathOrSelf(child));
}

/**
 * Resolve + fully vet the allowlist into concrete skill entries under the
 * controlled root. Each entry must be a safe single segment naming a directory
 * that exists, that contains a `SKILL.md`, that canonicalizes to a path still
 * within the real controlled root (symlink confinement), and that declares a
 * valid governance manifest. A missing/invalid vetted skill is a MISCONFIGURATION
 * and throws — boot must fail loudly, never silently skip a skill that was
 * supposed to be there. An empty allowlist resolves to an empty list (safe
 * default).
 */
function resolveAllowlistedEntries(opts: SkillAllowlistOptions = {}): ResolvedSkill[] {
  const allowlist = opts.allowlist ?? DISCO_SKILL_ALLOWLIST;
  const root = opts.skillsRoot ?? DISCO_SKILLS_ROOT;
  // Canonicalize the controlled root ONCE; every allowlisted dir is checked for
  // containment against THIS real path (not the un-resolved `root`).
  const realRoot = realPathOrSelf(root);
  const entries: ResolvedSkill[] = [];
  for (const name of allowlist) {
    if (!isSafeSkillName(name)) {
      throw new Error(
        `[disco-skills] invalid allowlisted skill name '${name}': must be a single, ` +
          `non-traversing directory segment under ${root}`,
      );
    }
    const dir = join(root, name);
    const skillMd = join(dir, "SKILL.md");
    if (!existsSync(skillMd)) {
      throw new Error(
        `[disco-skills] allowlisted skill '${name}' is missing its SKILL.md at ${skillMd}; ` +
          `refusing to start (a vetted skill must exist in the controlled skills dir)`,
      );
    }
    // SYMLINK CONFINEMENT (same class as the sandbox symlink-escape bug): the
    // single-segment name check above does NOT stop the skill DIR itself from
    // being a symlink that points OUTSIDE the controlled root. Canonicalize the
    // dir and REQUIRE it stays equal-to-or-under the real root. Comparing the
    // already-escaped realpath against itself would otherwise satisfy both the
    // `skillsOverride` gate AND the boot assertion, mounting `/tmp/evil-skill`.
    const realDir = realPathOrSelf(dir);
    if (!containsCanonical(realRoot, realDir)) {
      throw new SkillAllowlistViolation(
        `[disco-skills] allowlisted skill '${name}' resolves to ${realDir}, which escapes the ` +
          `controlled skills root ${realRoot}; refusing to mount a skill directory that is (or is ` +
          `reached via) a symlink leaving the root.`,
        [`${name} <- ${realDir}`],
      );
    }
    // The SKILL.md manifest must ALSO resolve under the real skill dir (and thus
    // under the real root): a symlinked SKILL.md pointing outside the root is the
    // same escape one level down, and is refused too.
    const realSkillMd = realPathOrSelf(skillMd);
    if (!containsCanonical(realDir, realSkillMd)) {
      throw new SkillAllowlistViolation(
        `[disco-skills] allowlisted skill '${name}' has a SKILL.md that resolves to ${realSkillMd}, ` +
          `outside its controlled directory ${realDir}; refusing (symlinked manifest escaping the root).`,
        [`${name} <- ${realSkillMd}`],
      );
    }
    // GOVERNANCE (campaign §5.2): a reviewed skill must declare a valid manifest
    // (id matching the dir name, version, surface(s), allowed-tools, risk) or it
    // is refused — parsed only AFTER the path is proven in-root.
    const manifest = readSkillManifest(name, realSkillMd);
    entries.push({ name, dir: realDir, manifest });
  }
  return entries;
}

/**
 * Resolve the allowlist to absolute skill directories under the controlled root.
 * See {@link resolveAllowlistedEntries} for the full set of load-time checks
 * (existence, symlink confinement, governance-manifest validation).
 */
export function resolveAllowlistedSkillPaths(opts: SkillAllowlistOptions = {}): string[] {
  return resolveAllowlistedEntries(opts).map((e) => e.dir);
}

/**
 * Load + validate the governance manifests (campaign §5.2) for every allowlisted
 * skill, in allowlist order. Applies the SAME containment + symlink checks as
 * {@link resolveAllowlistedSkillPaths}, so a manifest is only ever returned for a
 * skill proven to live within the controlled root. Empty allowlist → `[]`.
 */
export function loadAllowlistedSkillManifests(
  opts: SkillAllowlistOptions = {},
): InternalSkillManifest[] {
  return resolveAllowlistedEntries(opts).map((e) => e.manifest);
}

/** True iff `skill` was loaded from within one of the reviewed allowlisted dirs. */
export function isAllowlistedSkill(skill: Skill, allowedDirs: readonly string[]): boolean {
  return allowedDirs.some((dir) => isWithin(skill.filePath, dir));
}

/** The binding whose reviewed dir contains `skill`, or undefined if none does. */
function bindingFor(
  skill: Skill,
  bindings: readonly AllowlistedSkillBinding[],
): AllowlistedSkillBinding | undefined {
  return bindings.find((b) => isWithin(skill.filePath, b.dir));
}

/**
 * Assert every loaded skill's LIVE name equals the reviewed id for the directory
 * it loaded from. Returns the offending skills (none → []). A skill not matched to
 * any binding is left to the path-containment gate ({@link enforceAllowlist}); this
 * check only governs skills that DID load from a reviewed dir but under a name that
 * differs from that dir's reviewed id.
 */
function nameBindingViolations(
  skills: readonly Skill[],
  bindings: readonly AllowlistedSkillBinding[],
): string[] {
  if (bindings.length === 0) return [];
  const out: string[] = [];
  for (const s of skills) {
    const binding = bindingFor(s, bindings);
    if (binding !== undefined && s.name !== binding.name) {
      out.push(`${s.name} <- ${s.filePath} (expected name '${binding.name}')`);
    }
  }
  return out;
}

/**
 * The LOAD-TIME allowlist gate, installed as the loader's `skillsOverride`.
 *
 * Throws {@link SkillAllowlistViolation} if the loader resolved ANY skill that is
 * not contained within a reviewed allowlisted directory — i.e. a project-local
 * `.pi/skills` skill, a user/global skill, or an arbitrary extension-provided
 * skill. Because the loader calls this synchronously inside `reload()`, the throw
 * aborts the whole resource load: the kernel refuses to start rather than mount a
 * skill it did not vet.
 */
export function enforceAllowlist(
  base: SkillSet,
  allowedDirs: readonly string[],
  bindings: readonly AllowlistedSkillBinding[] = [],
): SkillSet {
  const violations = base.skills.filter((s) => !isAllowlistedSkill(s, allowedDirs));
  if (violations.length > 0) {
    const detail = violations.map((s) => `${s.name} <- ${s.filePath}`);
    throw new SkillAllowlistViolation(
      `[disco-skills] refusing to load ${violations.length} non-allowlisted skill(s): ` +
        `${detail.join("; ")}. Only reviewed Disco-owned skills under the controlled ` +
        `skills dir may be mounted (allowlist enforced at load time).`,
      detail,
    );
  }
  // NAME BINDING: a skill that DID load from a reviewed dir must mount under that
  // dir's reviewed id. A loaded `Skill.name` that differs from the reviewed id is
  // refused, so the live mounted name can never drift from the reviewed identity.
  const nameViolations = nameBindingViolations(base.skills, bindings);
  if (nameViolations.length > 0) {
    throw new SkillAllowlistViolation(
      `[disco-skills] refusing to load ${nameViolations.length} skill(s) whose live mounted name ` +
        `differs from the reviewed allowlist id: ${nameViolations.join("; ")}. The mounted skill ` +
        `name must equal the controlled directory id it was reviewed under.`,
      nameViolations,
    );
  }
  return base;
}

/**
 * BOOT ASSERTION (backstop): assert the loader's FINAL skill set contains only
 * allowlisted, Disco-owned skills. Called by the sidecar AFTER `reload()` on
 * `resourceLoader.getSkills().skills`. Defense in depth — fires loudly even if
 * the `skillsOverride` gate were ever bypassed or the SDK changed under us.
 */
export function assertOnlyAllowlistedSkills(
  loadedSkills: readonly Skill[],
  allowedDirs: readonly string[],
  skillsRoot?: string,
  bindings: readonly AllowlistedSkillBinding[] = [],
): void {
  // Defense in depth: independently re-verify (on canonicalized paths) that every
  // allowedDir is still contained in the real root, so an ESCAPED realpath that
  // somehow reached `allowedDirs` cannot satisfy this assertion either — the same
  // canonicalized-containment check the resolver applies, re-run at boot.
  if (skillsRoot !== undefined) {
    const realRoot = realPathOrSelf(skillsRoot);
    const escaped = allowedDirs.filter((dir) => !containsCanonical(realRoot, realPathOrSelf(dir)));
    if (escaped.length > 0) {
      throw new SkillAllowlistViolation(
        `[disco-skills] boot assertion failed: ${escaped.length} allowlisted dir(s) escape the ` +
          `controlled skills root ${realRoot}: ${escaped.join("; ")}. A skill directory that ` +
          `resolves outside the root (e.g. via symlink) must never be mounted.`,
        escaped.slice(),
      );
    }
  }
  const violations = loadedSkills.filter((s) => !isAllowlistedSkill(s, allowedDirs));
  if (violations.length > 0) {
    const detail = violations.map((s) => `${s.name} <- ${s.filePath}`);
    throw new SkillAllowlistViolation(
      `[disco-skills] boot assertion failed: ${violations.length} non-allowlisted skill(s) ` +
        `reached the live session: ${detail.join("; ")}. The kernel must expose ONLY ` +
        `reviewed Disco-owned skills.`,
      detail,
    );
  }
  // Backstop the live name-binding too: a skill that loaded from a reviewed dir but
  // under a name differing from that dir's reviewed id must never reach the session.
  const nameViolations = nameBindingViolations(loadedSkills, bindings);
  if (nameViolations.length > 0) {
    throw new SkillAllowlistViolation(
      `[disco-skills] boot assertion failed: ${nameViolations.length} skill(s) reached the live ` +
        `session under a name differing from their reviewed allowlist id: ${nameViolations.join("; ")}. ` +
        `The mounted skill name must equal the controlled directory id.`,
      nameViolations,
    );
  }
}

/**
 * Build the loader wiring that restricts Pi's skill loading to the Disco-owned
 * allowlist. Feed `additionalSkillPaths` and `skillsOverride` into the
 * `DefaultResourceLoader`, and pass `allowedDirs` to {@link assertOnlyAllowlistedSkills}
 * after reload. With an empty allowlist this yields no paths and a gate that
 * rejects anything the loader still managed to resolve.
 */
export function buildSkillLoaderConfig(opts: SkillAllowlistOptions = {}): SkillLoaderConfig {
  const entries = resolveAllowlistedEntries(opts);
  const allowedDirs = entries.map((e) => e.dir);
  const bindings: AllowlistedSkillBinding[] = entries.map((e) => ({ name: e.name, dir: e.dir }));
  const skillsRoot = realPathOrSelf(opts.skillsRoot ?? DISCO_SKILLS_ROOT);
  return {
    additionalSkillPaths: allowedDirs,
    skillsOverride: (base: SkillSet) => enforceAllowlist(base, allowedDirs, bindings),
    allowedDirs,
    skillsRoot,
    manifests: entries.map((e) => e.manifest),
    bindings,
  };
}

// ---- governance frontmatter manifest parsing (campaign §5.2) ----------------

/**
 * Parse + validate the governance frontmatter of one reviewed `SKILL.md`.
 *
 * We parse only the small, fixed set of scalar/inline-list keys the registry
 * governs (id/version/surface/allowed-tools/risk). Pi's own loader handles
 * `name`/`description` via a full YAML parser; this is a deliberately tiny
 * dependency-free reader for the extra keys, authored to match the inline-array
 * style used in the reviewed files. `id` must equal the allowlisted directory id.
 */
export function readSkillManifest(id: string, filePath: string): InternalSkillManifest {
  const fm = parseFrontmatter(filePath);

  const fmId = scalar(fm.id);
  if (fmId !== id) {
    throw new Error(
      `skill '${id}' (${filePath}) declares id '${fmId ?? "<missing>"}' — ` +
        `frontmatter id must match the directory id`,
    );
  }

  // NAME BINDING (campaign §5.2): the Pi SDK mounts and invokes a skill by its
  // frontmatter `name`, NOT its `id`. If `name` could differ from the reviewed id,
  // an allowlisted `build-basic/SKILL.md` could mount under an arbitrary name and
  // the id check would be merely cosmetic. Require `name` == the controlled
  // directory id so the LIVE mounted name equals the reviewed id.
  const fmName = scalar(fm.name);
  if (fmName !== id) {
    throw new Error(
      `skill '${id}' (${filePath}) declares name '${fmName ?? "<missing>"}' — ` +
        `frontmatter name must match the directory id (the live mounted skill name ` +
        `must equal the reviewed allowlist id)`,
    );
  }

  const version = scalar(fm.version);
  if (!version) {
    throw new Error(`skill '${id}' (${filePath}) is missing a 'version' field`);
  }

  const surface = list(fm.surface);
  if (surface.length === 0) {
    throw new Error(`skill '${id}' (${filePath}) must declare at least one 'surface'`);
  }
  for (const s of surface) {
    if (!VALID_SURFACES.has(s)) {
      throw new Error(
        `skill '${id}' (${filePath}) has invalid surface '${s}' (allowed: build, agent)`,
      );
    }
  }

  const allowedTools = list(fm["allowed-tools"]);
  if (allowedTools.length === 0) {
    throw new Error(`skill '${id}' (${filePath}) must declare 'allowed-tools'`);
  }
  // FAIL-CLOSED on unknown tools: `allowed-tools` must name ONLY tools from the
  // fixed Disco tool set (the single source of truth in tools.ts). A skill naming
  // a tool outside that set — e.g. an attacker-chosen `totally_not_a_disco_tool` —
  // is refused at LOAD TIME rather than silently passing governance.
  for (const tool of allowedTools) {
    if (!DISCO_TOOL_SET.has(tool)) {
      throw new Error(
        `skill '${id}' (${filePath}) declares allowed-tool '${tool}', which is not a Disco tool ` +
          `(allowed: ${DISCO_TOOL_NAMES.join(", ")})`,
      );
    }
  }

  const risk = scalar(fm.risk);
  if (!risk || !VALID_RISKS.has(risk)) {
    throw new Error(
      `skill '${id}' (${filePath}) has invalid risk '${risk ?? "<missing>"}' ` +
        `(allowed: low, medium, high)`,
    );
  }

  return {
    id,
    name: fmName,
    version,
    surface: surface as SkillSurface[],
    allowedTools,
    risk: risk as SkillRisk,
  };
}

/**
 * Extract the raw frontmatter key/values from a `SKILL.md`.
 *
 * Handles the subset authored in the reviewed files: top-level `key: scalar`
 * and `key: [a, b, c]` inline lists. Indented (block) lines — e.g. a folded
 * `description` body — are ignored, as are blanks and `#` comments. Values are
 * returned as `string` (scalar) or `string[]` (inline list).
 */
function parseFrontmatter(filePath: string): Record<string, string | string[]> {
  const raw = readFileSync(filePath, "utf8").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!raw.startsWith("---")) {
    throw new Error(`skill file ${filePath} has no frontmatter block`);
  }
  const end = raw.indexOf("\n---", 3);
  if (end === -1) {
    throw new Error(`skill file ${filePath} has an unterminated frontmatter block`);
  }
  const block = raw.slice(4, end);

  const out: Record<string, string | string[]> = {};
  for (const line of block.split("\n")) {
    if (line.trim() === "" || line.trimStart().startsWith("#")) continue;
    // Top-level keys only — indented lines belong to a block scalar/list value
    // we do not govern here.
    if (/^\s/.test(line)) continue;
    const idx = line.indexOf(":");
    if (idx === -1) continue;
    const key = line.slice(0, idx).trim();
    if (key === "") continue;
    const rawValue = line.slice(idx + 1).trim();
    out[key] =
      rawValue.startsWith("[") && rawValue.endsWith("]")
        ? rawValue
            .slice(1, -1)
            .split(",")
            .map((part) => unquote(part.trim()))
            .filter((part) => part.length > 0)
        : unquote(rawValue);
  }
  return out;
}

function scalar(value: string | string[] | undefined): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

function list(value: string | string[] | undefined): string[] {
  if (Array.isArray(value)) return value;
  if (typeof value === "string" && value.trim().length > 0) return [value.trim()];
  return [];
}

function unquote(value: string): string {
  if (value.length >= 2) {
    const first = value[0];
    const last = value[value.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return value.slice(1, -1);
    }
  }
  return value;
}
