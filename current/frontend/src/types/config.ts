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
  /** Which surfaces this skill applies to (["build"], ["agent"], or both). Empty/
   *  absent = applies everywhere (back-compat). */
  surfaces?: string[];
}

export interface SkillCreate {
  name: string;
  description?: string;
  body?: string;
  enabled?: boolean;
  surfaces?: string[];
}

export interface SkillPatch {
  name?: string;
  description?: string;
  body?: string;
  enabled?: boolean;
  surfaces?: string[];
}

export type McpStatus =
  | "disabled"
  | "connecting"
  | "connected"
  | "degraded"
  | "disconnected"
  | "error"
  | "approval_required";

export interface McpConnection {
  id: string;
  name: string;
  url: string;
  status: McpStatus;
  // rung B optional fields — live pool projection
  transport?: string;  // "stdio" | "streamable_http"
  risk_tier?: string;
  description_hash?: string;  // SHA-256 of the LAST APPROVED tool descriptions (the old hash)
  // E6 (#10): the AUTHORITATIVE new_hash the live agent-server pool just
  // computed when it detected drift against `description_hash`. Surfaces
  // on the ApprovalDiff so the user sees the REAL fingerprint of the
  // changed tool set, not a stub of the stored hash. Absent when the
  // server is in sync (no drift) or has never been approved (first
  // connect) — the UI must not show a diff in that case.
  new_description_hash?: string;
  config_hash?: string;
  new_config_hash?: string;
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
  approval_kind: "config" | "tools";
  config_hash?: string;
  description_hash?: string;
}
