/** The backend-aware live preview: a URL to iframe the agent's running dev server, or a
 * reason it isn't available (no server yet / Podman stub). */
export interface PreviewInfo {
  available: boolean;
  proxy?: boolean; // served via the agent-server proxy (the browser builds the URL)
  reason?: string;
  stub?: boolean;
  /** Exact platform-managed process generation. Rotates on restart/recovery. */
  generation?: string;
  /** Current PreviewManager lifecycle state. */
  status?: "starting" | "running" | "unavailable" | "restarting" | "crashed" | "stopped";
  /** Authoritative launcher class, not inferred from a process name in the UI. */
  launch_kind?: "static" | "framework" | "custom" | string;
  /** Whether workspace changes are applied by the runtime or need a frame reload. */
  reload_strategy?: "hmr" | "reload";
  /** Latest host-owned refresh failure while the prior healthy frame remains visible. */
  update_error?: string | null;
  /** Canonical platform-selected runtime port; clients never choose it. */
  port?: number | null;
  owner?: {
    pid: number;
    cmdline: string;
    session: string | null;
  } | null;
  ports?: Array<{
    port: number;
    owner: { pid: number; cmdline: string; session: string | null } | null;
  }>;
}
