import type { McpConnection, Skill } from "@/types/config";

/** Fixture skills — the surface is built against these first; the skills
 *  subsystem populates this for real in a later phase. */
export const SKILLS: Skill[] = [
  {
    id: "web-research",
    name: "Web research",
    description: "Search, extract, and ground answers against live sources.",
    enabled: true,
  },
  {
    id: "code-exec",
    name: "Code execution",
    description: "Run code in the sandbox to compute and verify.",
    enabled: true,
  },
  {
    id: "doc-analysis",
    name: "Document analysis",
    description: "Read and reason over uploaded documents.",
    enabled: false,
  },
];

/** Fixture MCP connections — external tool servers. The MCP client populates
 *  these for real in a later phase. */
export const MCP_CONNECTIONS: McpConnection[] = [
  { id: "fs", name: "Filesystem", url: "stdio://mcp-server-filesystem", status: "connected" },
  { id: "gh", name: "GitHub", url: "https://mcp.github.local", status: "disconnected" },
];
