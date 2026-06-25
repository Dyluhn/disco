/**
 * Internal Pi skill registry (EPIC G / PR G1).
 *
 * Two invariants:
 *  1. `loadInternalSkills()` returns exactly the reviewed, repo-owned skills —
 *     the right ids/names, with their governance manifest parsed from frontmatter.
 *  2. `internalSkillsOverride()` REPLACES whatever Pi discovered from disk with
 *     ONLY the internal skills: a planted project-discovered skill is dropped,
 *     so no user/project skill can ever reach the agent (campaign §1.1 / §5.2).
 */
import { describe, expect, it } from "vitest";

import {
  INTERNAL_SKILL_IDS,
  INTERNAL_SKILL_SOURCE,
  internalSkillsOverride,
  loadInternalSkills,
  type SkillsResult,
} from "../src/skills.ts";
import type { ResourceDiagnostic, Skill } from "@earendil-works/pi-coding-agent";

/** A fake project-discovered skill, exactly what `skillsOverride` must drop. */
function projectSkill(name: string): Skill {
  return {
    name,
    description: `attacker-controlled ${name} skill from the generated workspace`,
    filePath: `/work/generated/.pi/skills/${name}/SKILL.md`,
    baseDir: `/work/generated/.pi/skills/${name}`,
    sourceInfo: {
      path: `/work/generated/.pi/skills/${name}/SKILL.md`,
      source: "local",
      scope: "project",
      origin: "top-level",
    },
    disableModelInvocation: false,
  };
}

describe("loadInternalSkills (reviewed, repo-owned registry)", () => {
  it("returns exactly the two internal skills with correct ids/names", () => {
    const skills = loadInternalSkills();

    expect(skills.map((s) => s.id)).toEqual([...INTERNAL_SKILL_IDS]);
    expect(skills.map((s) => s.name)).toEqual([...INTERNAL_SKILL_IDS]);
    expect(skills.map((s) => s.id)).toEqual(["build-basic", "preview-repair"]);
  });

  it("parses the governance manifest (version/surface/allowed-tools/risk) from frontmatter", () => {
    const byId = new Map(loadInternalSkills().map((s) => [s.id, s]));

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

  it("mounts each as a real Pi Skill stamped with the internal source", () => {
    for (const entry of loadInternalSkills()) {
      expect(entry.skill.name).toBe(entry.id);
      expect(entry.skill.description.length).toBeGreaterThan(0);
      expect(entry.skill.filePath.endsWith(`${entry.id}/SKILL.md`)).toBe(true);
      expect(entry.skill.sourceInfo.source).toBe(INTERNAL_SKILL_SOURCE);
    }
  });
});

describe("internalSkillsOverride (drops everything discovered from disk)", () => {
  it("replaces a base containing a project-discovered skill with ONLY internal skills", () => {
    const override = internalSkillsOverride();

    const base: SkillsResult = {
      skills: [projectSkill("malicious"), projectSkill("backdoor")],
      diagnostics: [
        { type: "warning", message: "discovered project skill", path: "/work/generated" },
      ] as ResourceDiagnostic[],
    };

    const result = override(base);

    // The project skills are gone; only the internal allowlist remains.
    const names = result.skills.map((s) => s.name);
    expect(names).toEqual([...INTERNAL_SKILL_IDS]);
    expect(names).not.toContain("malicious");
    expect(names).not.toContain("backdoor");

    // Diagnostics about the dropped project skills do not survive either.
    expect(result.diagnostics).toEqual([]);
  });

  it("ignores the base entirely — an empty base still yields the internal skills", () => {
    const override = internalSkillsOverride();
    const result = override({ skills: [], diagnostics: [] });
    expect(result.skills.map((s) => s.name)).toEqual([...INTERNAL_SKILL_IDS]);
  });
});
