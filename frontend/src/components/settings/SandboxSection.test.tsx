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

// save/test mutates invoke their onSuccess so the W-48 preflight cascade (save →
// probe → surface verdict) is exercised end-to-end in the component.
const saveMutate = vi.fn((_cfg, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.());
const probeResult = {
  ok: false,
  status: "unreachable",
  detail: "gvisor sandbox host ssh://sandbox@host unreachable: connection refused",
};
const testMutate = vi.fn((_cfg, opts?: { onSuccess?: (r: unknown) => void }) =>
  opts?.onSuccess?.(probeResult),
);

vi.mock("@/hooks/useModels", () => ({
  useSandboxConfig: vi.fn(() => ({ data: DEFAULTS })),
  useUpdateSandboxConfig: vi.fn(() => ({
    mutate: saveMutate,
    isPending: false,
    error: null,
  })),
  useTestSandbox: vi.fn(() => ({ mutate: testMutate, isPending: false })),
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

  it("podman is temporarily NOT offered in the picker (commented out — no live host here)", async () => {
    wrap();
    // gVisor renders once the async config has loaded, so asserting podman's absence
    // afterwards isn't racing the initial render.
    await screen.findByRole("radio", { name: /gvisor/i });
    expect(screen.queryByRole("radio", { name: /podman/i })).toBeNull();
    // ...but podman_url stays in the round-tripped config shape (the "Save sends the FULL
    // payload" test below proves the field survives), so re-enabling the card is a pure
    // BACKEND_META un-comment with no wire change.
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
    // W-48(b): selecting gVisor seeds the remote-host field (the LOCAL socket default
    // is the wrong tier for the REMOTE gVisor backend) so the user only edits the host.
    expect(payload.docker_socket).toBe("ssh://sandbox@");
    expect(payload.podman_url).toBe(DEFAULTS.podman_url);
  });
});

describe("SandboxSection — W-48 connectivity preflight + gVisor pre-fill", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("gVisor: pre-fills ssh://sandbox@ + shows the tailnet-IP hint", async () => {
    wrap();
    const gvisor = await screen.findByRole("radio", { name: /gvisor/i });
    await userEvent.click(gvisor);
    const input = (await waitFor(() => {
      const el = document.querySelector('input[data-sandbox-field="docker_socket"]');
      expect(el).not.toBeNull();
      return el as HTMLInputElement;
    }));
    // Seeded with the ssh:// template (not the wrong-tier local socket default).
    expect(input.value).toBe("ssh://sandbox@");
    // The inline "use the tailnet IP, not the LAN IP" guidance is shown.
    expect(screen.getByText(/tailscale ip/i)).toBeInTheDocument();
    expect(screen.getByText(/not the lan ip/i)).toBeInTheDocument();
  });

  it("Save runs the connectivity preflight and surfaces the typed named verdict", async () => {
    wrap();
    const gvisor = await screen.findByRole("radio", { name: /gvisor/i });
    await userEvent.click(gvisor);
    const saveBtn = await screen.findByRole("button", { name: /save sandbox/i });
    await waitFor(() => expect(saveBtn).toBeEnabled());
    await userEvent.click(saveBtn);
    // save → (onSuccess) → test probe → surface the verdict
    expect(saveMutate).toHaveBeenCalledTimes(1);
    expect(testMutate).toHaveBeenCalledTimes(1);
    const verdict = await screen.findByRole("status");
    expect(verdict).toHaveAttribute("data-sandbox-probe", "unreachable");
    expect(verdict).toHaveTextContent(/ssh:\/\/sandbox@host unreachable/i);
  });

  it("Test connection button probes the draft directly", async () => {
    wrap();
    const gvisor = await screen.findByRole("radio", { name: /gvisor/i });
    await userEvent.click(gvisor);
    const testBtn = await screen.findByRole("button", { name: /test connection/i });
    await userEvent.click(testBtn);
    expect(testMutate).toHaveBeenCalledTimes(1);
    expect(saveMutate).not.toHaveBeenCalled();
    expect(await screen.findByRole("status")).toHaveTextContent(/unreachable/i);
  });
});
