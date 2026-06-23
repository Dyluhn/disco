/**
 * W-49: SandboxSection progressive disclosure. Asserts that the form shows exactly the
 * ONE connection field that varies per backend up front, folds the rest under Advanced,
 * drops `runtime` from the form, and STILL round-trips the full payload (runtime/image/
 * workspace_root defaults) on Save — the payload shape is unchanged.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SandboxConfig } from "@/types/sandbox";
import { SandboxSection } from "./SandboxSection";

const DEFAULTS: SandboxConfig = {
  backend: "local",
  docker_socket: "unix:///var/run/docker.sock",
  podman_url: "ssh://user@podhost/run/podman.sock",
  runtime: "runc",
  image: "disco-sandbox:base",
  workspace_root: "/srv/disco/workspaces",
};

const saveMutate = vi.fn();

vi.mock("@/hooks/useModels", () => ({
  useSandboxConfig: vi.fn(() => ({ data: DEFAULTS })),
  useUpdateSandboxConfig: vi.fn(() => ({
    mutate: saveMutate,
    isPending: false,
    error: null,
  })),
}));

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SandboxSection />
    </QueryClientProvider>,
  );
}

/** A field is "visible up front" when its input is present and NOT inside a <details>. */
function fieldShownUpFront(field: string): boolean {
  const input = document.querySelector(`input[data-sandbox-field="${field}"]`);
  if (!input) return false;
  return input.closest("details") === null;
}

describe("SandboxSection — W-49 progressive disclosure", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("local: shows NO connection field up front, folds the rest under Advanced", async () => {
    wrap();
    await screen.findByRole("radiogroup", { name: /sandbox backend/i });
    // local = none up front (zero typing). docker_socket/image/workspace_root all hidden
    // under Advanced; runtime is dropped from the form entirely.
    expect(fieldShownUpFront("docker_socket")).toBe(false);
    expect(document.querySelector('input[data-sandbox-field="runtime"]')).toBeNull();
    const details = document.querySelector("details");
    expect(details).not.toBeNull();
    // The Advanced block carries image + workspace_root + docker_socket.
    const inDetails = within(details as HTMLElement);
    expect(inDetails.getByText(/sandbox image/i)).toBeInTheDocument();
    expect(inDetails.getByText(/workspace root/i)).toBeInTheDocument();
  });

  it("gVisor: shows exactly ONE up-front connection field (Remote host)", async () => {
    wrap();
    const gvisor = await screen.findByRole("radio", { name: /gvisor/i });
    await userEvent.click(gvisor);
    // The single varying field is docker_socket, relabelled "Remote host (Tailscale)".
    await waitFor(() => expect(fieldShownUpFront("docker_socket")).toBe(true));
    expect(screen.getByText(/remote host \(tailscale\)/i)).toBeInTheDocument();
    // runtime never appears as an input.
    expect(document.querySelector('input[data-sandbox-field="runtime"]')).toBeNull();
    // image + workspace_root are still present, but folded under Advanced.
    expect(fieldShownUpFront("image")).toBe(false);
    expect(fieldShownUpFront("workspace_root")).toBe(false);
  });

  it("podman: podman_url lives under Advanced (none up front)", async () => {
    wrap();
    const podman = await screen.findByRole("radio", { name: /podman/i });
    await userEvent.click(podman);
    await waitFor(() => expect(fieldShownUpFront("podman_url")).toBe(false));
    // podman_url IS rendered (in Advanced), just not up front.
    expect(document.querySelector('input[data-sandbox-field="podman_url"]')).not.toBeNull();
  });

  it("Save still sends the FULL payload incl. runtime/image/workspace_root defaults", async () => {
    wrap();
    const gvisor = await screen.findByRole("radio", { name: /gvisor/i });
    await userEvent.click(gvisor); // makes the draft dirty (backend + runtime change)
    const saveBtn = await screen.findByRole("button", { name: /save sandbox/i });
    await waitFor(() => expect(saveBtn).toBeEnabled());
    await userEvent.click(saveBtn);
    expect(saveMutate).toHaveBeenCalledTimes(1);
    const payload = saveMutate.mock.calls[0][0] as SandboxConfig;
    // Payload SHAPE unchanged — all six keys present, hidden fields kept their defaults,
    // runtime seeded to the gVisor default even though it's not in the form.
    expect(payload.backend).toBe("gvisor");
    expect(payload.runtime).toBe("runsc");
    expect(payload.image).toBe(DEFAULTS.image);
    expect(payload.workspace_root).toBe(DEFAULTS.workspace_root);
    expect(payload.docker_socket).toBe(DEFAULTS.docker_socket);
    expect(payload.podman_url).toBe(DEFAULTS.podman_url);
  });
});
