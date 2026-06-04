import { MCP_CONNECTIONS, SKILLS } from "@/fixtures/config";
import type { McpConnection, Skill } from "@/types/config";
import { apiGet, apiSend, fixtureDelay, isLive } from "./client";

/**
 * Data-access for the skills + MCP surfaces (wiring-pending scaffolds). Components
 * reach these only through hooks. Live (VITE_API_BASE set) → the app-server
 * GET /api/skills, PUT /api/skills/{id}, GET /api/mcp; otherwise → the in-repo
 * fixture (session-scoped, no browser storage). DTO shapes match the types.
 */

let fixtureSkills: Skill[] = SKILLS.map((s) => ({ ...s }));
const fixtureMcp: McpConnection[] = MCP_CONNECTIONS.map((c) => ({ ...c }));

export async function listSkills(): Promise<Skill[]> {
  if (isLive()) return apiGet<Skill[]>("/api/skills");
  await fixtureDelay();
  return fixtureSkills.map((s) => ({ ...s }));
}

export async function setSkillEnabled(id: string, enabled: boolean): Promise<Skill[]> {
  if (isLive()) return apiSend<Skill[]>("PUT", `/api/skills/${encodeURIComponent(id)}`, { enabled });
  await fixtureDelay();
  fixtureSkills = fixtureSkills.map((s) => (s.id === id ? { ...s, enabled } : s));
  return fixtureSkills.map((s) => ({ ...s }));
}

export async function listMcpConnections(): Promise<McpConnection[]> {
  if (isLive()) return apiGet<McpConnection[]>("/api/mcp");
  await fixtureDelay();
  return fixtureMcp.map((c) => ({ ...c }));
}
