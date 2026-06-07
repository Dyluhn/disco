/** Skills + MCP connection types (Settings). Skills are now a real, persistent
 *  subsystem (.md instruction files, Claude-Code style); MCP remains a scaffold. */

export interface Skill {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  /** The markdown instructions handed to the agent (SKILL.md body). Optional on
   *  read for back-compat; always present from the live backend. */
  body?: string;
}

export interface SkillCreate {
  name: string;
  description?: string;
  body?: string;
  enabled?: boolean;
}

export interface SkillPatch {
  name?: string;
  description?: string;
  body?: string;
  enabled?: boolean;
}

export type McpStatus = "connected" | "disconnected" | "error";

export interface McpConnection {
  id: string;
  name: string;
  url: string;
  status: McpStatus;
}
