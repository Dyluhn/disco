import { finishedWorkspaceEvents } from "@/test/workspaceFixtures";
/**
 * UI-7 — the committed-workspace manifest is not fetched before it can exist.
 *
 * While a build was still RUNNING the canvas asked for
 * `/api/projects/<cid>/manifest`, got the entirely expected 404 (nothing has
 * been committed yet), and put a console error on the page on every visit. The
 * request is now gated on a workspace actually having been sealed.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent } from "@/types/agent";

const { useProjectManifestMock, useProjectFileMock, listProjectsMock } = vi.hoisted(() => ({
  useProjectManifestMock: vi.fn<(...args: unknown[]) => { data?: { files: { path: string; bytes: number }[] } }>(() => ({ data: undefined })),
  listProjectsMock: vi.fn(async () => ({ projects: [] as { id: string; files_missing: boolean; last_snapshot_at?: string }[], status: "ok" })),
  useProjectFileMock: vi.fn(() => ({ data: { content: "Imported source text", binary: false }, isPending: false, isError: false })),
}));

vi.mock("@/api/projects", () => ({ listProjects: listProjectsMock }));

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
  listProjectsMock.mockResolvedValue({ projects: [], status: "ok" });
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
    render(withQc(<ExecutionCanvas events={finishedWorkspaceEvents("conv_done")} status="FINISHED" cid="conv_done" />));
    expect(useProjectManifestMock).toHaveBeenCalledWith("conv_done", true, "FINISHED:sealed-1");
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


it.each(["IDLE", "PAUSED", "ERROR"] as const)("loads legacy persisted import bytes in %s without trusting the old message", async (status) => {
  const legacy = { ...importReady, meta: {}, message: { role: "user", content: "Old localized import notice" } };
  listProjectsMock.mockResolvedValue({ projects: [{ id: "conv_legacy", files_missing: false }], status: "ok" });
  useProjectManifestMock.mockReturnValue({ data: { files: [{ path: "original.txt", bytes: 20 }] } });
  const mount = () => render(withQc(<ExecutionCanvas events={[legacy]} status={status} cid="conv_legacy" />));
  const view = mount();
  await waitFor(() => expect(useProjectManifestMock).toHaveBeenCalledWith("conv_legacy", true, `${status}:project`));
  expect(screen.getByText("Imported source text")).toBeVisible();
  expect(useProjectFileMock).toHaveBeenCalledWith("conv_legacy", "original.txt", `${status}:project`);
  expect(listProjectsMock).toHaveBeenCalledTimes(1);
  view.unmount(); mount();
  await waitFor(() => expect(screen.getByText("Imported source text")).toBeVisible());
  expect(listProjectsMock).toHaveBeenCalledTimes(2);
});

it.each([
  { id: "some_other_project", files_missing: false },
  { id: "conv_missing", files_missing: true },
])("does not request a manifest for unmatched or missing project storage: %j", async (project) => {
  listProjectsMock.mockResolvedValue({ projects: [project], status: "ok" });
  render(withQc(<ExecutionCanvas events={[]} status="IDLE" cid="conv_missing" />));
  await waitFor(() => expect(listProjectsMock).toHaveBeenCalledTimes(1));
  expect(useProjectManifestMock).toHaveBeenLastCalledWith("conv_missing", false, "IDLE:");
  expect(useProjectFileMock).not.toHaveBeenCalled();
});

it.each([{ status: "RUNNING", untrusted: false }, { status: "IDLE", untrusted: true }] as const)("never probes legacy workspace existence during running/untrusted states: %j", ({ status, untrusted }) => {
  render(withQc(<ExecutionCanvas events={[]} status={status} untrusted={untrusted} cid="conv_probe" />));
  expect(listProjectsMock).not.toHaveBeenCalled();
  expect(useProjectManifestMock).toHaveBeenLastCalledWith("conv_probe", false, `${status}:`);
});

it("ignores cached saved files while the current final capture is pending", () => {
  const completed = finishedWorkspaceEvents("delayed");
  useProjectManifestMock.mockReturnValue({ data: { files: [{ path: "original.txt", bytes: 20 }] } });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (events: AgentEvent[]) => <QueryClientProvider client={qc}><ExecutionCanvas events={events} status="FINISHED" cid="delayed" /></QueryClientProvider>;
  const { rerender } = render(view([completed[0]]));
  expect(useProjectManifestMock).toHaveBeenLastCalledWith("delayed", false, "FINISHED:");
  expect(screen.queryByText("Imported source text")).not.toBeInTheDocument();
  expect(useProjectFileMock).not.toHaveBeenCalled();
  rerender(view(completed));
  expect(useProjectManifestMock).toHaveBeenLastCalledWith("delayed", true, "FINISHED:sealed-1");
  expect(screen.getByText("Imported source text")).toBeVisible();
  const next = finishedWorkspaceEvents("delayed", 30, 2);
  useProjectFileMock.mockClear();
  rerender(view([...completed, next[0]]));
  expect(useProjectManifestMock).toHaveBeenLastCalledWith("delayed", false, "FINISHED:sealed-1");
  expect(screen.queryByText("Imported source text")).not.toBeInTheDocument();
  expect(useProjectFileMock).not.toHaveBeenCalled();
  rerender(view([...completed, ...next]));
  expect(useProjectManifestMock).toHaveBeenLastCalledWith("delayed", true, "FINISHED:sealed-2");
});
