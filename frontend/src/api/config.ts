import { MCP_CONNECTIONS } from "@/fixtures/config";
import type { McpConnection, McpServerApprove, McpServerConfig, Skill, SkillCreate, SkillPatch } from "@/types/config";
import { apiGet, apiSend, fixtureDelay, isLive } from "./client";

/**
 * Data-access for the skills + MCP surfaces. Components reach these only through
 * hooks.
 *
 * Skills are a REAL subsystem: live (VITE_API_BASE set) → the app-server's
 * GET/POST/PUT/DELETE /api/skills, backed by .md files on disk. Offline → a
 * localStorage-backed store so skills persist across reloads in fixture mode too
 * (the user explicitly wanted persistence). MCP remains a session-scoped scaffold.
 */

const SKILLS_LS_KEY = "pmx.skills";

function slugify(name: string): string {
  return name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "skill";
}

function readLocalSkills(): Skill[] {
  try {
    const raw = window.localStorage.getItem(SKILLS_LS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Skill[]) : [];
  } catch {
    return [];
  }
}

function writeLocalSkills(skills: Skill[]): void {
  try {
    window.localStorage.setItem(SKILLS_LS_KEY, JSON.stringify(skills));
  } catch {
    /* swallow — private mode */
  }
}

export async function listSkills(): Promise<Skill[]> {
  if (isLive()) return apiGet<Skill[]>("/api/skills");
  await fixtureDelay();
  return readLocalSkills();
}

export async function createSkill(create: SkillCreate): Promise<Skill> {
  if (isLive()) return apiSend<Skill>("POST", "/api/skills", create);
  await fixtureDelay();
  const skills = readLocalSkills();
  let id = slugify(create.name);
  let i = 2;
  while (skills.some((s) => s.id === id)) id = `${slugify(create.name)}-${i++}`;
  const skill: Skill = {
    id,
    name: create.name,
    description: create.description ?? "",
    body: create.body ?? "",
    enabled: create.enabled ?? true,
    surfaces: create.surfaces ?? [],
  };
  writeLocalSkills([...skills, skill]);
  return skill;
}

export async function updateSkill(id: string, patch: SkillPatch): Promise<Skill> {
  if (isLive())
    return apiSend<Skill>("PUT", `/api/skills/${encodeURIComponent(id)}`, patch);
  await fixtureDelay();
  const skills = readLocalSkills();
  let updated: Skill | undefined;
  const next = skills.map((s) => {
    if (s.id !== id) return s;
    updated = { ...s, ...patch };
    return updated;
  });
  writeLocalSkills(next);
  if (!updated) throw new Error(`unknown skill ${id}`);
  return updated;
}

export async function deleteSkill(id: string): Promise<void> {
  if (isLive()) {
    await apiSend<void>("DELETE", `/api/skills/${encodeURIComponent(id)}`);
    return;
  }
  await fixtureDelay();
  writeLocalSkills(readLocalSkills().filter((s) => s.id !== id));
}

/** Back-compat shim: the old toggle helper, now a thin wrapper over updateSkill. */
export async function setSkillEnabled(id: string, enabled: boolean): Promise<Skill> {
  return updateSkill(id, { enabled });
}

const fixtureMcp: McpConnection[] = MCP_CONNECTIONS.map((c) => ({ ...c }));

export async function listMcpConnections(): Promise<McpConnection[]> {
  if (isLive()) return apiGet<McpConnection[]>("/api/mcp");
  await fixtureDelay();
  return fixtureMcp.map((c) => ({ ...c }));
}

export async function createMcpServer(config: McpServerConfig): Promise<McpConnection> {
  if (isLive()) return apiSend<McpConnection>("POST", "/api/mcp/servers", config);
  await fixtureDelay();
  const conn: McpConnection = {
    id: config.name,
    name: config.name,
    url: config.url,
    status: "disconnected",
    transport: config.transport,
    risk_tier: config.risk_tier,
    enabled: config.enabled,
  };
  fixtureMcp.push(conn);
  return conn;
}

export async function updateMcpServer(
  name: string,
  patch: McpServerConfig,
): Promise<McpConnection> {
  if (isLive())
    return apiSend<McpConnection>(
      "PATCH",
      `/api/mcp/servers/${encodeURIComponent(name)}`,
      patch,
    );
  await fixtureDelay();
  const idx = fixtureMcp.findIndex((c) => c.id === name);
  if (idx === -1) throw new Error(`unknown server ${name}`);
  const updated = { ...fixtureMcp[idx], ...patch };
  fixtureMcp[idx] = updated;
  return updated;
}

export async function deleteMcpServer(name: string): Promise<void> {
  if (isLive()) {
    await apiSend<void>("DELETE", `/api/mcp/servers/${encodeURIComponent(name)}`);
    return;
  }
  await fixtureDelay();
  const idx = fixtureMcp.findIndex((c) => c.id === name);
  if (idx === -1) throw new Error(`unknown server ${name}`);
  fixtureMcp.splice(idx, 1);
}

export async function approveMcpServer(
  name: string,
  body: McpServerApprove,
): Promise<McpConnection> {
  if (isLive())
    return apiSend<McpConnection>(
      "POST",
      `/api/mcp/servers/${encodeURIComponent(name)}/approve`,
      body,
    );
  await fixtureDelay();
  const idx = fixtureMcp.findIndex((c) => c.id === name);
  if (idx === -1) throw new Error(`unknown server ${name}`);
  const updated = {
    ...fixtureMcp[idx],
    description_hash: body.description_hash,
    approved_at: new Date().toISOString(),
    status: "connected" as const,
  };
  fixtureMcp[idx] = updated;
  return updated;
}
