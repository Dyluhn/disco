/**
 * Canonical Build Preview contracts. Owned web builds render only the
 * platform-managed Preview; srcdoc remains a hardened artifact-viewer fallback
 * for imported/untrusted content.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";
import { FilesPane } from "@/components/build/canvas/FilesPane";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import { previewHostUrl } from "@/api/preview";
import type { AgentEvent, PreviewInfo } from "@/types/agent";

const {
  canonicalPreviewBootstrapUrlMock,
  useBuildPreviewMock,
  useWorkspaceVersionsMock,
} = vi.hoisted(() => ({
  canonicalPreviewBootstrapUrlMock: vi.fn((cid: string, target = "/") =>
    Promise.resolve({
      url: `http://127.77.0.2:8000/__disco/preview-auth/${cid}`,
      intent: `canonical:${cid}:${target}`,
    }),
  ),
  useBuildPreviewMock: vi.fn<
    (...args: unknown[]) => { data: PreviewInfo | null }
  >(() => ({ data: null })),
  useWorkspaceVersionsMock: vi.fn<
    (...args: unknown[]) => {
      versions: never[];
      loading: boolean;
      refetch: ReturnType<typeof vi.fn>;
    }
  >(() => ({ versions: [], loading: false, refetch: vi.fn() })),
}));

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: (...args: unknown[]) => useBuildPreviewMock(...args),
}));

vi.mock("@/hooks/useWorkspaceVersions", () => ({
  useWorkspaceVersions: (...args: unknown[]) =>
    useWorkspaceVersionsMock(...args),
}));

vi.mock("@/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    agentHttpBase: () => "http://agent.test:8000",
    canonicalPreviewBootstrapUrl: canonicalPreviewBootstrapUrlMock,
  };
});

function withClient(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

function fileWrite(path: string, content: string): AgentEvent {
  return {
    id: `write-${path}-${content.length}`,
    kind: "action",
    thought: "",
    tool_call: { tool_name: "file_write", arguments: { path, content } },
  } as AgentEvent;
}

function deliverable(
  id: string,
  path: string,
  artifactKind: "app" | "files",
): AgentEvent {
  return {
    id,
    kind: "deliverable",
    title: id,
    path,
    artifact_kind: artifactKind,
    deployment_url: "",
  } as AgentEvent;
}

function version(seq = 1): AgentEvent {
  return {
    id: `version-${seq}`,
    kind: "workspace_version",
    version_seq: seq,
    tree_digest: `tree-${seq}`,
    trigger: "finish",
  } as AgentEvent;
}

function sealedVersion(seq = 1): AgentEvent {
  return {
    ...version(seq),
    id: `sealed-version-${seq}`,
    final_seal: {
      schema_version: 1,
      scope: { namespace: "workspace.tree", identifier: "conv" },
      terminal_seq: 10,
      latest_effect_seq: 9,
      version_seq: seq,
      tree_digest: `tree-${seq}`,
    },
  } as AgentEvent;
}

const HTML = fileWrite(
  "index.html",
  "<html><body><h1>Canonical Preview</h1></body></html>",
);
const VITE_HTML = fileWrite(
  "index.html",
  '<div id="root"></div><script type="module" src="/src/main.tsx"></script>',
);
const NOTE = fileWrite("notes.txt", "not a web app");

beforeEach(() => {
  window.localStorage.clear();
  useBuildPreviewMock.mockReturnValue({ data: null });
  useWorkspaceVersionsMock.mockReturnValue({
    versions: [],
    loading: false,
    refetch: vi.fn(),
  });
  canonicalPreviewBootstrapUrlMock.mockImplementation(
    (cid: string, target = "/") =>
      Promise.resolve({
        url: `http://127.77.0.2:8000/__disco/preview-auth/${cid}`,
        intent: `canonical:${cid}:${target}`,
      }),
  );
});

afterEach(async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  vi.clearAllMocks();
});

describe("previewHostUrl legacy helper", () => {
  it("still fails closed for an unconfigured or bare-IP remote base", () => {
    expect(previewHostUrl("conv_12345678", 8000, "")).toBeNull();
    expect(
      previewHostUrl("conv_abcdef123456", 8000, "http://192.0.2.10:8000"),
    ).toBeNull();
  });

  it("keeps the old host transport isolated when an operator uses it", () => {
    expect(
      previewHostUrl("conv_abcdef123456", 8000, "http://127.0.0.1:8000"),
    ).toBe("http://p2-abcdef12-8000.localhost:8000");
  });

  // Restored 2026-07-26. These four cases were dropped when the preview
  // architecture moved to canonical capabilities, but previewHostUrl is still
  // live production code (api/client.ts:94, called at :208), so its host-rewrite
  // rules lost their only coverage. Epic 5 forbids a removed test on a shipping
  // path.
  it("extracts cid8 and maps localhost to .localhost", () => {
    expect(previewHostUrl("conv_abcdef123456", 5173, "http://localhost:8000")).toBe(
      "http://p2-abcdef12-5173.localhost:8000",
    );
  });

  it("prepends cid8-port to other hostnames", () => {
    expect(previewHostUrl("conv_11112222", 8000, "http://my-magic-dns.net")).toBe(
      "http://p2-11112222-8000.my-magic-dns.net",
    );
    expect(previewHostUrl("conv_11112222", 8000, "https://my-magic-dns.net:443")).toBe(
      "https://p2-11112222-8000.my-magic-dns.net",
    );
  });

  it("maps IPv6 loopback and fails closed on other bare IPs", () => {
    expect(previewHostUrl("conv_abcdef123456", 8000, "http://[::1]:8000")).toBe(
      "http://p2-abcdef12-8000.localhost:8000",
    );
    expect(previewHostUrl("conv_abcdef123456", 8000, "http://[2001:db8::1]:8000")).toBeNull();
  });

  it("drops a relative API prefix from the wildcard preview origin", () => {
    const expected = new URL("/", window.location.origin);
    expected.hostname = "p2-abcdef12-8000.localhost";
    expect(previewHostUrl("conv_abcdef123456", 8000, "/svc/agent")).toBe(
      expected.toString().replace(/\/+$/, ""),
    );
  });
});

describe("ExecutionCanvas — canonical Preview selection", () => {
  it("switches once on FINISHED for a runtime-directory app handoff", async () => {
    const app = deliverable("web-app", ".", "app");
    const laterFile = deliverable("readme", "README.md", "files");
    const { rerender } = render(
      withClient(
        <ExecutionCanvas events={[NOTE]} status="RUNNING" cid="conv_finish" />,
      ),
    );
    expect(screen.getByRole("tab", { name: "Preview" })).toHaveAttribute(
      "aria-selected",
      "false",
    );

    rerender(
      withClient(
        <ExecutionCanvas
          events={[NOTE, app, laterFile]}
          status="FINISHED"
          cid="conv_finish"
        />,
      ),
    );
    expect(screen.getByRole("tab", { name: "Preview" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await screen.findByTitle("Preview");
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
  });

  it("does not force Preview when no app or HTML exists", () => {
    const { rerender } = render(
      withClient(
        <ExecutionCanvas events={[NOTE]} status="RUNNING" cid="conv_nonweb" />,
      ),
    );
    rerender(
      withClient(
        <ExecutionCanvas events={[NOTE]} status="FINISHED" cid="conv_nonweb" />,
      ),
    );
    expect(screen.getByRole("tab", { name: "Preview" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });
});

describe("PreviewPane — one canonical owned-web surface", () => {
  it.each([
    ["plain static", HTML, "static"],
    ["Vite", VITE_HTML, "framework"],
  ])(
    "uses the real canonical frame for %s",
    async (_name, entry, launchKind) => {
      useBuildPreviewMock.mockReturnValue({
        data: {
          available: true,
          status: "running",
          generation: `generation-${launchKind}`,
          launch_kind: launchKind,
          reload_strategy: launchKind === "framework" ? "hmr" : "reload",
          port: 43120,
        },
      });
      render(
        withClient(
          <PreviewPane
            events={[entry, deliverable("app", "index.html", "app")]}
            status="RUNNING"
            cid={`conv_${launchKind}`}
          />,
        ),
      );

      expect(await screen.findByTitle("Preview")).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /rendered|live server/i }),
      ).not.toBeInTheDocument();
      expect(screen.queryByTitle("Static preview")).not.toBeInTheDocument();
      await waitFor(() =>
        expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledWith(
          `conv_${launchKind}`,
          "/?_disco_refresh=0",
        ),
      );
    },
  );

  it("shows an honest preparing state until the canonical capability is ready", () => {
    canonicalPreviewBootstrapUrlMock.mockImplementation(
      () => new Promise<never>(() => {}),
    );
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        status: "starting",
        generation: "starting-1",
      },
    });
    render(
      withClient(
        <PreviewPane
          events={[deliverable("app", ".", "app")]}
          status="RUNNING"
          cid="conv_preparing"
        />,
      ),
    );

    expect(screen.getByText("Preparing Preview")).toBeInTheDocument();
    expect(screen.queryByTitle("Preview")).not.toBeInTheDocument();
  });

  it("retains the last valid frame while the runtime is unavailable", async () => {
    const current: { data: PreviewInfo | null } = {
      data: {
        available: true,
        status: "running",
        generation: "stable-generation",
        reload_strategy: "reload",
      },
    };
    useBuildPreviewMock.mockImplementation(() => current);
    const events = [HTML, deliverable("app", "index.html", "app")];
    const { rerender } = render(
      withClient(
        <PreviewPane events={events} status="RUNNING" cid="conv_retained" />,
      ),
    );
    const frame = await screen.findByTitle("Preview");

    current.data = {
      available: false,
      status: "crashed",
      generation: "stable-generation",
      reason: "managed runtime exited",
      reload_strategy: "reload",
    };
    rerender(
      withClient(
        <PreviewPane events={events} status="RUNNING" cid="conv_retained" />,
      ),
    );

    expect(screen.getByTitle("Preview")).toBe(frame);
    // Restored 2026-07-26: the TRUSTED runtime frame's sandbox posture lost its
    // only assertion when the preview moved to canonical capabilities. The
    // untrusted path is asserted below (sandbox=""); without this, a change that
    // silently widened or dropped the trusted sandbox would pass every test.
    expect(frame).toHaveAttribute(
      "sandbox",
      "allow-scripts allow-forms allow-same-origin allow-popups allow-downloads",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Preview runtime is recovering: managed runtime exited",
    );
    expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(1);
  });

  it("keeps the latest app canonical when a file handoff follows it", async () => {
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: false,
        status: "stopped",
        generation: "sealed-app",
        reason: "sealed snapshot",
      },
    });
    render(
      withClient(
        <PreviewPane
          events={[
            HTML,
            deliverable("site", "release/index.html", "app"),
            deliverable("notes", "release/notes.txt", "files"),
            version(),
          ]}
          status="FINISHED"
          cid="conv_app_then_files"
        />,
      ),
    );

    expect(await screen.findByTitle("Preview")).toBeInTheDocument();
    expect(
      screen.queryByTitle("Static artifact viewer"),
    ).not.toBeInTheDocument();
    expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledWith(
      "conv_app_then_files",
      "/?_disco_refresh=0",
    );
  });

  it("fails closed when canonical capability minting fails", async () => {
    canonicalPreviewBootstrapUrlMock.mockRejectedValue(
      new Error('{"detail":{"reason":"preview_unavailable"}}'),
    );
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        status: "running",
        generation: "cannot-mint",
      },
    });
    render(
      withClient(
        <PreviewPane events={[HTML]} status="RUNNING" cid="conv_fail_closed" />,
      ),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "preview_unavailable",
    );
    expect(screen.queryByTitle("Preview")).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("preview-app");
  });

  // Regression: a finished static-site Build showed only the bare code
  // "preview_unavailable" (and, before the mint resolved, kept claiming the
  // runtime was "Starting…"), so the user was never told what happened or what
  // to do about it.
  it("explains a refused preview capability in plain words instead of only the raw reason code", async () => {
    canonicalPreviewBootstrapUrlMock.mockRejectedValue(
      new Error('{"detail":{"reason":"preview_unavailable"}}'),
    );
    useBuildPreviewMock.mockReturnValue({
      data: {
        available: true,
        status: "running",
        generation: "pv_finished_static_site",
      },
    });
    render(
      withClient(
        <PreviewPane events={[HTML]} status="FINISHED" cid="conv_static_site" />,
      ),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Preview is not available for this run");
    expect(alert).toHaveTextContent("Download source");
    expect(alert).not.toHaveTextContent(
      "Starting the platform-managed application runtime",
    );
    // The code stays visible, but only as a secondary line.
    expect(alert).toHaveTextContent("Reason code: preview_unavailable");
  });

  // Regression: a run reports FINISHED 1-4s before its final seal is durable, so
  // a capability minted in that window is correctly refused. Nothing else moved
  // the launch key afterwards, so the pane stayed on that refusal until someone
  // pressed Refresh. The seal's arrival must re-mint on its own.
  it("re-mints when the final seal lands so a capability refused during finalize does not latch", async () => {
    canonicalPreviewBootstrapUrlMock.mockRejectedValue(
      new Error('{"detail":{"reason":"preview_unavailable"}}'),
    );
    useBuildPreviewMock.mockReturnValue({
      data: { available: true, status: "running", generation: "pv_sealing" },
    });
    const finishing = [HTML, deliverable("site", "index.html", "app"), version(1)];
    const { rerender } = render(
      withClient(
        <PreviewPane events={finishing} status="FINISHED" cid="conv_seal_race" />,
      ),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Preview is not available for this run",
    );
    expect(screen.queryByTitle("Preview")).not.toBeInTheDocument();

    // The final seal lands a moment later; the capability now mints.
    canonicalPreviewBootstrapUrlMock.mockResolvedValue({
      url: "http://127.77.0.2:8000/__disco/preview-auth/conv_seal_race",
      intent: "canonical:conv_seal_race:/",
    });
    rerender(
      withClient(
        <PreviewPane
          events={[...finishing, sealedVersion(1)]}
          status="FINISHED"
          cid="conv_seal_race"
        />,
      ),
    );

    expect(await screen.findByTitle("Preview")).toBeInTheDocument();
  });
});

describe("PreviewPane — hardened artifact viewer", () => {
  it("uses srcdoc only for untrusted imported HTML and never mints runtime access", () => {
    render(
      withClient(
        <PreviewPane events={[HTML]} status="FINISHED" cid={null} untrusted />,
      ),
    );

    const frame = screen.getByTitle("Static artifact viewer");
    expect(frame).toHaveAttribute("srcdoc");
    expect(frame).toHaveAttribute("sandbox", "");
    expect(screen.getByText(/not a runtime Preview/i)).toBeInTheDocument();
    expect(canonicalPreviewBootstrapUrlMock).not.toHaveBeenCalled();
  });

  it("serves a server-side HTML artifact through the hardened artifact route", () => {
    render(
      withClient(
        <PreviewPane
          events={[slidesObservationEvent("deck.html")]}
          status="FINISHED"
          cid="conv_artifact"
          untrusted
        />,
      ),
    );

    const frame = screen.getByTitle("Static artifact viewer");
    expect(frame.getAttribute("src")).toMatch(
      /\/artifacts\/deck\.html\?inline=true/,
    );
    expect(frame).toHaveAttribute("sandbox", "");
    expect(canonicalPreviewBootstrapUrlMock).not.toHaveBeenCalled();
  });
});

function slidesObservationEvent(filename: string): AgentEvent {
  return {
    kind: "observation",
    id: `observation-${filename}`,
    action_id: `action-${filename}`,
    tool_result: {
      tool_name: "slides_generate",
      success: true,
      content: "",
      structured: { filename },
    },
  } as AgentEvent;
}

describe("FilesPane — server-side artifact sizes", () => {
  it("does not claim a server-side artifact has zero bytes", () => {
    render(
      <FilesPane
        events={[slidesObservationEvent("deck.html")]}
        streamingFile={null}
      />,
    );
    expect(screen.queryByText(/^0 B$/)).not.toBeInTheDocument();
  });

  it("shows byte size for event-carried content", () => {
    render(<FilesPane events={[HTML]} streamingFile={null} />);
    expect(screen.getByText(/\d+ B/)).toBeInTheDocument();
  });
});
