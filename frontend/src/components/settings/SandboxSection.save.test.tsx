/**
 * Sandbox save canonical-response regression.
 *
 * Firefox repro on identical production tree: Settings Sandbox Local Advanced
 * change socket/image -> Test succeeds -> Save PUT 200 normalized config with
 * connections.local updated -> button still enabled "Save sandbox". Reload shows
 * "Saved" disabled and persisted fields correct.
 *
 * Root cause under test: SandboxSection initializes draft only when null,
 * compares JSON.stringify(draft) against query data, and onSave never adopts
 * the canonical PUT response (useUpdateSandboxConfig writes it to React Query).
 * So draft stays stale vs canonical data -> dirty stays true, and the
 * post-save probe fires with the stale draft instead of normalized values.
 *
 * Uses real React Query hooks with the API boundary mocked (@/api/models), so
 * the query-cache write + component dirty check run for real. Every successful
 * save mock resolves a FULL canonical SandboxConfig (never empty).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SandboxSection } from "./SandboxSection";
import type { SandboxConfig } from "@/types/sandbox";
import type { ProbeResult } from "@/types/probe";

const { getSandboxConfigMock, updateSandboxConfigMock, testSandboxMock } =
  vi.hoisted(() => ({
    getSandboxConfigMock: vi.fn(),
    updateSandboxConfigMock: vi.fn(),
    testSandboxMock: vi.fn(),
  }));

vi.mock("@/api/models", () => ({
  getSandboxConfig: getSandboxConfigMock,
  updateSandboxConfig: updateSandboxConfigMock,
  testSandbox: testSandboxMock,
}));

const LOCAL_SOCKET = "unix:///var/run/docker.sock";
const GVISOR_SOCKET = "ssh://sandbox@100.1.2.3";
const EDITED_SOCKET = "unix:///var/run/docker-alt.sock";
const EDITED_IMAGE = "disco-sandbox:edited";

function baseConnections() {
  return {
    local: {
      docker_socket: LOCAL_SOCKET,
      podman_url: "ssh://user@podhost/run/podman.sock",
      runtime: "runc",
      image: "disco-sandbox:base",
      workspace_root: "/srv/disco/workspaces",
    },
    gvisor: {
      docker_socket: GVISOR_SOCKET,
      podman_url: "ssh://user@podhost/run/podman.sock",
      runtime: "runsc",
      image: "disco-sandbox:base",
      workspace_root: "/srv/disco/workspaces",
    },
  };
}

function initialData(): SandboxConfig {
  return {
    backend: "local",
    docker_socket: LOCAL_SOCKET,
    podman_url: "ssh://user@podhost/run/podman.sock",
    runtime: "runc",
    image: "disco-sandbox:base",
    workspace_root: "/srv/disco/workspaces",
    connections: baseConnections(),
  };
}

/** Backend owns canonical per-backend connections: submitted flat fields folded
 * into connections[backend]. The canonical response therefore differs from the
 * submitted draft in `connections` even when flat fields match. */
function canonicalFor(submitted: SandboxConfig): SandboxConfig {
  return {
    ...submitted,
    connections: {
      ...(submitted.connections ?? {}),
      [submitted.backend]: {
        docker_socket: submitted.docker_socket,
        podman_url: submitted.podman_url,
        runtime: submitted.runtime,
        image: submitted.image,
        workspace_root: submitted.workspace_root,
      },
    },
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const okProbe: ProbeResult = { ok: true, status: "ok", detail: "ok" };

function wrap() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={qc}>
      <SandboxSection />
    </QueryClientProvider>,
  );
}

function socketInput(): HTMLInputElement {
  const el = document.querySelector(
    'input[data-sandbox-field="docker_socket"]',
  ) as HTMLInputElement | null;
  if (!el) throw new Error("missing docker_socket input");
  return el;
}

function imageInput(): HTMLInputElement {
  const el = document.querySelector(
    'input[data-sandbox-field="image"]',
  ) as HTMLInputElement | null;
  if (!el) throw new Error("missing image input");
  return el;
}

async function saveButton(): Promise<HTMLElement> {
  return screen.findByRole("button", { name: "Saved" });
}

async function editLocalSocketAndImage(socket: string, image: string) {
  fireEvent.change(socketInput(), { target: { value: socket } });
  fireEvent.change(imageInput(), { target: { value: image } });
}

beforeEach(() => {
  vi.clearAllMocks();
  getSandboxConfigMock.mockReset();
  updateSandboxConfigMock.mockReset();
  testSandboxMock.mockReset();
  testSandboxMock.mockResolvedValue(okProbe);
});

