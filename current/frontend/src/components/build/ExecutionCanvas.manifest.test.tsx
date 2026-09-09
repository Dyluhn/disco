/**
 * UI-7 — the committed-workspace manifest is not fetched before it can exist.
 *
 * While a build was still RUNNING the canvas asked for
 * `/api/projects/<cid>/manifest`, got the entirely expected 404 (nothing has
 * been committed yet), and put a console error on the page on every visit. The
 * request is now gated on a workspace actually having been sealed.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent } from "@/types/agent";

const { useProjectManifestMock } = vi.hoisted(() => ({
  useProjectManifestMock: vi.fn(() => ({ data: undefined })),
}));

vi.mock("@/hooks/useProjects", () => ({
  useProjectManifest: (...args: unknown[]) => useProjectManifestMock(...(args as [])),
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
});

describe("ExecutionCanvas — the project manifest is only requested once one can exist", () => {
  it("does NOT request the manifest while a run is still working with nothing committed", () => {
    render(withQc(<ExecutionCanvas events={[]} status="RUNNING" cid="conv_running" />));
    expect(useProjectManifestMock).toHaveBeenCalledWith("conv_running", false);
  });

  it("requests it as soon as a workspace version has landed mid-run", () => {
    render(withQc(<ExecutionCanvas events={[sealed]} status="RUNNING" cid="conv_sealed" />));
    expect(useProjectManifestMock).toHaveBeenCalledWith("conv_sealed", true);
  });

  it("requests it on a finished run", () => {
    render(withQc(<ExecutionCanvas events={[]} status="FINISHED" cid="conv_done" />));
    expect(useProjectManifestMock).toHaveBeenCalledWith("conv_done", true);
  });
});
