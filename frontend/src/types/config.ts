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
  // rung B optional fields — live pool projection
  transport?: string;  // "stdio" | "streamable_http"
  risk_tier?: string;
  description_hash?: string;  // SHA-256 of approved tool descriptions
  approved_at?: string;  // ISO-8601
  enabled?: boolean;
}

export interface McpServerConfig {
  name: string;
  url: string;
  transport?: string;
  enabled?: boolean;
  allowed_tools?: string[] | null;
  risk_tier?: string;
}

export interface McpServerApprove {
  description_hash: string;
}
