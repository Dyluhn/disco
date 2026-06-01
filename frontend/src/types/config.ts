/** Skills + MCP connection types (Settings scaffolds, Prompt 4). These surfaces
 *  are present and interactive against fixtures but WIRING-PENDING — their
 *  subsystems land in later phases. The UI marks that honestly. */

export interface Skill {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
}

export type McpStatus = "connected" | "disconnected" | "error";

export interface McpConnection {
  id: string;
  name: string;
  url: string;
  status: McpStatus;
}