describe("SandboxSection — save adopts canonical response", () => {
  it("successful save becomes Saved disabled without reload; probe uses normalized values", async () => {
    getSandboxConfigMock.mockResolvedValue(initialData());
    let submitted!: SandboxConfig;
    const saveGate = deferred<SandboxConfig>();
    updateSandboxConfigMock.mockImplementation((cfg: SandboxConfig) => {
      submitted = cfg;
      return saveGate.promise.then(() => canonicalFor(submitted));
    });
    wrap();

    const saveBtn = await saveButton();
    await waitFor(() => expect(saveBtn).toHaveTextContent(/saved/i));
    await editLocalSocketAndImage(EDITED_SOCKET, EDITED_IMAGE);
    await waitFor(() => expect(saveBtn).toBeEnabled());

    await userEvent.click(saveBtn);
    expect(updateSandboxConfigMock).toHaveBeenCalledTimes(1);
    // Resolve the canonical 200 (differs in connections.local).
    saveGate.resolve(canonicalFor(submitted));

    // Without reload: draft adopts canonical -> not dirty -> Saved disabled.
    await waitFor(() => expect(saveBtn).toBeDisabled());
    await waitFor(() => expect(saveBtn).toHaveTextContent(/saved/i));
    expect(socketInput().value).toBe(EDITED_SOCKET);
    expect(imageInput().value).toBe(EDITED_IMAGE);

    // Post-save connectivity probe runs against normalized values, not stale draft.
    await waitFor(() => expect(testSandboxMock).toHaveBeenCalledTimes(1));
    const probed = testSandboxMock.mock.calls[0][0] as SandboxConfig;
    expect(probed.docker_socket).toBe(EDITED_SOCKET);
    expect(probed.image).toBe(EDITED_IMAGE);
    expect(probed.connections?.local?.docker_socket).toBe(EDITED_SOCKET);
    expect(probed.connections?.local?.image).toBe(EDITED_IMAGE);
  });

  it("edit while save pending is not overwritten or marked saved by the stale response", async () => {
    getSandboxConfigMock.mockResolvedValue(initialData());
    let firstSubmitted!: SandboxConfig;
    const saveGate = deferred<SandboxConfig>();
    updateSandboxConfigMock.mockImplementationOnce((cfg: SandboxConfig) => {
      firstSubmitted = cfg;
      return saveGate.promise.then(() => canonicalFor(firstSubmitted));
    });
    wrap();

    const saveBtn = await saveButton();
    await editLocalSocketAndImage(EDITED_SOCKET, "disco-sandbox:v1");
    await waitFor(() => expect(saveBtn).toBeEnabled());
    await userEvent.click(saveBtn);
    await waitFor(() => expect(updateSandboxConfigMock).toHaveBeenCalledTimes(1));

    // Edit while the first save is still pending.
    fireEvent.change(imageInput(), { target: { value: "disco-sandbox:v2" } });

    // Stale first response resolves (canonical for v1, not v2).
    saveGate.resolve(canonicalFor(firstSubmitted));
    // Give the stale onSuccess a chance to wrongly adopt/overwrite.
    await waitFor(() => expect(testSandboxMock).toHaveBeenCalledTimes(1));

    // Newer edit survives; still dirty/retryable, never flipped to Saved.
    expect(imageInput().value).toBe("disco-sandbox:v2");
    await waitFor(() => expect(saveBtn).toBeEnabled());
    expect(saveBtn).toHaveTextContent(/save sandbox/i);
  });

  it("failed save retains input and remains retryable", async () => {
    getSandboxConfigMock.mockResolvedValue(initialData());
    updateSandboxConfigMock.mockRejectedValueOnce(new Error("save failed"));
    updateSandboxConfigMock.mockImplementation((cfg: SandboxConfig) =>
      Promise.resolve(canonicalFor(cfg)),
    );
    wrap();

    const saveBtn = await saveButton();
    await editLocalSocketAndImage(EDITED_SOCKET, EDITED_IMAGE);
    await waitFor(() => expect(saveBtn).toBeEnabled());
    await userEvent.click(saveBtn);

    await screen.findByRole("alert");
    expect(socketInput().value).toBe(EDITED_SOCKET);
    expect(imageInput().value).toBe(EDITED_IMAGE);
    await waitFor(() => expect(saveBtn).toBeEnabled());
    expect(saveBtn).toHaveTextContent(/save sandbox/i);

    // Retry with the retained input succeeds to Saved without reload.
    await userEvent.click(saveBtn);
    await waitFor(() => expect(saveBtn).toBeDisabled());
    expect(saveBtn).toHaveTextContent(/saved/i);
  });

  it("switching backend retains its OWN saved connection fields", async () => {
    getSandboxConfigMock.mockResolvedValue(initialData());
    updateSandboxConfigMock.mockImplementation((cfg: SandboxConfig) =>
      Promise.resolve(canonicalFor(cfg)),
    );
    wrap();

    const saveBtn = await saveButton();
    await editLocalSocketAndImage(EDITED_SOCKET, EDITED_IMAGE);
    await waitFor(() => expect(saveBtn).toBeEnabled());
    await userEvent.click(saveBtn);
    await waitFor(() => expect(testSandboxMock).toHaveBeenCalledTimes(1));

    await userEvent.click(await screen.findByRole("radio", { name: /gvisor/i }));
    await waitFor(() => expect(socketInput().value).toBe(GVISOR_SOCKET));

    await userEvent.click(
      await screen.findByRole("radio", { name: /local container/i }),
    );
    await waitFor(() => expect(socketInput().value).toBe(EDITED_SOCKET));
    expect(imageInput().value).toBe(EDITED_IMAGE);
  });
});
