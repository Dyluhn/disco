/** Wire shapes of `GET /api/diagnostics` (agent-server) and `GET /api/health` (app-server). */

export interface BuildInfo {
  tag: string;
  commit: string;
  source: "image" | "checkout" | "unknown" | string;
}

export interface DiagnosticCheck {
  name: string;
  status: "PASS" | "WARN" | "FAIL" | "SKIP" | string;
  detail: string;
}

export interface AgentServerHealth {
  status: "ok" | "degraded" | string;
  version: string;
  build: BuildInfo;
  checks: Record<string, string | number>;
}

export interface Diagnostics {
  generated_at: string;
  version: string;
  build: BuildInfo;
  agent_server: AgentServerHealth;
  sandbox: { reachable: boolean; backend: string; detail: string };
  checks: DiagnosticCheck[];
  env: Record<string, string>;
}

export interface AppServerHealth {
  status: string;
  service: string;
  version: string;
  build: BuildInfo;
}
