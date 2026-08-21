import { MCP_CONNECTIONS } from "@/fixtures/config";
import type { McpConnection, McpImportResult, McpServerApprove, McpServerConfig, Skill, SkillCreate, SkillPatch } from "@/types/config";
import type { ProbeResult } from "@/types/probe";
import { agentGet, agentLive, agentSend, apiGet, apiSend, fixtureDelay, isLive } from "./client";

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
  if (isLive()) {
    const configured = await apiGet<McpConnection[]>("/api/mcp");
    if (!agentLive()) return configured;
    const runtime = await agentGet<{
      enabled: boolean;
      servers: Record<string, { status: McpConnection["status"] }>;
    }>("/api/mcp/servers");
    return configured.map((connection) => {
      const live = runtime.servers[connection.id];
      if (connection.enabled === false || !runtime.enabled) {
        return { ...connection, status: "disabled" };
      }
      return live ? { ...connection, status: live.status } : connection;
    });
  }
  await fixtureDelay();
  return fixtureMcp.map((c) => ({ ...c }));
}

/** Paste-a-config import: send the raw pasted `mcpServers` blob to the server,
 * which owns the ONLY parser (the client never pre-parses). `dryRun` previews;
 * `dryRun=false` stores pasted secret values as SecretStore refs and creates
 * the servers through the same path as the manual form. */
export async function importMcpConfig(text: string, dryRun: boolean): Promise<McpImportResult> {
  if (!isLive()) {
    throw new Error("Connect the app server to import a pasted MCP config.");
  }
  const result = await apiSend<McpImportResult>("POST", "/api/mcp/servers/import", {
    text,
    dry_run: dryRun,
  });
  if (!dryRun && agentLive()) await agentSend("POST", "/api/mcp/reload");
  return result;
}

export async function createMcpServer(config: McpServerConfig): Promise<McpConnection> {
  if (isLive()) {
    const connection = await apiSend<McpConnection>("POST", "/api/mcp/servers", config);
    if (agentLive()) await agentSend("POST", "/api/mcp/reload");
    return connection;
  }
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
  if (isLive()) {
    const connection = await apiSend<McpConnection>(
      "PATCH",
      `/api/mcp/servers/${encodeURIComponent(name)}`,
      patch,
    );
    if (agentLive()) await agentSend("POST", "/api/mcp/reload");
    return connection;
  }
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
    if (agentLive()) await agentSend("POST", "/api/mcp/reload");
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
  if (isLive()) {
    const connection = await apiSend<McpConnection>(
      "POST",
      `/api/mcp/servers/${encodeURIComponent(name)}/approve`,
      body,
    );
    if (agentLive()) await agentSend("POST", "/api/mcp/reload");
    return connection;
  }
  await fixtureDelay();
  const idx = fixtureMcp.findIndex((c) => c.id === name);
  if (idx === -1) throw new Error(`unknown server ${name}`);
  const updated: McpConnection =
    body.approval_kind === "config"
      ? {
          ...fixtureMcp[idx],
          config_hash: body.config_hash,
          new_config_hash: undefined,
          status: "approval_required",
        }
      : {
          ...fixtureMcp[idx],
          description_hash: body.description_hash,
          new_description_hash: undefined,
          approved_at: new Date().toISOString(),
          status: "connected",
        };
  fixtureMcp[idx] = updated;
  return updated;
}

export async function revokeMcpServer(name: string): Promise<McpConnection> {
  if (isLive()) {
    const connection = await apiSend<McpConnection>(
      "POST",
      `/api/mcp/servers/${encodeURIComponent(name)}/revoke`,
    );
    if (agentLive()) await agentSend("POST", "/api/mcp/reload");
    return connection;
  }
  await fixtureDelay();
  const idx = fixtureMcp.findIndex((c) => c.id === name);
  if (idx === -1) throw new Error(`unknown server ${name}`);
  const updated: McpConnection = {
    ...fixtureMcp[idx],
    status: "approval_required",
    config_hash: undefined,
    description_hash: undefined,
    approved_at: undefined,
  };
  fixtureMcp[idx] = updated;
  return updated;
}

/** T4.5 — handshake a configured MCP server (initialize + tools/list) and report
 * the tool count or the real connection error. Runs against the agent-server,
 * which owns the MCP transport clients. */
export async function testMcpConnection(name: string): Promise<ProbeResult> {
  return agentSend<ProbeResult>("POST", "/api/mcp/test", { name });
}
