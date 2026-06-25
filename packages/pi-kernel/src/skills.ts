/**
 * Internal Pi skill registry (Disco Pi Build Kernel Campaign, EPIC G / PR G1).
 *
 * Pi can discover skills from disk (project-local `.pi/skills`, user skills,
 * package skills). Campaign §1.1 / §5.2 forbid ALL of that: only Disco-owned,
 * versioned, reviewed skills may ever mount. This module is the enforcement
 * point on the skill axis.
 *
 *   - {@link loadInternalSkills} reads the reviewed, repo-owned
 *     `packages/pi-kernel/skills/` directory (one `SKILL.md` per skill) through
 *     the Pi SDK's own loader, then validates each against a hardcoded
 *     ALLOWLIST of skill ids — so an extra directory dropped into the reviewed
 *     path is NOT mounted unless its id is in the allowlist.
 *   - {@link internalSkillsOverride} builds the `skillsOverride` the runner
 *     passes to `DefaultResourceLoader`. It IGNORES `base` entirely (every
 *     disk/project/user/package-discovered skill) and returns ONLY the internal
 *     skills. There is no path by which a discovered skill survives.
 *
 * Skill metadata (id/version/surface/allowed-tools/risk — campaign §5.2) lives
 * in each `SKILL.md` frontmatter and is parsed here. Pi's own loader only reads
 * `name`/`description`/`disable-model-invocation`; the extra governance fields
 * are parsed by {@link readSkillManifest}.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  loadSkillsFromDir,
  type ResourceDiagnostic,
  type Skill,
} from "@earendil-works/pi-coding-agent";

/** Surfaces a skill may be exposed on (campaign §7.8). */
export type SkillSurface = "build" | "agent";

/** Risk tier per campaign §5.2 — drives review/audit attention. */
export type SkillRisk = "low" | "medium" | "high";

/**
 * The reviewed allowlist of internal skill ids, in mount order. Membership here
 * — NOT mere presence on disk — is what authorizes a skill to mount. Adding a
 * skill is a deliberate, reviewed code change to this list.
 */
export const INTERNAL_SKILL_IDS = ["build-basic", "preview-repair"] as const;

export type InternalSkillId = (typeof INTERNAL_SKILL_IDS)[number];

/** The `source` tag stamped on internal skills' Pi `sourceInfo`. */
export const INTERNAL_SKILL_SOURCE = "disco-internal";

/** Absolute path to the reviewed, repo-owned skills directory. */
export const INTERNAL_SKILLS_DIR: string = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "skills",
);

const VALID_SURFACES: ReadonlySet<string> = new Set<SkillSurface>(["build", "agent"]);
const VALID_RISKS: ReadonlySet<string> = new Set<SkillRisk>(["low", "medium", "high"]);

/** Governance metadata parsed from a skill's `SKILL.md` frontmatter. */
export interface InternalSkillManifest {
  id: string;
  version: string;
  surface: SkillSurface[];
  allowedTools: string[];
  risk: SkillRisk;
}

/**
 * A reviewed internal skill: its governance manifest plus the Pi {@link Skill}
 * object that is actually mounted into the session.
 */
export interface InternalSkill extends InternalSkillManifest {
  /** Skill name as Pi sees it (mirrors {@link InternalSkillManifest.id}). */
  name: string;
  /** The Pi skill object mounted via the resource loader. */
  skill: Skill;
}

/** The `{ skills, diagnostics }` shape Pi's `skillsOverride` consumes/returns. */
export interface SkillsResult {
  skills: Skill[];
  diagnostics: ResourceDiagnostic[];
}

/**
 * Load the reviewed internal skills from {@link INTERNAL_SKILLS_DIR}.
 *
 * Discovery is delegated to the Pi SDK loader (so the mounted `Skill` objects
 * are byte-identical to what Pi would build), then constrained to the
 * {@link INTERNAL_SKILL_IDS} allowlist in declared order. A missing allowlisted
 * skill, or a frontmatter manifest that fails validation, throws — the registry
 * must be self-consistent or the kernel must not start with skills.
 */
export function loadInternalSkills(): InternalSkill[] {
  const { skills } = loadSkillsFromDir({
    dir: INTERNAL_SKILLS_DIR,
    source: INTERNAL_SKILL_SOURCE,
  });

  const byName = new Map<string, Skill>();
  for (const skill of skills) {
    byName.set(skill.name, skill);
  }

  const result: InternalSkill[] = [];
  for (const id of INTERNAL_SKILL_IDS) {
    const skill = byName.get(id);
    if (!skill) {
      throw new Error(
        `internal skill '${id}' not found under ${INTERNAL_SKILLS_DIR} ` +
          `(expected ${id}/SKILL.md)`,
      );
    }
    const manifest = readSkillManifest(id, skill.filePath);
    result.push({ ...manifest, name: skill.name, skill });
  }
  return result;
}

/**
 * Build the `skillsOverride` for `DefaultResourceLoader`.
 *
 * The returned function DROPS `base` wholesale — every skill Pi discovered from
 * disk (project `.pi/skills`, user skills, package skills) and every diagnostic
 * about them — and returns ONLY the internal skills. This is the §1.1 invariant:
 * no user/project skill can ever reach the agent, regardless of what is on disk.
 */
export function internalSkillsOverride(): (base: SkillsResult) => SkillsResult {
  return (_base: SkillsResult): SkillsResult => {
    const internal = loadInternalSkills();
    return {
      skills: internal.map((entry) => entry.skill),
      diagnostics: [],
    };
  };
}

// ---- frontmatter manifest parsing -------------------------------------------

/**
 * Parse + validate the governance frontmatter of one reviewed `SKILL.md`.
 *
 * We parse only the small, fixed set of scalar/inline-list keys the registry
 * governs (id/version/surface/allowed-tools/risk). Pi's own loader handles
 * `name`/`description` via a full YAML parser; this is a deliberately tiny
 * dependency-free reader for the extra keys, authored to match the inline-array
 * style used in the reviewed files. `id` must equal the directory id.
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

  const risk = scalar(fm.risk);
  if (!risk || !VALID_RISKS.has(risk)) {
    throw new Error(
      `skill '${id}' (${filePath}) has invalid risk '${risk ?? "<missing>"}' ` +
        `(allowed: low, medium, high)`,
    );
  }

  return {
    id,
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
