/**
 * UI-7 — the committed-workspace manifest is not fetched before it can exist.
 *
 * While a build was still RUNNING the canvas asked for
 * `/api/projects/<cid>/manifest`, got the entirely expected 404 (nothing has
 * been committed yet), and put a console error on the page on every visit. The
 * request is now gated on a workspace actually having been sealed.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent } from "@/types/agent";

const { useProjectManifestMock, useProjectFileMock } = vi.hoisted(() => ({
  useProjectManifestMock: vi.fn<(...args: unknown[]) => { data?: { files: { path: string; bytes: number }[] } }>(() => ({ data: undefined })),
  useProjectFileMock: vi.fn(() => ({ data: { content: "Imported source text", binary: false }, isPending: false, isError: false })),
}));

vi.mock("@/hooks/useProjects", () => ({
  useProjectManifest: (...args: unknown[]) => useProjectManifestMock(...args),
  useProjectFile: (...args: unknown[]) => useProjectFileMock(...(args as [])),
}));

vi.mock("@/hooks/useSessions", () => ({
  useSessions: () => ({
    sessions: [],
    selectedName: null,
    setSelectedName: vi.fn(),
    view: null,
  }),
}));

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: () => ({ data: null }),
}));

import { ExecutionCanvas } from "./ExecutionCanvas";

function withQc(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

const sealed = {
  id: "wv-1",
  kind: "workspace_version",
  version_seq: 1,
  tree_digest: "sha256:manifest-test",
  trigger: "turn",
} as unknown as AgentEvent;

afterEach(() => {
  vi.clearAllMocks();
  useProjectManifestMock.mockReturnValue({ data: undefined });
});

describe("ExecutionCanvas — the project manifest is only requested once one can exist", () => {
  it("does NOT request the manifest while a run is still working with nothing committed", () => {
    render(withQc(<ExecutionCanvas events={[]} status="RUNNING" cid="conv_running" />));
    expect(useProjectManifestMock).toHaveBeenCalledWith("conv_running", false, "RUNNING:");
  });

  it("requests it as soon as a workspace version has landed mid-run", () => {
    render(withQc(<ExecutionCanvas events={[sealed]} status="RUNNING" cid="conv_sealed" />));
    expect(useProjectManifestMock).toHaveBeenCalledWith("conv_sealed", true, "RUNNING:wv-1");
  });

  it("requests it on a finished run", () => {
    render(withQc(<ExecutionCanvas events={[]} status="FINISHED" cid="conv_done" />));
    expect(useProjectManifestMock).toHaveBeenCalledWith("conv_done", true, "FINISHED:");
  });
});


const importReady: AgentEvent = {
  id: "import-ready", kind: "message", source: "environment",
  message: { role: "user", content: "Imported files" },
  meta: { project_import: { workspace_ready: true } },
};

it("shows imported files and actual bytes before continuation and after reload", () => {
  useProjectManifestMock.mockReturnValue({ data: { files: [{ path: "src/original.txt", bytes: 20 }] } });
  const mount = () => render(withQc(<ExecutionCanvas events={[importReady]} status="IDLE" cid="conv_import" />));
  const view = mount();
  expect(useProjectManifestMock).toHaveBeenCalledWith("conv_import", true, "IDLE:import-ready");
  expect(screen.getByRole("button", { name: "src/original.txt" })).toBeVisible();
  expect(screen.getByText("Imported source text")).toBeVisible();
  expect(useProjectFileMock).toHaveBeenCalledWith("conv_import", "src/original.txt", "IDLE:import-ready");
  view.unmount(); mount();
  expect(screen.getByText("Imported source text")).toBeVisible();
});

it.each([
  { event: { ...importReady, source: "user" }, untrusted: false },
  { event: { ...importReady, meta: {} }, untrusted: false },
  { event: { ...importReady, meta: { project_import: { workspace_ready: "true" } } }, untrusted: false },
  { event: importReady, untrusted: true },
])("ignores user text, malformed hints, and imported-share events: %j", ({ event, untrusted }) => {
  render(withQc(<ExecutionCanvas events={[event as AgentEvent]} untrusted={untrusted} status="IDLE" cid="conv_unready" />));
  expect(useProjectManifestMock).toHaveBeenCalledWith("conv_unready", false, "IDLE:");
  expect(useProjectFileMock).not.toHaveBeenCalled();
});
