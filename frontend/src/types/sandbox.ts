/**
 * Sandbox backend config (settings) — the wire mirror of the app-server's
 * SandboxConfigDTO, plus the static per-backend legibility (isolation tier, adversarial
 * safety, the confirmation coupling, the Podman-stub flag). The legibility is UI copy
 * that mirrors `tools/sandbox/isolation.py`; the editable config round-trips to the server.
 */

/** One backend's saved connection block — mirrors the server's SandboxConnectionDTO.
 * Carried in SandboxConfig.connections so switching to a backend RESTORES its own
 * last-known setup instead of blanking it (the gVisor-socket-blanking outage fix). */
export interface SandboxConnection {
  docker_socket: string;
  podman_url: string;
  runtime: string;
  image: string;
  workspace_root: string;
}

export interface SandboxConfig {
  backend: string; // "process" | "gvisor" | "local" | "podman"
  docker_socket: string;
  podman_url: string;
  runtime: string;
  image: string;
  workspace_root: string;
  // per-backend saved connection blocks (backend id → its connection). Server-populated;
  // the UI reads it to restore a backend's own connection when you flip to it.
  connections?: Record<string, SandboxConnection>;
}

/** Reachability of the active sandbox backend — mirrors the server's SandboxHealthDTO.
 * Polled by the app shell to surface an unreachable sandbox BEFORE a run is started. */
export interface SandboxHealth {
  reachable: boolean;
  backend: string;
  detail: string;
}

export type SandboxField = "docker_socket" | "podman_url" | "runtime" | "image" | "workspace_root";

export interface BackendMeta {
  id: string;
  name: string;
  tier: string; // isolation tier, legible at the point of choice
  adversarialSafe: boolean;
  stub: boolean; // Podman = verified code, but not live/verifiable in THIS environment
  blurb: string;
  fields: SandboxField[]; // which connection fields this backend uses
  defaultRuntime: string;
  confirmNote: string; // the isolation → confirmation coupling, surfaced
  // W-49: progressive disclosure. `primaryField` is the ONE connection field that
  // varies per backend and is shown up-front (null = none → zero typing). Every other
  // field (runtime is dropped from the form entirely; image/workspace_root and any
  // non-primary connection field) collapses under "Advanced" with its saved default.
  primaryField: SandboxField | null;
  primaryLabel?: string; // friendlier label for the primary field at the point of use
  // W-48(b): when this backend is selected and the primary field is still empty / a
  // wrong-tier default (e.g. the LOCAL docker socket left on the REMOTE gVisor tier),
  // seed it with this template so the user only edits the ONE varying part (the host).
  primaryPrefill?: string;
  primaryHint?: string; // inline guidance under the primary field (e.g. "use the tailnet IP")
  provides: string; // the short "what you provide" subline
  // Whether this backend can actually run + stream the noVNC live-view stack
  // (Xvfb/x11vnc/websockify). MUST mirror the server's LIVE_VIEW_BACKENDS set
  // (_container.py): only gVisor ships the stack + carries the accepted live-jail
  // security model. The Live-browser Settings toggle is greyed (and enabling is
  // rejected) when the selected backend has this false.
  supportsLiveView: boolean;
}

const FIELD_LABEL: Record<SandboxField, string> = {
  docker_socket: "Docker endpoint (local socket or ssh://user@host)",
  podman_url: "Podman remote URL (rootless socket over Tailscale SSH)",
  runtime: "OCI runtime",
  image: "Sandbox image",
  workspace_root: "Workspace root (host path)",
};

export function fieldLabel(f: SandboxField): string {
  return FIELD_LABEL[f];
}

/** The three selectable backends (process/dev is intentionally not offered — running on
 * the host isn't an isolation choice). Ordered strong → weak, like isolation.py. */
export const BACKEND_META: BackendMeta[] = [
  {
    id: "gvisor",
    name: "gVisor",
    tier: "Strong isolation",
    adversarialSafe: true,
    stub: false,
    blurb:
      "gVisor user-space kernel (syscall interception) on a remote host — Docker over keyless Tailscale SSH. Safe for untrusted / adversarial code.",
    fields: ["docker_socket", "runtime", "image", "workspace_root"],
    defaultRuntime: "runsc",
    confirmNote: "Strong sandbox → by default the agent only pauses on HIGH-risk actions.",
    primaryField: "docker_socket",
    primaryLabel: "Remote host (Tailscale)",
    primaryPrefill: "ssh://sandbox@",
    primaryHint:
      "Use your Tailscale IP or hostname (e.g. ssh://sandbox@100.x.y.z) — NOT the LAN IP. Only the tailnet reaches the host keyless.",
    provides: "You provide: the remote host to reach over Tailscale SSH. Everything else has a default.",
    supportsLiveView: true, // the only backend that ships the noVNC stack + live-jail model
  },
  {
    id: "local",
    name: "Local container",
    tier: "Container-grade · shared kernel",
    adversarialSafe: false,
    stub: false,
    blurb:
      "A runc container on this host — real namespaces/cgroups but a SHARED kernel, weaker than gVisor. For trusted local use, not adversarial workloads.",
    fields: ["docker_socket", "runtime", "image", "workspace_root"],
    defaultRuntime: "runc",
    confirmNote:
      "Lower isolation → the confirmation default leans TIGHTER: risky actions pause at MEDIUM, not just HIGH.",
    primaryField: null,
    provides: "You provide: nothing — it runs on this host with sensible defaults. Save and go.",
    supportsLiveView: false, // shared-kernel local backend: no live stream here
  },
  {
    id: "podman",
    name: "Podman (remote)",
    tier: "Stub — completed at deployment",
    adversarialSafe: false,
    stub: true,
    blurb:
      "Rootless Podman/crun over keyless Tailscale SSH. The backend code is real and verified, but it is a STUB in this environment — it configures but isn't live here.",
    fields: ["podman_url", "runtime", "image"],
    defaultRuntime: "crun",
    confirmNote: "Container-grade behind a host boundary — not for adversarial workloads.",
    primaryField: null,
    provides: "You provide: the rootless Podman URL under Advanced. A stub here — configures but doesn't run.",
    supportsLiveView: false, // deployment stub; doesn't expose preview ports here
  },
];

export function backendMeta(id: string): BackendMeta | undefined {
  return BACKEND_META.find((b) => b.id === id);
}

/** Whether the given sandbox backend can run + stream the noVNC live view. Mirrors the
 * server's LIVE_VIEW_BACKENDS (only gVisor). Unknown/undefined backend → false (honest:
 * if we don't recognise it, don't claim live-browser works). */
export function backendSupportsLiveView(backendId: string | undefined | null): boolean {
  return !!backendId && (backendMeta(backendId)?.supportsLiveView ?? false);
}
