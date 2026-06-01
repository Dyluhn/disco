import { MCP_CONNECTIONS, SKILLS } from "@/fixtures/config";
import type { McpConnection, Skill } from "@/types/config";

/**
 * Data-access for the (wiring-pending) skills + MCP surfaces. Components reach
 * these only through hooks. Session-scoped in-memory state (no browser storage);
 * swap for the live skills registry + MCP client when those subsystems land.
 */

const delay = () => new Promise((r) => setTimeout(r, 20));

let skills: Skill[] = SKILLS.map((s) => ({ ...s }));
const mcp: McpConnection[] = MCP_CONNECTIONS.map((c) => ({ ...c }));

export async function listSkills(): Promise<Skill[]> {
  await delay();
  return skills.map((s) => ({ ...s }));
}

export async function setSkillEnabled(id: string, enabled: boolean): Promise<Skill[]> {
  await delay();
  skills = skills.map((s) => (s.id === id ? { ...s, enabled } : s));
  return skills.map((s) => ({ ...s }));
}

export async function listMcpConnections(): Promise<McpConnection[]> {
  await delay();
  return mcp.map((c) => ({ ...c }));
}
