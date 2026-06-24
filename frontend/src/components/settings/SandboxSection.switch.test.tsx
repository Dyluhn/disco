/**
 * P1 #2 — backend switch must NOT bleed one backend's socket into another.
 *
 * The outage residue: an OLD flat config (active gvisor + a bad host-less
 * `ssh://sandbox@` docker_socket, NO per-backend `connections.local` block). When the
 * user switches to Local, Local must get a CLEAN, backend-appropriate connection (the
 * local docker socket) — NEVER the carried-over gvisor `ssh://` socket. Switching back
 * to gVisor must still surface the (still-invalid) gvisor socket so the user can fix it.
 *
 * The server normally supplies a clean per-backend block for every backend; this test
 * also covers the DEFENSIVE path where the client has no `connections` map at all.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SandboxConfig } from "@/types/sandbox";
import { SandboxSection } from "./SandboxSection";

// The OLD flat outage config the server would migrate: active gvisor with a host-less
// socket. `connections` carries ONLY the active backend's (bad) block — exactly what the
// mapper produces for a migrated flat config (no clean local block was ever saved, but the
// server now also seeds clean defaults; we test BOTH by toggling this).
function makeData(withCleanLocal: boolean): SandboxConfig {
  const base: SandboxConfig = {
    backend: "gvisor",
    docker_socket: "ssh://sandbox@", // the bad host-less socket
    podman_url: "ssh://user@podhost/run/podman.sock",
    runtime: "runsc",
    image: "disco-sandbox:base",
    workspace_root: "/srv/disco/workspaces",
    connections: {
      gvisor: {
        docker_socket: "ssh://sandbox@",
        podman_url: "ssh://user@podhost/run/podman.sock",
        runtime: "runsc",
        image: "disco-sandbox:base",
        workspace_root: "/srv/disco/workspaces",
      },
    },
  };
  if (withCleanLocal) {
    base.connections!.local = {
      docker_socket: "unix:///var/run/docker.sock",
      podman_url: "ssh://user@podhost/run/podman.sock",
      runtime: "runc",
      image: "disco-sandbox:base",
      workspace_root: "/srv/disco/workspaces",
    };
  }
  return base;
}

const saveMutate = vi.fn((_cfg, opts?: { onSuccess?: () => void }) => opts?.onSuccess?.());
const testMutate = vi.fn((_cfg, opts?: { onSuccess?: (r: unknown) => void }) =>
  opts?.onSuccess?.({ ok: true, status: "ok", detail: "ok" }),
);

let currentData: SandboxConfig;
vi.mock("@/hooks/useModels", () => ({
  useSandboxConfig: vi.fn(() => ({ data: currentData })),
  useUpdateSandboxConfig: vi.fn(() => ({ mutate: saveMutate, isPending: false, error: null })),
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

async function savedPayload(): Promise<SandboxConfig> {
  const saveBtn = await screen.findByRole("button", { name: /save sandbox/i });
  await waitFor(() => expect(saveBtn).toBeEnabled());
  await userEvent.click(saveBtn);
  return saveMutate.mock.calls.at(-1)![0] as SandboxConfig;
}

afterEach(() => vi.clearAllMocks());

describe("SandboxSection — backend switch does not bleed sockets", () => {
  it("old-flat bad-gvisor config → switch to Local → Local gets a CLEAN socket (no ssh)", async () => {
    currentData = makeData(true); // server supplied a clean local block
    wrap();
    const local = await screen.findByRole("radio", { name: /local container/i });
    await userEvent.click(local);
    const payload = await savedPayload();
    expect(payload.backend).toBe("local");
    // The bad gvisor socket must NOT carry over — Local gets its own clean docker socket.
    expect(payload.docker_socket).not.toContain("ssh://sandbox@");
    expect(payload.docker_socket).toBe("unix:///var/run/docker.sock");
    expect(payload.runtime).toBe("runc");
  });

  it("DEFENSIVE: no connections map at all → switch to Local still gets a clean socket", async () => {
    currentData = { ...makeData(false), connections: undefined };
    wrap();
    const local = await screen.findByRole("radio", { name: /local container/i });
    await userEvent.click(local);
    const payload = await savedPayload();
    expect(payload.backend).toBe("local");
    expect(payload.docker_socket).not.toContain("ssh://sandbox@");
    expect(payload.docker_socket).toBe("unix:///var/run/docker.sock");
  });

  it("switching back to gVisor still shows the (invalid) gvisor socket to fix", async () => {
    currentData = makeData(true);
    wrap();
    // start on gvisor (active), go to local, then back to gvisor
    await userEvent.click(await screen.findByRole("radio", { name: /local container/i }));
    await userEvent.click(await screen.findByRole("radio", { name: /gvisor/i }));
    const input = await waitFor(() => {
      const el = document.querySelector('input[data-sandbox-field="docker_socket"]') as HTMLInputElement;
      expect(el).not.toBeNull();
      return el;
    });
    // gVisor's own (still host-less) socket is restored so the user can complete the host.
    expect(input.value).toBe("ssh://sandbox@");
  });
});
