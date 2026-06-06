/**
 * Sandbox backend config (settings) — the wire mirror of the app-server's
 * SandboxConfigDTO, plus the static per-backend legibility (isolation tier, adversarial
 * safety, the confirmation coupling, the Podman-stub flag). The legibility is UI copy
 * that mirrors `tools/sandbox/isolation.py`; the editable config round-trips to the server.
 */

export interface SandboxConfig {
  backend: string; // "process" | "gvisor" | "local" | "podman"
  docker_socket: string;
  podman_url: string;
  runtime: string;
  image: string;
  workspace_root: string;
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
  },
];

export function backendMeta(id: string): BackendMeta | undefined {
  return BACKEND_META.find((b) => b.id === id);
}
